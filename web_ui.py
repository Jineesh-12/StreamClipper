import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8765"))
PROJECT_DIR = Path(__file__).resolve().parent
SCRIPT_PATH = PROJECT_DIR / "scripts" / "streamclips.py"

_default_out = (
    "/tmp/StreamClips"
    if os.name != "nt" and Path("/tmp").exists()
    else str(Path.home() / "StreamClips")
)
BASE_OUT_DIR = Path(os.environ.get("STREAMCLIPS_OUT_DIR", _default_out)).expanduser().resolve()
BASE_OUT_DIR.mkdir(parents=True, exist_ok=True)

VENV_PYTHON = Path.home() / ".streamclips" / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
PYTHON_EXE = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable

AUTO_TOPIC = "Highlights"
AUTO_LAYOUT = "auto"
AUTO_FOCUS = ""
AUTO_FX_ENABLED = True
MAX_STORED_JOBS = 40
AUTO_CLEANUP_SEC = 1800

JOBS_LOCK = threading.Lock()
JOBS: dict[str, dict] = {}


def _sanitize_folder_name(raw_name: str, fallback: str = "Video_Clips") -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", (raw_name or "").strip())
    cleaned = re.sub(r"\s+", "_", cleaned).strip("._ ")
    if not cleaned:
        cleaned = fallback
    return cleaned[:80]


def _infer_video_folder_name(source: str, uploaded_names: list[str] | None = None) -> str:
    if uploaded_names:
        first_stem = Path(uploaded_names[0]).stem
        return _sanitize_folder_name(first_stem, "Uploaded_Video")

    src = (source or "").strip()
    if not src:
        return "StreamClipper_Video"

    parsed = urlparse(src)
    if parsed.scheme in ("http", "https"):
        qs = parse_qs(parsed.query)
        if "v" in qs and qs["v"]:
            return _sanitize_folder_name(f"YouTube_{qs['v'][0]}", "YouTube_Video")
        parts = [p for p in parsed.path.split("/") if p]
        if parts:
            last = parts[-1]
            if last in ("watch", "shorts", "live", "embed") and len(parts) >= 2:
                return _sanitize_folder_name(f"{parts[-2]}_{parts[-1]}", "Web_Video")
            return _sanitize_folder_name(Path(last).stem, "Web_Video")
        return _sanitize_folder_name(parsed.netloc, "Web_Video")

    return _sanitize_folder_name(Path(src).stem, "Local_Video")


def _new_job(job_id: str, out_folder: Path, video_folder: str) -> dict:
    return {
        "job_id": job_id,
        "running": True,
        "phase": "generating",
        "logs": [],
        "clips_created": 0,
        "clips": [],
        "zip_url": f"/api/download-zip/{job_id}",
        "zip_name": f"StreamClipper_{video_folder}.zip",
        "video_folder": video_folder,
        "current_step": "Starting...",
        "progress_pct": 5,
        "output_folder": str(out_folder),
        "created_at": time.time(),
        "finished_at": None,
        "cleaned_up": False,
    }


def _register_job(job_id: str, out_folder: Path, video_folder: str) -> None:
    with JOBS_LOCK:
        JOBS[job_id] = _new_job(job_id, out_folder, video_folder)
        if len(JOBS) > MAX_STORED_JOBS:
            oldest_ids = sorted(
                JOBS.keys(),
                key=lambda k: JOBS[k].get("created_at", 0.0),
            )[: len(JOBS) - MAX_STORED_JOBS]
            for old_id in oldest_ids:
                if not JOBS[old_id].get("running"):
                    old_dir = Path(JOBS[old_id].get("output_folder", ""))
                    if old_dir.exists() and old_dir != BASE_OUT_DIR:
                        shutil.rmtree(old_dir, ignore_errors=True)
                    JOBS.pop(old_id, None)


def append_log(job_id: str, message: str) -> None:
    stamp = time.strftime("%H:%M:%S")
    line = f"[{stamp}] {message}"
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job["logs"].append(line)
        if len(job["logs"]) > 600:
            job["logs"] = job["logs"][-600:]


def set_job_status(job_id: str, **kwargs) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job.update(kwargs)


def _organize_and_refresh_clips(job_id: str, out_folder: Path) -> list[dict]:
    with JOBS_LOCK:
        job = JOBS.get(job_id) or {}
        video_folder = _sanitize_folder_name(job.get("video_folder", "StreamClipper_Video"))

    target_dir = out_folder / "StreamClipper" / video_folder
    target_dir.mkdir(parents=True, exist_ok=True)

    mp4s = [
        p for p in out_folder.rglob("*.mp4")
        if p.is_file() and not p.name.endswith("_music_temp.mp4")
    ]
    for src_mp4 in mp4s:
        dest_mp4 = target_dir / src_mp4.name
        if src_mp4.resolve() != dest_mp4.resolve():
            shutil.move(str(src_mp4), str(dest_mp4))

    clip_items: list[dict] = []
    for p in sorted(target_dir.glob("*.mp4")):
        if not p.is_file():
            continue
        rel_name = p.relative_to(out_folder).as_posix()
        size_mb = round(p.stat().st_size / (1024 * 1024), 2)
        clip_items.append({
            "name": p.name,
            "rel_path": rel_name,
            "size_mb": size_mb,
            "url": f"/api/download/{job_id}/{rel_name}",
        })

    set_job_status(
        job_id,
        clips=clip_items,
        clips_created=len(clip_items),
        zip_url=f"/api/download-zip/{job_id}" if clip_items else "",
        zip_name=f"StreamClipper_{video_folder}.zip",
    )
    return clip_items


def _schedule_cleanup(job_id: str, out_folder: Path, delay_sec: int = AUTO_CLEANUP_SEC) -> None:
    def _cleanup_later() -> None:
        time.sleep(delay_sec)
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if not job or job.get("running"):
                return
            job["cleaned_up"] = True
            job["clips"] = []
            job["zip_url"] = ""
        if out_folder.exists() and out_folder != BASE_OUT_DIR:
            shutil.rmtree(out_folder, ignore_errors=True)

    threading.Thread(target=_cleanup_later, daemon=True).start()


def _get_job_snapshot(job_id: str | None) -> dict:
    with JOBS_LOCK:
        if not JOBS:
            return {
                "job_id": "",
                "running": False,
                "phase": "idle",
                "logs": [],
                "clips_created": 0,
                "clips": [],
                "zip_url": "",
                "zip_name": "StreamClipper_Clips.zip",
                "current_step": "Idle",
                "progress_pct": 0,
                "output_folder": str(BASE_OUT_DIR),
                "cleaned_up": False,
            }
        target_id = job_id if job_id and job_id in JOBS else max(
            JOBS.keys(), key=lambda k: JOBS[k].get("created_at", 0.0)
        )
        snap = dict(JOBS[target_id])
        snap["logs"] = list(JOBS[target_id]["logs"])
        snap["clips"] = list(JOBS[target_id]["clips"])
        return snap


def _job_cache_dir(job_out_folder: Path) -> Path:
    cache_dir = job_out_folder / ".cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def verify_gemini_api_key(api_key: str) -> tuple[bool, str]:
    """Verify a user-supplied Google Gemini API key with a lightweight test request."""
    key = (api_key or "").strip()
    if not key:
        return False, "Please paste a Google API Key first."

    try:
        from google import genai
    except ImportError:
        return False, "Server is missing the google-genai package."

    test_models = [
        "gemini-2.5-flash-lite",
        "gemini-2.5-flash",
        "gemini-3.1-flash-lite",
    ]
    last_err = ""
    client = genai.Client(api_key=key)
    for model_name in test_models:
        try:
            resp = client.models.generate_content(
                model=model_name,
                contents="Reply with OK",
            )
            if resp and (resp.text or "").strip():
                return True, f"Verified! Key is active ({model_name})."
        except Exception as exc:  # noqa: BLE001
            err_str = str(exc)
            last_err = err_str
            low = err_str.lower()
            if any(
                bad in low
                for bad in ("api_key_invalid", "api key not valid", "invalid argument", "401", "403", "permission_denied")
            ):
                return False, "Invalid Google API Key. Check the key and try again."
            if "not found" in low or "404" in low:
                continue
            break

    return False, f"Could not verify API key: {last_err[:180] or 'Unknown error'}"


def run_clip_worker(job_id: str, form: dict) -> None:
    source = str(form.get("source", "")).strip()
    user_api_key = str(form.get("api_key", "")).strip()
    num_clips = str(form.get("num_clips", "6")).strip() or "6"
    min_sec = str(form.get("min_sec", "20")).strip() or "20"
    max_sec = str(form.get("max_sec", "60")).strip() or "60"
    beep_words = bool(form.get("beep_words", True))
    out_folder = BASE_OUT_DIR / "jobs" / job_id
    out_folder.mkdir(parents=True, exist_ok=True)
    cache_home = _job_cache_dir(out_folder)

    set_job_status(job_id, running=True, phase="generating", current_step="Starting...", progress_pct=8)

    try:
        if not user_api_key:
            append_log(job_id, "Error: No Google API Key set.")
            set_job_status(job_id, running=False, phase="error", current_step="API key required", progress_pct=0)
            return

        if not source:
            append_log(job_id, "Error: Paste a video/stream URL or click Upload to select a file.")
            set_job_status(job_id, running=False, phase="error", current_step="Missing input", progress_pct=0)
            return

        if not SCRIPT_PATH.exists():
            append_log(job_id, f"Error: Missing {SCRIPT_PATH}")
            set_job_status(job_id, running=False, phase="error", current_step="Error", progress_pct=0)
            return

        cmd = [
            PYTHON_EXE,
            "-u",
            str(SCRIPT_PATH),
            "clip",
            source,
            "--brain",
            "gemini-api",
            "--topic",
            AUTO_TOPIC,
            "--clips",
            num_clips,
            "--layout",
            AUTO_LAYOUT,
            "--min",
            min_sec,
            "--max",
            max_sec,
            "--language",
            "en",
            "--fast",
            "--repick",
        ]
        if AUTO_FOCUS:
            cmd += ["--focus", AUTO_FOCUS]

        run_env = os.environ.copy()
        run_env["STREAMCLIPS_NO_ENV_FALLBACK"] = "1"
        run_env["GEMINI_API_KEY"] = user_api_key
        run_env["GOOGLE_API_KEY"] = user_api_key
        run_env["STREAMCLIPS_BLEEP"] = "1" if beep_words else "0"
        run_env["STREAMCLIPS_AUTO_FX"] = "1" if AUTO_FX_ENABLED else "0"
        run_env["STREAMCLIPS_OUT_DIR"] = str(out_folder)
        run_env["STREAMCLIPS_HOME"] = str(cache_home)
        run_env["YT_DLP_EXTRACTOR_ARGS"] = "youtube:player-client=android,mweb,web"
        run_env["PYTHONIOENCODING"] = "utf-8"
        run_env["PYTHONUTF8"] = "1"

        append_log(job_id, f"Job ID: {job_id}")
        append_log(job_id, f"Starting job ({num_clips} clips, {min_sec}s-{max_sec}s)...")

        proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=run_env,
        )
        assert proc.stdout is not None

        for line in proc.stdout:
            clean = line.rstrip()
            if not clean:
                continue
            append_log(job_id, clean)
            low = clean.lower()
            if "downloading" in low or "audio" in low:
                set_job_status(job_id, current_step="Downloading...", progress_pct=25)
            elif "listening" in low or "heard " in low or "whisper" in low:
                set_job_status(job_id, current_step="Transcribing...", progress_pct=50)
            elif "picking" in low or "picked " in low:
                set_job_status(job_id, current_step="Selecting moments...", progress_pct=72)
            elif "clip " in low and "saved " in low:
                _organize_and_refresh_clips(job_id, out_folder)
                set_job_status(job_id, current_step="Rendering clips...", progress_pct=88)

        rc = proc.wait()
        clips = _organize_and_refresh_clips(job_id, out_folder)

        if rc == 0:
            append_log(job_id, f"Done: {len(clips)} clip(s) ready in StreamClipper folder.")
            set_job_status(
                job_id,
                running=False,
                phase="completed",
                current_step="Completed",
                progress_pct=100,
                finished_at=time.time(),
            )
            _schedule_cleanup(job_id, out_folder)
        else:
            append_log(job_id, f"Exited with code {rc}.")
            set_job_status(
                job_id,
                running=False,
                phase="error",
                current_step="Error",
                progress_pct=0,
                finished_at=time.time(),
            )
            _schedule_cleanup(job_id, out_folder, delay_sec=600)

    except Exception as err:  # noqa: BLE001
        append_log(job_id, f"Error: {err}")
        set_job_status(
            job_id,
            running=False,
            phase="error",
            current_step="Error",
            progress_pct=0,
            finished_at=time.time(),
        )
        _schedule_cleanup(job_id, out_folder, delay_sec=600)


def run_upload_worker(
    job_id: str,
    form: dict,
    video_paths: list[str],
    music_path: str | None,
    upload_temp_folder: Path,
) -> None:
    """Process uploaded local videos using the existing StreamClipper engine."""
    out_folder = BASE_OUT_DIR / "jobs" / job_id
    out_folder.mkdir(parents=True, exist_ok=True)
    try:
        from scripts.upload_processor import process_uploads

        set_job_status(
            job_id,
            running=True,
            phase="generating",
            current_step="Preparing uploaded videos...",
            progress_pct=5,
        )
        append_log(job_id, f"Job ID: {job_id}")

        count = process_uploads(
            video_paths=video_paths,
            music_path=music_path,
            output_folder=str(out_folder),
            api_key=str(form.get("api_key", "")).strip(),
            num_clips=int(form.get("num_clips", 6)),
            min_sec=int(form.get("min_sec", 20)),
            max_sec=int(form.get("max_sec", 60)),
            beep_words=str(form.get("beep_words", "true")).lower() == "true",
            log=lambda msg: append_log(job_id, msg),
            status=lambda **kw: set_job_status(job_id, **kw),
        )
        clips = _organize_and_refresh_clips(job_id, out_folder)
        final_count = len(clips) if clips else count
        append_log(job_id, f"Complete: {final_count} clip(s) ready in StreamClipper folder.")
        set_job_status(
            job_id,
            running=False,
            phase="completed",
            current_step="Completed",
            progress_pct=100,
            clips_created=final_count,
            finished_at=time.time(),
        )
        _schedule_cleanup(job_id, out_folder)
    except Exception as exc:  # noqa: BLE001
        append_log(job_id, f"Upload error: {exc}")
        set_job_status(
            job_id,
            running=False,
            phase="error",
            current_step="Upload failed",
            progress_pct=0,
            finished_at=time.time(),
        )
        _schedule_cleanup(job_id, out_folder, delay_sec=600)
    finally:
        shutil.rmtree(upload_temp_folder, ignore_errors=True)


HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>StreamClipper Studio</title>
<style>
  :root {
    --bg-deep: #050510;
    --card-bg: rgba(9, 10, 26, 0.84);
    --input-bg: rgba(5, 7, 20, 0.86);
    --border-soft: rgba(139, 92, 246, 0.22);
    --neon-pink: #c026d3;
    --neon-violet: #7c3aed;
    --neon-blue: #0284c7;
    --text: #f8fafc;
    --muted: #9aa6c8;
    --ok: #34d399;
    --err: #f87171;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    color: var(--text);
    min-height: 100vh;
    padding: 24px 16px 68px;
    position: relative;
    overflow-x: hidden;
    background:
      radial-gradient(ellipse 85% 48% at 8% 100%, rgba(109, 40, 217, 0.24) 0%, rgba(76, 29, 149, 0.08) 45%, transparent 72%),
      radial-gradient(ellipse 80% 45% at 92% 94%, rgba(29, 78, 216, 0.22) 0%, rgba(88, 28, 135, 0.07) 45%, transparent 74%),
      radial-gradient(ellipse 70% 38% at 50% 0%, rgba(91, 33, 182, 0.14) 0%, transparent 65%),
      linear-gradient(170deg, #050510 0%, #070718 52%, #04081a 100%);
  }

  .wave-layer {
    position: fixed;
    inset: -12%;
    z-index: 0;
    pointer-events: none;
    overflow: hidden;
  }

  .wave-band {
    position: absolute;
    width: 145%;
    height: 36%;
    left: -22%;
    border-radius: 44% 56% 48% 52% / 62% 38% 62% 38%;
    filter: blur(42px);
    opacity: 0.16;
  }

  .wave-1 {
    bottom: -14%;
    background: linear-gradient(90deg, rgba(124, 58, 237, 0.45), rgba(37, 99, 235, 0.42), rgba(168, 85, 247, 0.35));
    transform: rotate(-12deg);
  }

  .wave-2 {
    bottom: 2%;
    background: linear-gradient(90deg, rgba(37, 99, 235, 0.32), rgba(109, 40, 217, 0.34), rgba(56, 189, 248, 0.24));
    transform: rotate(-15deg);
  }

  .wave-3 {
    top: -18%;
    background: linear-gradient(90deg, rgba(88, 28, 135, 0.34), rgba(67, 56, 202, 0.28), rgba(30, 64, 175, 0.24));
    transform: rotate(-10deg);
  }

  .app {
    position: relative;
    z-index: 1;
    max-width: 1020px;
    margin: 0 auto;
    display: grid;
    gap: 18px;
  }

  .topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 14px;
    flex-wrap: wrap;
    background: var(--card-bg);
    backdrop-filter: blur(18px);
    border: 1px solid var(--border-soft);
    border-radius: 22px;
    padding: 16px 20px;
    box-shadow: 0 18px 42px rgba(2, 3, 12, 0.72), 0 0 14px rgba(124, 58, 237, 0.07);
  }

  .brand {
    display: flex;
    align-items: center;
    gap: 13px;
  }

  .brand-mark {
    width: 40px;
    height: 40px;
    border-radius: 13px;
    background: linear-gradient(135deg, rgba(192, 38, 211, 0.85), rgba(124, 58, 237, 0.85) 50%, rgba(14, 165, 233, 0.85));
    display: grid;
    place-items: center;
    font-weight: 800;
    font-size: 0.95rem;
    color: #ffffff;
    box-shadow: 0 0 10px rgba(124, 58, 237, 0.28);
  }

  .brand h1 {
    font-size: 1.15rem;
    font-weight: 700;
    letter-spacing: -0.015em;
  }

  .top-actions {
    display: inline-flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }

  .status-pill {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 7px 14px;
    border-radius: 999px;
    font-size: 0.8rem;
    font-weight: 600;
    background: rgba(7, 9, 24, 0.85);
    border: 1px solid var(--border-soft);
    color: var(--muted);
  }

  .status-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--muted);
  }

  .status-pill.running {
    color: #bae6fd;
    border-color: rgba(56, 189, 248, 0.42);
  }
  .status-pill.running .status-dot { background: #38bdf8; }

  .status-pill.completed {
    color: #d1fae5;
    border-color: rgba(52, 211, 153, 0.42);
  }
  .status-pill.completed .status-dot { background: var(--ok); }

  .status-pill.error {
    color: #fee2e2;
    border-color: rgba(248, 113, 113, 0.42);
  }
  .status-pill.error .status-dot { background: var(--err); }

  .settings-btn {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    min-height: 38px;
    padding: 0 14px;
    border-radius: 999px;
    background: rgba(7, 9, 24, 0.88);
    border: 1px solid rgba(139, 92, 246, 0.34);
    color: #e2e8f0;
    font-size: 0.8rem;
    font-weight: 600;
    cursor: pointer;
  }

  .key-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--err);
  }

  .key-dot.ready {
    background: var(--ok);
  }

  .card {
    background: var(--card-bg);
    backdrop-filter: blur(18px);
    border: 1px solid var(--border-soft);
    border-radius: 22px;
    padding: 20px;
    box-shadow: 0 18px 42px rgba(2, 3, 12, 0.72), 0 0 14px rgba(124, 58, 237, 0.06);
  }

  .card-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 16px;
    flex-wrap: wrap;
  }

  .card-title {
    font-size: 0.96rem;
    font-weight: 700;
    color: #f8fafc;
  }

  .field label {
    display: block;
    font-size: 0.78rem;
    font-weight: 600;
    color: var(--muted);
    margin-bottom: 6px;
  }

  input[type="text"],
  input[type="password"] {
    width: 100%;
    background: var(--input-bg);
    border: 1px solid rgba(139, 92, 246, 0.28);
    color: var(--text);
    border-radius: 14px;
    padding: 12px 14px;
    font-size: 0.92rem;
    outline: none;
  }

  .upload-launch-row {
    margin-top: 14px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    flex-wrap: wrap;
  }

  .media-bar {
    display: none;
    margin-top: 12px;
    padding: 14px 16px;
    border-radius: 18px;
    background: rgba(5, 7, 20, 0.78);
    border: 1px solid rgba(139, 92, 246, 0.26);
    gap: 12px;
  }

  .media-bar.open {
    display: grid;
  }

  .media-toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    flex-wrap: wrap;
  }

  .pill-group {
    display: inline-flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }

  .pill-action {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    min-height: 40px;
    padding: 8px 16px;
    border-radius: 999px;
    background: rgba(9, 12, 30, 0.92);
    border: 1px solid rgba(139, 92, 246, 0.38);
    color: #e2e8f0;
    font-size: 0.84rem;
    font-weight: 600;
    cursor: pointer;
  }

  .pill-action.active {
    border-color: rgba(192, 132, 252, 0.72);
    background: linear-gradient(135deg, rgba(124, 58, 237, 0.28), rgba(14, 165, 233, 0.20));
    color: #ffffff;
  }

  .media-summary {
    font-size: 0.8rem;
    color: var(--muted);
    font-weight: 600;
  }

  .media-columns {
    display: grid;
    grid-template-columns: 1.25fr 1fr;
    gap: 12px;
  }

  @media (max-width: 760px) {
    .media-columns { grid-template-columns: 1fr; }
  }

  .media-box {
    background: rgba(7, 9, 24, 0.85);
    border: 1px solid rgba(139, 92, 246, 0.20);
    border-radius: 14px;
    padding: 11px 13px;
  }

  .media-box-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    font-size: 0.78rem;
    font-weight: 700;
    color: #e2e8f0;
    margin-bottom: 8px;
  }

  .count-chip {
    padding: 2px 9px;
    border-radius: 999px;
    font-size: 0.72rem;
    font-weight: 700;
    background: rgba(124, 58, 237, 0.22);
    border: 1px solid rgba(168, 85, 247, 0.35);
    color: #e9d5ff;
  }

  .file-list {
    display: grid;
    gap: 6px;
    max-height: 120px;
    overflow-y: auto;
  }

  .file-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    font-size: 0.78rem;
    padding: 6px 8px;
    border-radius: 10px;
    background: rgba(5, 7, 19, 0.82);
    border: 1px solid rgba(139, 92, 246, 0.14);
  }

  .file-name {
    color: #f1f5f9;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .file-badge {
    flex-shrink: 0;
    font-size: 0.7rem;
    font-weight: 700;
    padding: 2px 7px;
    border-radius: 999px;
  }

  .file-badge.ready {
    color: #bae6fd;
    background: rgba(56, 189, 248, 0.14);
    border: 1px solid rgba(56, 189, 248, 0.35);
  }

  .file-badge.uploaded {
    color: #a7f3d0;
    background: rgba(52, 211, 153, 0.16);
    border: 1px solid rgba(52, 211, 153, 0.40);
  }

  .file-badge.bad {
    color: #fecaca;
    background: rgba(248, 113, 113, 0.16);
    border: 1px solid rgba(248, 113, 113, 0.42);
  }

  .empty-note {
    font-size: 0.77rem;
    color: var(--muted);
  }

  .neon-stepper-bar {
    margin-top: 16px;
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    align-items: center;
    border-radius: 22px;
    padding: 14px 8px;
    background: rgba(6, 8, 22, 0.78);
    position: relative;
  }

  .neon-stepper-bar::before {
    content: "";
    position: absolute;
    inset: -1px;
    border-radius: 23px;
    padding: 1px;
    background: linear-gradient(90deg, rgba(192, 38, 211, 0.58), rgba(124, 58, 237, 0.55), rgba(14, 165, 233, 0.58));
    -webkit-mask: linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0);
    -webkit-mask-composite: xor;
    mask-composite: exclude;
    pointer-events: none;
  }

  @media (max-width: 820px) {
    .neon-stepper-bar {
      grid-template-columns: 1fr;
      gap: 14px;
      padding: 14px;
    }
  }

  .stepper-cell {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 11px;
    padding: 2px 16px;
    position: relative;
  }

  .stepper-heading {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    font-size: 0.92rem;
    font-weight: 700;
    color: #f1f5f9;
  }

  .pill-track {
    width: 100%;
    max-width: 245px;
    height: 46px;
    border-radius: 999px;
    background: rgba(5, 7, 19, 0.92);
    border: 1px solid rgba(139, 92, 246, 0.36);
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 4px;
  }

  .pill-btn {
    width: 44px;
    height: 36px;
    border-radius: 999px;
    border: 1px solid rgba(168, 85, 247, 0.44);
    background: rgba(18, 12, 40, 0.78);
    color: #f1f5f9;
    font-size: 1.2rem;
    font-weight: 600;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    user-select: none;
  }

  .pill-btn.plus-btn {
    border-color: rgba(56, 189, 248, 0.46);
    background: rgba(9, 22, 44, 0.78);
  }

  .pill-val {
    flex: 1;
    text-align: center;
    background: transparent;
    border: none;
    color: #ffffff;
    font-size: 1rem;
    font-weight: 700;
    width: 58px;
    outline: none;
    -moz-appearance: textfield;
  }

  .pill-val::-webkit-outer-spin-button,
  .pill-val::-webkit-inner-spin-button {
    -webkit-appearance: none;
    margin: 0;
  }

  .footer-bar {
    margin-top: 18px;
    padding-top: 16px;
    border-top: 1px solid rgba(139, 92, 246, 0.14);
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    flex-wrap: wrap;
  }

  .yn-control {
    display: inline-flex;
    align-items: center;
    gap: 12px;
  }

  .yn-label {
    font-size: 0.88rem;
    font-weight: 600;
    color: #e2e8f0;
  }

  .yn-group {
    display: inline-flex;
    gap: 6px;
    padding: 4px;
    border-radius: 999px;
    background: rgba(5, 7, 19, 0.88);
    border: 1px solid rgba(139, 92, 246, 0.32);
  }

  .yn-btn {
    border: 1px solid transparent;
    background: transparent;
    color: var(--muted);
    font-size: 0.82rem;
    font-weight: 700;
    min-height: 34px;
    padding: 6px 16px;
    border-radius: 999px;
    cursor: pointer;
  }

  .yn-btn.active {
    color: #ffffff;
    background: linear-gradient(135deg, rgba(124, 58, 237, 0.36), rgba(14, 165, 233, 0.28));
    border-color: rgba(168, 85, 247, 0.48);
  }

  .btn {
    background: linear-gradient(135deg, rgba(147, 51, 234, 0.88), rgba(79, 70, 229, 0.88) 55%, rgba(2, 132, 199, 0.88));
    color: #ffffff;
    border: 1px solid rgba(255, 255, 255, 0.18);
    border-radius: 999px;
    min-height: 42px;
    padding: 10px 22px;
    font-size: 0.88rem;
    font-weight: 700;
    cursor: pointer;
    text-decoration: none;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
  }

  .btn:disabled {
    opacity: 0.45;
    cursor: not-allowed;
  }

  .btn-ghost {
    background: rgba(7, 9, 24, 0.82);
    color: var(--muted);
    border: 1px solid var(--border-soft);
    border-radius: 999px;
    min-height: 34px;
    padding: 6px 14px;
    font-size: 0.78rem;
    font-weight: 600;
    cursor: pointer;
  }

  .stats-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 12px;
    margin-bottom: 14px;
  }

  @media (max-width: 700px) {
    .stats-grid { grid-template-columns: 1fr; }
  }

  .stat-box {
    background: var(--input-bg);
    border: 1px solid var(--border-soft);
    border-radius: 16px;
    padding: 12px 15px;
  }

  .stat-label {
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--muted);
  }

  .stat-val {
    font-size: 0.96rem;
    font-weight: 700;
    margin-top: 4px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .progress-wrap {
    width: 100%;
    height: 9px;
    background: rgba(5, 7, 19, 0.92);
    border: 1px solid var(--border-soft);
    border-radius: 999px;
    overflow: hidden;
    margin-bottom: 14px;
  }

  .progress-bar {
    height: 100%;
    width: 0%;
    background: linear-gradient(90deg, rgba(192, 38, 211, 0.82), rgba(124, 58, 237, 0.85), rgba(14, 165, 233, 0.85));
    transition: width 0.25s ease;
  }

  .clips-panel {
    margin-bottom: 14px;
    display: grid;
    gap: 8px;
  }

  .clip-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    flex-wrap: wrap;
    padding: 10px 14px;
    border-radius: 14px;
    background: rgba(6, 9, 24, 0.88);
    border: 1px solid rgba(52, 211, 153, 0.28);
  }

  .clip-meta {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }

  .clip-title {
    font-size: 0.86rem;
    font-weight: 700;
    color: #f8fafc;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .clip-sub {
    font-size: 0.74rem;
    color: var(--muted);
  }

  pre.console {
    background: rgba(3, 4, 14, 0.92);
    border: 1px solid var(--border-soft);
    border-radius: 16px;
    padding: 14px 16px;
    height: 240px;
    overflow-y: auto;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 0.8rem;
    line-height: 1.55;
    color: #cbd5e1;
    white-space: pre-wrap;
  }

  .modal-backdrop {
    position: fixed;
    inset: 0;
    z-index: 50;
    background: rgba(2, 3, 12, 0.82);
    backdrop-filter: blur(10px);
    display: none;
    align-items: center;
    justify-content: center;
    padding: 16px;
  }

  .modal-backdrop.open {
    display: flex;
  }

  .modal-card {
    width: 100%;
    max-width: 460px;
    background: rgba(10, 11, 30, 0.96);
    border: 1px solid rgba(139, 92, 246, 0.42);
    border-radius: 22px;
    padding: 22px 24px;
  }

  .modal-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 14px;
  }

  .modal-title {
    font-size: 1rem;
    font-weight: 700;
  }

  .api-row {
    display: flex;
    gap: 8px;
    align-items: center;
  }

  .api-status {
    margin-top: 10px;
    font-size: 0.82rem;
    font-weight: 600;
    min-height: 20px;
    color: var(--muted);
  }

  .api-status.ok { color: var(--ok); }
  .api-status.err { color: var(--err); }

  .modal-actions {
    margin-top: 16px;
    display: flex;
    justify-content: flex-end;
    gap: 10px;
  }

  .signature-badge {
    position: fixed;
    right: 16px;
    bottom: 14px;
    width: 112px;
    height: 32px;
    z-index: 20;
    pointer-events: none;
    user-select: none;
    display: flex;
    align-items: center;
    justify-content: center;
    border-radius: 999px;
    background: rgba(7, 8, 24, 0.78);
    backdrop-filter: blur(12px);
    border: 1px solid rgba(139, 92, 246, 0.30);
  }

  .sig-word {
    display: inline-flex;
    align-items: center;
    gap: 1px;
    font-size: 0.82rem;
    font-weight: 700;
    letter-spacing: 0.04em;
  }

  .sig-letter {
    display: inline-block;
    background: linear-gradient(135deg, #d8b4fe 0%, #a855f7 48%, #38bdf8 100%);
    -webkit-background-clip: text;
    background-clip: text;
    color: transparent;
    animation: sigWave 2.8s ease-in-out infinite;
  }

  .sig-letter:nth-child(1) { animation-delay: 0.00s; }
  .sig-letter:nth-child(2) { animation-delay: 0.12s; }
  .sig-letter:nth-child(3) { animation-delay: 0.24s; }
  .sig-letter:nth-child(4) { animation-delay: 0.36s; }
  .sig-letter:nth-child(5) { animation-delay: 0.48s; }
  .sig-letter:nth-child(6) { animation-delay: 0.60s; }
  .sig-letter:nth-child(7) { animation-delay: 0.72s; }

  @keyframes sigWave {
    0%, 100% { transform: translateY(0px); opacity: 0.86; }
    35% { transform: translateY(-3.5px); opacity: 1; }
    70% { transform: translateY(0.5px); opacity: 0.9; }
  }
</style>
</head>
<body>
<div class="wave-layer" aria-hidden="true">
  <div class="wave-band wave-1"></div>
  <div class="wave-band wave-2"></div>
  <div class="wave-band wave-3"></div>
</div>

<div class="app">
  <header class="topbar">
    <div class="brand">
      <div class="brand-mark">SC</div>
      <div>
        <h1>StreamClipper Studio</h1>
      </div>
    </div>

    <div class="top-actions">
      <div id="statusPill" class="status-pill">
        <span class="status-dot"></span>
        <span id="statusText">Idle</span>
      </div>

      <button type="button" class="settings-btn" onclick="openSettings()" title="API Key Settings" aria-label="Settings">
        <span id="keyDot" class="key-dot"></span>
        <span id="keyLabel">API Key</span>
      </button>
    </div>
  </header>

  <section class="card">
    <div class="card-head">
      <div class="card-title">New Clip Job</div>
    </div>

    <div class="field">
      <label for="sourceInput">Video / Stream URL</label>
      <input id="sourceInput" type="text" placeholder="https://www.youtube.com/watch?v=..." />
    </div>

    <input id="videoFiles" type="file" accept=".mp4,.mkv,.mov,.avi,.webm,.m4v,.wmv,.3gp,video/*" multiple style="display:none;" onchange="handleFileSelection('video')" />
    <input id="musicFile" type="file" accept=".mp3,.wav,.m4a,.aac,.flac,.ogg,.opus,audio/*" style="display:none;" onchange="handleFileSelection('music')" />

    <div class="upload-launch-row">
      <button type="button" class="pill-action" id="uploadToggleBtn" onclick="toggleUploadPanel()" aria-expanded="false">
        <span>Upload</span>
      </button>
      <span class="media-summary" id="closedMediaBadge"></span>
    </div>

    <div class="media-bar" id="mediaBar">
      <div class="media-toolbar">
        <div class="pill-group">
          <button type="button" class="pill-action" id="pickVideoBtn" onclick="document.getElementById('videoFiles').click()">
            <span>Upload video</span>
          </button>
          <button type="button" class="pill-action" id="pickMusicBtn" onclick="document.getElementById('musicFile').click()">
            <span>Upload music</span>
          </button>
          <button type="button" class="btn-ghost" id="clearMediaBtn" onclick="clearSelectedMedia()" style="display:none;">Clear files</button>
        </div>
        <div class="media-summary" id="mediaSummary">Paste a link or upload video file(s)</div>
      </div>

      <div class="media-columns">
        <div class="media-box">
          <div class="media-box-head">
            <span>Videos</span>
            <span class="count-chip" id="videoCountChip">0 files</span>
          </div>
          <div class="file-list" id="videoList">
            <div class="empty-note">No video files selected</div>
          </div>
        </div>

        <div class="media-box">
          <div class="media-box-head">
            <span>Background Music (Optional)</span>
            <span class="count-chip" id="musicCountChip">None</span>
          </div>
          <div class="file-list" id="musicList">
            <div class="empty-note">No music selected (optional)</div>
          </div>
        </div>
      </div>
    </div>

    <div class="neon-stepper-bar">
      <div class="stepper-cell">
        <div class="stepper-heading">
          <label for="numClipsInput" style="margin:0;color:inherit;font-size:inherit;font-weight:inherit;cursor:pointer;">Clips</label>
        </div>
        <div class="pill-track">
          <button type="button" class="pill-btn" aria-label="Decrease clips" onclick="stepField('numClipsInput', -1)">&minus;</button>
          <input id="numClipsInput" class="pill-val" type="number" value="4" min="1" max="20" onchange="syncBounds()" />
          <button type="button" class="pill-btn plus-btn" aria-label="Increase clips" onclick="stepField('numClipsInput', 1)">+</button>
        </div>
      </div>

      <div class="stepper-cell">
        <div class="stepper-heading">
          <label for="minSecInput" style="margin:0;color:inherit;font-size:inherit;font-weight:inherit;cursor:pointer;">Min Seconds</label>
        </div>
        <div class="pill-track">
          <button type="button" class="pill-btn" aria-label="Decrease min seconds" onclick="stepField('minSecInput', -5)">&minus;</button>
          <input id="minSecInput" class="pill-val" type="number" value="20" min="5" max="295" onchange="syncBounds('minSecInput')" />
          <button type="button" class="pill-btn plus-btn" aria-label="Increase min seconds" onclick="stepField('minSecInput', 5)">+</button>
        </div>
      </div>

      <div class="stepper-cell">
        <div class="stepper-heading">
          <label for="maxSecInput" style="margin:0;color:inherit;font-size:inherit;font-weight:inherit;cursor:pointer;">Max Seconds</label>
        </div>
        <div class="pill-track">
          <button type="button" class="pill-btn" aria-label="Decrease max seconds" onclick="stepField('maxSecInput', -5)">&minus;</button>
          <input id="maxSecInput" class="pill-val" type="number" value="60" min="10" max="300" onchange="syncBounds('maxSecInput')" />
          <button type="button" class="pill-btn plus-btn" aria-label="Increase max seconds" onclick="stepField('maxSecInput', 5)">+</button>
        </div>
      </div>
    </div>

    <div class="footer-bar">
      <div class="yn-control">
        <span class="yn-label">Mute profanity</span>
        <div class="yn-group" role="group" aria-label="Mute profanity">
          <button type="button" id="beepYesBtn" class="yn-btn active" onclick="setMuteProfanity(true)">Yes</button>
          <button type="button" id="beepNoBtn" class="yn-btn" onclick="setMuteProfanity(false)">No</button>
        </div>
      </div>
      <button class="btn" id="startBtn" type="button" onclick="startPipeline()">Generate Clips</button>
    </div>
  </section>

  <section class="card">
    <div class="card-head">
      <div class="card-title">Clips &amp; Logs</div>
      <button class="btn-ghost" type="button" onclick="clearConsole()">Clear Logs</button>
    </div>

    <div class="stats-grid">
      <div class="stat-box">
        <div class="stat-label">Stage</div>
        <div class="stat-val" id="stepVal">Idle</div>
      </div>
      <div class="stat-box">
        <div class="stat-label">Clips Ready</div>
        <div class="stat-val" id="countVal" style="color: var(--ok);">0</div>
      </div>
      <div class="stat-box">
        <div class="stat-label">Job ID</div>
        <div class="stat-val" id="jobIdVal" style="color: #cbd5e1; font-size: 0.86rem;">None</div>
      </div>
    </div>

    <div class="progress-wrap">
      <div class="progress-bar" id="progFill"></div>
    </div>

    <div class="clips-panel" id="clipsList"></div>

    <pre class="console" id="logBox">Ready.</pre>
  </section>
</div>

<div class="signature-badge" aria-label="Jineesh">
  <span class="sig-word" aria-hidden="true">
    <span class="sig-letter">J</span><span class="sig-letter">i</span><span class="sig-letter">n</span><span class="sig-letter">e</span><span class="sig-letter">e</span><span class="sig-letter">s</span><span class="sig-letter">h</span>
  </span>
</div>

<div id="settingsModal" class="modal-backdrop" onclick="if(event.target===this) closeSettings()">
  <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="modalTitle">
    <div class="modal-head">
      <div id="modalTitle" class="modal-title">Google API Key</div>
      <button type="button" class="btn-ghost" onclick="closeSettings()">Close</button>
    </div>

    <div class="field">
      <label for="apiKeyInput">Paste your Google Gemini API Key</label>
      <div class="api-row">
        <input id="apiKeyInput" type="password" placeholder="AIzaSy..." autocomplete="off" />
        <button type="button" class="btn-ghost" id="toggleVisBtn" onclick="toggleKeyVisibility()">Show</button>
      </div>
    </div>

    <div id="apiStatus" class="api-status"></div>

    <div class="modal-actions">
      <button type="button" class="btn-ghost" onclick="clearSavedKey()">Remove</button>
      <button type="button" class="btn" id="verifySaveBtn" onclick="verifyAndSaveKey()">Submit</button>
    </div>
  </div>
</div>

<script>
const STORAGE_KEY = "streamclipper_user_gemini_api_key";
const JOB_STORAGE_KEY = "streamclipper_active_job_id";
const VIDEO_EXTS = [".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".wmv", ".3gp"];
const MUSIC_EXTS = [".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"];

let muteProfanity = true;
let localClearedAt = 0;
let videoEntries = [];
let musicEntry = null;
let activeJobId = "";

try {
  activeJobId = (sessionStorage.getItem(JOB_STORAGE_KEY) || "").trim();
} catch (_) {}

function setActiveJobId(jid) {
  activeJobId = (jid || "").trim();
  try {
    if (activeJobId) sessionStorage.setItem(JOB_STORAGE_KEY, activeJobId);
    else sessionStorage.removeItem(JOB_STORAGE_KEY);
  } catch (_) {}
}

function toggleUploadPanel(forceOpen = null) {
  const bar = document.getElementById("mediaBar");
  const btn = document.getElementById("uploadToggleBtn");
  const isOpen = forceOpen !== null ? !!forceOpen : !bar.classList.contains("open");
  bar.classList.toggle("open", isOpen);
  btn.classList.toggle("active", isOpen || videoEntries.length > 0 || !!musicEntry);
  btn.setAttribute("aria-expanded", String(isOpen));
  renderMediaState();
}

function hasAllowedExt(fileName, exts) {
  const low = (fileName || "").toLowerCase();
  return exts.some((ext) => low.endsWith(ext));
}

function formatSize(bytes) {
  if (!bytes || bytes <= 0) return "Mobile file";
  const mb = bytes / (1024 * 1024);
  if (mb >= 1024) return `${(mb / 1024).toFixed(2)} GB`;
  if (mb >= 1) return `${mb.toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

function escapeHtml(text) {
  return String(text || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function handleFileSelection(kind) {
  toggleUploadPanel(true);
  if (kind === "video") {
    const files = Array.from(document.getElementById("videoFiles").files || []);
    videoEntries = files.map((file) => {
      const mime = (file.type || "").toLowerCase();
      const valid = hasAllowedExt(file.name, VIDEO_EXTS) || mime.startsWith("video/");
      return {
        file,
        name: file.name || "uploaded_video.mp4",
        sizeLabel: formatSize(file.size),
        state: valid ? "ready" : "bad",
        label: valid ? `Ready (${formatSize(file.size)})` : "Unsupported format",
      };
    });
  } else {
    const file = (document.getElementById("musicFile").files || [])[0] || null;
    if (!file) {
      musicEntry = null;
    } else {
      const mime = (file.type || "").toLowerCase();
      const valid = hasAllowedExt(file.name, MUSIC_EXTS) || mime.startsWith("audio/");
      musicEntry = {
        file,
        name: file.name || "background_music.mp3",
        sizeLabel: formatSize(file.size),
        state: valid ? "ready" : "bad",
        label: valid ? `Ready (${formatSize(file.size)})` : "Unsupported format",
      };
    }
  }
  renderMediaState();
}

function setMediaUploadStatus(state, customLabel = "") {
  videoEntries = videoEntries.map((item) => {
    if (item.state === "bad") return item;
    return {
      ...item,
      state,
      label: customLabel || (state === "uploaded" ? "Uploaded" : `Ready (${item.sizeLabel})`),
    };
  });
  if (musicEntry && musicEntry.state !== "bad") {
    musicEntry = {
      ...musicEntry,
      state,
      label: customLabel || (state === "uploaded" ? "Uploaded" : `Ready (${musicEntry.sizeLabel})`),
    };
  }
  renderMediaState();
}

function clearSelectedMedia() {
  document.getElementById("videoFiles").value = "";
  document.getElementById("musicFile").value = "";
  videoEntries = [];
  musicEntry = null;
  renderMediaState();
}

function renderMediaState() {
  const validVideos = videoEntries.filter((v) => v.state !== "bad");
  const badVideos = videoEntries.filter((v) => v.state === "bad");
  const barOpen = document.getElementById("mediaBar").classList.contains("open");

  document.getElementById("uploadToggleBtn").classList.toggle(
    "active",
    barOpen || videoEntries.length > 0 || !!musicEntry
  );
  document.getElementById("pickVideoBtn").classList.toggle("active", videoEntries.length > 0);
  document.getElementById("pickMusicBtn").classList.toggle("active", !!musicEntry);
  document.getElementById("clearMediaBtn").style.display =
    (videoEntries.length > 0 || musicEntry) ? "inline-flex" : "none";

  document.getElementById("videoCountChip").textContent =
    `${videoEntries.length} ${videoEntries.length === 1 ? "file" : "files"}`;
  document.getElementById("musicCountChip").textContent =
    musicEntry ? (musicEntry.state === "uploaded" ? "Uploaded" : "Checked") : "None";

  const vList = document.getElementById("videoList");
  if (!videoEntries.length) {
    vList.innerHTML = '<div class="empty-note">No video files selected</div>';
  } else {
    vList.innerHTML = videoEntries.map((v) => `
      <div class="file-item">
        <span class="file-name" title="${escapeHtml(v.name)}">${escapeHtml(v.name)}</span>
        <span class="file-badge ${v.state}">${escapeHtml(v.label)}</span>
      </div>
    `).join("");
  }

  const mList = document.getElementById("musicList");
  if (!musicEntry) {
    mList.innerHTML = '<div class="empty-note">No music selected (optional)</div>';
  } else {
    mList.innerHTML = `
      <div class="file-item">
        <span class="file-name" title="${escapeHtml(musicEntry.name)}">${escapeHtml(musicEntry.name)}</span>
        <span class="file-badge ${musicEntry.state}">${escapeHtml(musicEntry.label)}</span>
      </div>
    `;
  }

  const summary = document.getElementById("mediaSummary");
  const closedBadge = document.getElementById("closedMediaBadge");
  if (!videoEntries.length && !musicEntry) {
    summary.textContent = "Paste a link or upload video file(s)";
    closedBadge.textContent = "";
  } else if (badVideos.length > 0 || (musicEntry && musicEntry.state === "bad")) {
    summary.textContent = "Check highlighted file format before generating";
    closedBadge.textContent = !barOpen ? "File check needed" : "";
  } else {
    const parts = [`${validVideos.length} video(s)`];
    if (musicEntry) parts.push(`music: ${musicEntry.name}`);
    const allUploaded = validVideos.length > 0 && validVideos.every((v) => v.state === "uploaded");
    const statusTxt = `${parts.join(" • ")} (${allUploaded ? "Uploaded" : "Checked & Ready"})`;
    summary.textContent = statusTxt;
    closedBadge.textContent = !barOpen ? statusTxt : "";
  }
}

function getSavedApiKey() {
  try {
    return (localStorage.getItem(STORAGE_KEY) || "").trim();
  } catch (_) {
    return "";
  }
}

function refreshKeyIndicator() {
  const hasKey = !!getSavedApiKey();
  document.getElementById("keyDot").classList.toggle("ready", hasKey);
  document.getElementById("keyLabel").textContent = hasKey ? "API Ready" : "Set API Key";
}

function openSettings(statusMsg = "", isErr = false) {
  const input = document.getElementById("apiKeyInput");
  input.value = getSavedApiKey();
  input.type = "password";
  document.getElementById("toggleVisBtn").textContent = "Show";
  const st = document.getElementById("apiStatus");
  st.textContent = statusMsg;
  st.className = "api-status" + (statusMsg ? (isErr ? " err" : " ok") : "");
  document.getElementById("settingsModal").classList.add("open");
  setTimeout(() => input.focus(), 40);
}

function closeSettings() {
  document.getElementById("settingsModal").classList.remove("open");
}

function toggleKeyVisibility() {
  const input = document.getElementById("apiKeyInput");
  const btn = document.getElementById("toggleVisBtn");
  if (input.type === "password") {
    input.type = "text";
    btn.textContent = "Hide";
  } else {
    input.type = "password";
    btn.textContent = "Show";
  }
}

function clearSavedKey() {
  try { localStorage.removeItem(STORAGE_KEY); } catch (_) {}
  document.getElementById("apiKeyInput").value = "";
  const st = document.getElementById("apiStatus");
  st.textContent = "API key removed.";
  st.className = "api-status";
  refreshKeyIndicator();
}

async function verifyAndSaveKey() {
  const key = document.getElementById("apiKeyInput").value.trim();
  const st = document.getElementById("apiStatus");
  const btn = document.getElementById("verifySaveBtn");

  if (!key) {
    st.textContent = "Please paste a Google API Key first.";
    st.className = "api-status err";
    return;
  }

  btn.disabled = true;
  btn.textContent = "Checking...";
  st.textContent = "Testing API key with Google Gemini...";
  st.className = "api-status";

  try {
    const res = await fetch("/api/verify-key", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_key: key }),
    });
    const data = await res.json();
    if (res.ok && data.valid) {
      try { localStorage.setItem(STORAGE_KEY, key); } catch (_) {}
      st.textContent = data.message || "API key verified and saved!";
      st.className = "api-status ok";
      refreshKeyIndicator();
      setTimeout(closeSettings, 750);
    } else {
      st.textContent = data.message || "API key check failed.";
      st.className = "api-status err";
    }
  } catch (_) {
    st.textContent = "Could not reach server to verify key.";
    st.className = "api-status err";
  } finally {
    btn.disabled = false;
    btn.textContent = "Submit";
  }
}

function stepField(id, delta) {
  const el = document.getElementById(id);
  const minVal = parseInt(el.min || "0", 10);
  const maxVal = parseInt(el.max || "9999", 10);
  const current = parseInt(el.value || minVal, 10);
  const next = Math.min(maxVal, Math.max(minVal, current + delta));
  el.value = next;
  syncBounds(id);
}

function syncBounds(changedId = "") {
  const minEl = document.getElementById("minSecInput");
  const maxEl = document.getElementById("maxSecInput");
  let minV = parseInt(minEl.value || "20", 10);
  let maxV = parseInt(maxEl.value || "60", 10);
  if (minV >= maxV) {
    if (changedId === "minSecInput") {
      maxV = Math.min(300, minV + 5);
      maxEl.value = maxV;
    } else {
      minV = Math.max(5, maxV - 5);
      minEl.value = minV;
    }
  }
}

function setMuteProfanity(enabled) {
  muteProfanity = !!enabled;
  document.getElementById("beepYesBtn").classList.toggle("active", muteProfanity);
  document.getElementById("beepNoBtn").classList.toggle("active", !muteProfanity);
}

function clearConsole() {
  localClearedAt = Date.now();
  document.getElementById("logBox").textContent = "";
}

function updateBadge(phase, running) {
  const pill = document.getElementById("statusPill");
  const label = document.getElementById("statusText");
  pill.className = "status-pill";
  if (running || phase === "generating") {
    pill.classList.add("running");
    label.textContent = "Processing";
  } else if (phase === "completed") {
    pill.classList.add("completed");
    label.textContent = "Completed";
  } else if (phase === "error") {
    pill.classList.add("error");
    label.textContent = "Error";
  } else {
    label.textContent = "Idle";
  }
}

function renderClips(clips, cleanedUp, zipUrl, zipName) {
  const wrap = document.getElementById("clipsList");
  if (!clips || !clips.length) {
    wrap.innerHTML = cleanedUp
      ? '<div class="empty-note">Temporary server files were cleaned up for this job.</div>'
      : "";
    return;
  }

  let html = "";
  if (zipUrl) {
    html += `
      <div class="clip-row">
        <div class="clip-meta">
          <span class="clip-title">${escapeHtml(zipName || "StreamClipper_Clips.zip")}</span>
          <span class="clip-sub">Extracts into StreamClipper folder on your device</span>
        </div>
        <a class="btn" href="${escapeHtml(zipUrl)}" download="${escapeHtml(zipName || "StreamClipper_Clips.zip")}">Download Folder (.zip)</a>
      </div>
    `;
  }

  html += clips.map((c) => `
    <div class="clip-row">
      <div class="clip-meta">
        <span class="clip-title" title="${escapeHtml(c.name)}">${escapeHtml(c.name)}</span>
        <span class="clip-sub">${escapeHtml(String(c.size_mb ?? 0))} MB</span>
      </div>
      <a class="btn" href="${escapeHtml(c.url)}" download="${escapeHtml(c.name)}">Download</a>
    </div>
  `).join("");

  wrap.innerHTML = html;
}

async function startPipeline() {
  const apiKey = getSavedApiKey();
  if (!apiKey) {
    openSettings("Paste and verify your Google API Key before generating clips.", true);
    return;
  }

  syncBounds();
  const startBtn = document.getElementById("startBtn");
  const validVideos = videoEntries.filter((v) => v.state !== "bad");

  startBtn.disabled = true;
  localClearedAt = 0;

  if (videoEntries.length > 0) {
    if (validVideos.length === 0 || (musicEntry && musicEntry.state === "bad")) {
      startBtn.disabled = false;
      alert("Please remove or replace any unsupported file before generating clips.");
      return;
    }

    const prevText = startBtn.textContent;
    startBtn.textContent = "Uploading...";
    const form = new FormData();
    for (const item of validVideos) form.append("videos", item.file, item.name || "uploaded_video.mp4");
    if (musicEntry) form.append("music", musicEntry.file, musicEntry.name || "background_music.mp3");
    form.append("api_key", apiKey);
    form.append("num_clips", document.getElementById("numClipsInput").value);
    form.append("min_sec", document.getElementById("minSecInput").value);
    form.append("max_sec", document.getElementById("maxSecInput").value);
    form.append("beep_words", String(muteProfanity));

    try {
      const response = await fetch("/api/upload", { method: "POST", body: form });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        setMediaUploadStatus("bad", "Upload failed");
        startBtn.disabled = false;
        alert(data.error || "Upload failed.");
      } else {
        if (data.job_id) setActiveJobId(data.job_id);
        setMediaUploadStatus("uploaded", "Uploaded");
        document.getElementById("logBox").textContent = "Upload verified. Starting clip job...";
      }
    } catch (_) {
      setMediaUploadStatus("bad", "Upload failed");
      startBtn.disabled = false;
      alert("Upload failed. Check your connection and try again.");
    } finally {
      startBtn.textContent = prevText;
    }
    return;
  }

  const payload = {
    source: document.getElementById("sourceInput").value,
    num_clips: document.getElementById("numClipsInput").value,
    min_sec: document.getElementById("minSecInput").value,
    max_sec: document.getElementById("maxSecInput").value,
    beep_words: muteProfanity,
    api_key: apiKey,
  };

  try {
    const res = await fetch("/api/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      startBtn.disabled = false;
      alert(data.error || "Could not start job.");
    } else if (data.job_id) {
      setActiveJobId(data.job_id);
    }
  } catch (_) {
    startBtn.disabled = false;
    alert("Network error.");
  }
}

async function pollStatus() {
  try {
    const q = activeJobId ? `?job_id=${encodeURIComponent(activeJobId)}` : "";
    const res = await fetch(`/api/status${q}`);
    const s = await res.json();

    if (s.job_id && !activeJobId) {
      setActiveJobId(s.job_id);
    }

    const busy = !!s.running;
    document.getElementById("startBtn").disabled = busy;
    updateBadge(s.phase || "idle", busy);

    document.getElementById("stepVal").textContent = s.current_step || (busy ? "Running..." : "Idle");
    document.getElementById("countVal").textContent = s.clips_created ?? 0;
    document.getElementById("jobIdVal").textContent = s.job_id || "None";
    document.getElementById("progFill").style.width = `${s.progress_pct ?? 0}%`;

    renderClips(s.clips || [], !!s.cleaned_up, s.zip_url || "", s.zip_name || "StreamClipper_Clips.zip");

    const logBox = document.getElementById("logBox");
    if (Date.now() - localClearedAt > 1500) {
      const nearBottom = logBox.scrollTop + logBox.clientHeight >= logBox.scrollHeight - 28;
      logBox.textContent = (s.logs && s.logs.length) ? s.logs.join("\\n") : "No logs yet.";
      if (nearBottom) {
        logBox.scrollTop = logBox.scrollHeight;
      }
    }
  } catch (_) {}
}

renderMediaState();
refreshKeyIndicator();
pollStatus();
setInterval(pollStatus, 1200);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args) -> None:
        return

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_zip_download(self, job_id: str) -> None:
        clean_id = Path(job_id).name
        if clean_id != job_id or not clean_id:
            self._send_json(400, {"error": "Invalid job ID"})
            return

        job_root = (BASE_OUT_DIR / "jobs" / clean_id).resolve()
        bundle_root = (job_root / "StreamClipper").resolve()
        if not bundle_root.is_dir():
            self._send_json(404, {"error": "Clips folder not found or expired"})
            return

        files = [p for p in bundle_root.rglob("*") if p.is_file()]
        if not files:
            self._send_json(404, {"error": "No clip files available to zip"})
            return

        with JOBS_LOCK:
            job = JOBS.get(clean_id) or {}
            zip_name = _sanitize_folder_name(
                Path(job.get("zip_name", "StreamClipper_Clips.zip")).stem,
                "StreamClipper_Clips",
            ) + ".zip"

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for fpath in sorted(files):
                arcname = Path("StreamClipper") / fpath.relative_to(bundle_root)
                zf.write(fpath, arcname.as_posix())
        raw_zip = buf.getvalue()

        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(raw_zip)))
        self.send_header("Content-Disposition", f'attachment; filename="{zip_name}"')
        self.end_headers()
        self.wfile.write(raw_zip)

    def _serve_clip_download(self, job_id: str, rel_path: str) -> None:
        clean_id = Path(job_id).name
        if clean_id != job_id or not clean_id:
            self._send_json(400, {"error": "Invalid job ID"})
            return

        job_root = (BASE_OUT_DIR / "jobs" / clean_id).resolve()
        candidate = (job_root / unquote(rel_path)).resolve()

        if not str(candidate).startswith(str(job_root) + os.sep):
            self._send_json(403, {"error": "Forbidden"})
            return
        if not candidate.is_file() or candidate.suffix.lower() != ".mp4":
            self._send_json(404, {"error": "Clip not found or expired"})
            return

        file_size = candidate.stat().st_size
        range_header = self.headers.get("Range")
        start = 0
        end = file_size - 1
        status_code = 200

        if range_header and range_header.startswith("bytes="):
            part = range_header[6:].split(",", 1)[0].strip()
            s_txt, _, e_txt = part.partition("-")
            try:
                if s_txt:
                    start = max(0, int(s_txt))
                if e_txt:
                    end = min(file_size - 1, int(e_txt))
                if not s_txt and e_txt:
                    suffix_len = min(file_size, int(e_txt))
                    start = max(0, file_size - suffix_len)
                    end = file_size - 1
                if start > end or start >= file_size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{file_size}")
                    self.end_headers()
                    return
                status_code = 206
            except ValueError:
                start, end, status_code = 0, file_size - 1, 200

        content_len = end - start + 1
        self.send_response(status_code)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(content_len))
        self.send_header("Content-Disposition", f'attachment; filename="{candidate.name}"')
        if status_code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.end_headers()

        with candidate.open("rb") as fh:
            fh.seek(start)
            left = content_len
            while left > 0:
                chunk = fh.read(min(262144, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            raw = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        elif parsed.path == "/api/status":
            qs = parse_qs(parsed.query)
            jid = (qs.get("job_id") or [""])[0].strip() or None
            self._send_json(200, _get_job_snapshot(jid))
        elif parsed.path.startswith("/api/download-zip/"):
            job_id = parsed.path[len("/api/download-zip/"):].strip("/")
            if not job_id or "/" in job_id:
                self._send_json(400, {"error": "Invalid zip download path"})
                return
            self._serve_zip_download(job_id)
        elif parsed.path.startswith("/api/download/"):
            rest = parsed.path[len("/api/download/"):]
            job_id, sep, rel_path = rest.partition("/")
            if not sep or not rel_path:
                self._send_json(400, {"error": "Invalid download path"})
                return
            self._serve_clip_download(job_id, rel_path)
        else:
            self._send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path == "/api/upload":
            length = int(self.headers.get("Content-Length", "0"))
            max_upload = 2 * 1024 * 1024 * 1024
            if length <= 0 or length > max_upload:
                self._send_json(400, {"error": "Upload is empty or exceeds the 2 GB limit."})
                return
            content_type = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in content_type.lower():
                self._send_json(400, {"error": "Please use the upload form to select files."})
                return

            job_id = uuid.uuid4().hex[:12]
            temp_folder = BASE_OUT_DIR / ".uploads" / job_id
            spool_path = temp_folder / "multipart_payload.bin"
            try:
                temp_folder.mkdir(parents=True, exist_ok=True)
                remaining = length
                with spool_path.open("wb") as out_f:
                    out_f.write(b"Content-Type: " + content_type.encode("utf-8") + b"\r\nMIME-Version: 1.0\r\n\r\n")
                    while remaining > 0:
                        chunk = self.rfile.read(min(1048576, remaining))
                        if not chunk:
                            break
                        out_f.write(chunk)
                        remaining -= len(chunk)

                with spool_path.open("rb") as in_f:
                    message = BytesParser(policy=default).parse(in_f)
                spool_path.unlink(missing_ok=True)

                fields: dict[str, str] = {}
                video_paths: list[str] = []
                uploaded_video_names: list[str] = []
                music_path: str | None = None

                for part in message.iter_parts():
                    field_name = part.get_param("name", header="content-disposition")
                    filename = part.get_filename()
                    payload = part.get_payload(decode=True) or b""
                    if filename:
                        safe_filename = Path(filename.replace("\\", "/")).name
                        suffix = Path(safe_filename).suffix.lower()
                        if field_name == "videos":
                            allowed = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".wmv", ".3gp"}
                            ext = suffix if suffix in allowed else ".mp4"
                            destination = temp_folder / (uuid.uuid4().hex + ext)
                            destination.write_bytes(payload)
                            video_paths.append(str(destination))
                            uploaded_video_names.append(Path(safe_filename).stem or "uploaded_video")
                        elif field_name == "music":
                            allowed = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
                            ext = suffix if suffix in allowed else ".mp3"
                            destination = temp_folder / ("background_music" + ext)
                            destination.write_bytes(payload)
                            music_path = str(destination)
                    elif field_name:
                        fields[field_name] = payload.decode("utf-8", errors="replace")

                if not fields.get("api_key", "").strip():
                    shutil.rmtree(temp_folder, ignore_errors=True)
                    self._send_json(400, {"error": "Please set and verify your Gemini API key first."})
                    return
                if not video_paths:
                    shutil.rmtree(temp_folder, ignore_errors=True)
                    self._send_json(400, {"error": "No video files were selected."})
                    return
                if int(fields.get("min_sec", 20)) <= 0 or int(fields.get("max_sec", 60)) < int(fields.get("min_sec", 20)):
                    shutil.rmtree(temp_folder, ignore_errors=True)
                    self._send_json(400, {"error": "Clip duration settings are invalid."})
                    return

                video_folder = _infer_video_folder_name("", uploaded_video_names)
                job_out = BASE_OUT_DIR / "jobs" / job_id
                _register_job(job_id, job_out, video_folder)
                threading.Thread(
                    target=run_upload_worker,
                    args=(job_id, fields, video_paths, music_path, temp_folder),
                    daemon=True,
                ).start()
                self._send_json(200, {"started": True, "job_id": job_id, "videos": len(video_paths), "music_added": bool(music_path)})
            except Exception as exc:  # noqa: BLE001
                spool_path.unlink(missing_ok=True)
                shutil.rmtree(temp_folder, ignore_errors=True)
                self._send_json(400, {"error": f"Upload error: {exc}"})
            return

        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length) if length > 0 else b"{}"
        try:
            data = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "Invalid JSON"})
            return

        if parsed.path == "/api/verify-key":
            valid, msg = verify_gemini_api_key(str(data.get("api_key", "")))
            self._send_json(200 if valid else 400, {"valid": valid, "message": msg})
        elif parsed.path == "/api/start":
            if not str(data.get("api_key", "")).strip():
                self._send_json(400, {"error": "Please set your Google API Key in Settings first."})
                return
            job_id = uuid.uuid4().hex[:12]
            video_folder = _infer_video_folder_name(str(data.get("source", "")))
            job_out = BASE_OUT_DIR / "jobs" / job_id
            _register_job(job_id, job_out, video_folder)
            threading.Thread(target=run_clip_worker, args=(job_id, data), daemon=True).start()
            self._send_json(200, {"started": True, "job_id": job_id})
        else:
            self._send_json(404, {"error": "Not found"})


def main() -> int:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"StreamClipper Studio running at http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())