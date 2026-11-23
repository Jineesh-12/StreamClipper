import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8765"))
PROJECT_DIR = Path(__file__).resolve().parent
SCRIPT_PATH = PROJECT_DIR / "scripts" / "streamclips.py"
DEFAULT_CLIPS_DIR = Path.home() / "StreamClips"

VENV_PYTHON = Path.home() / ".streamclips" / "venv" / "Scripts" / "python.exe"
PYTHON_EXE = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable

# Automatic studio settings (hidden from the web UI)
AUTO_TOPIC = "Highlights"
AUTO_LAYOUT = "auto"
AUTO_FOCUS = ""
AUTO_FX_ENABLED = True

STATE_LOCK = threading.Lock()
JOB_STATE = {
    "running": False,
    "phase": "idle",  # idle | generating | completed | error
    "logs": [],
    "clips_created": 0,
    "current_step": "Idle",
    "progress_pct": 0,
    "output_folder": str(DEFAULT_CLIPS_DIR),
}


def append_log(message: str) -> None:
    stamp = time.strftime("%H:%M:%S")
    line = f"[{stamp}] {message}"
    with STATE_LOCK:
        JOB_STATE["logs"].append(line)
        if len(JOB_STATE["logs"]) > 600:
            JOB_STATE["logs"] = JOB_STATE["logs"][-600:]


def set_status(**kwargs) -> None:
    with STATE_LOCK:
        JOB_STATE.update(kwargs)


def _same_drive_cache_dir(out_folder: Path) -> Path:
    anchor = out_folder.anchor or str(out_folder.root or "/")
    cache_dir = Path(anchor) / "StreamClipsDownloads"
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
        except Exception as exc:  # noqa: BLE001 - inspect API error details
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


def run_clip_worker(form: dict) -> None:
    source = str(form.get("source", "")).strip()
    user_api_key = str(form.get("api_key", "")).strip()
    num_clips = str(form.get("num_clips", "6")).strip() or "6"
    min_sec = str(form.get("min_sec", "20")).strip() or "20"
    max_sec = str(form.get("max_sec", "60")).strip() or "60"
    beep_words = bool(form.get("beep_words", True))
    auto_fx = AUTO_FX_ENABLED
    topic = AUTO_TOPIC
    layout = AUTO_LAYOUT
    focus = AUTO_FOCUS

    raw_folder = str(form.get("output_folder", "")).strip()
    out_folder = Path(raw_folder).expanduser().resolve() if raw_folder else DEFAULT_CLIPS_DIR.resolve()
    out_folder.mkdir(parents=True, exist_ok=True)
    cache_home = _same_drive_cache_dir(out_folder)

    set_status(
        running=True,
        phase="generating",
        logs=[],
        clips_created=0,
        current_step="Starting...",
        progress_pct=8,
        output_folder=str(out_folder),
    )

    try:
        if not user_api_key:
            append_log("Error: No Google API Key set. Click the Settings gear icon at the top right to add your key.")
            set_status(running=False, phase="error", current_step="API key required", progress_pct=0)
            return

        if not source:
            append_log("Error: Click Upload to select video files or paste a video URL/path first.")
            set_status(running=False, phase="error", current_step="Missing input", progress_pct=0)
            return

        if not SCRIPT_PATH.exists():
            append_log(f"Error: Missing {SCRIPT_PATH}")
            set_status(running=False, phase="error", current_step="Error", progress_pct=0)
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
            topic,
            "--clips",
            num_clips,
            "--layout",
            layout,
            "--min",
            min_sec,
            "--max",
            max_sec,
            "--language",
            "en",
            "--fast",
            "--repick",
        ]
        if source.startswith(("http://", "https://")):
            cmd += ["--cookies", "firefox"]
        if focus:
            cmd += ["--focus", focus]

        run_env = os.environ.copy()
        run_env["STREAMCLIPS_NO_ENV_FALLBACK"] = "1"
        run_env["GEMINI_API_KEY"] = user_api_key
        run_env["GOOGLE_API_KEY"] = user_api_key
        run_env["STREAMCLIPS_BLEEP"] = "1" if beep_words else "0"
        run_env["STREAMCLIPS_AUTO_FX"] = "1" if auto_fx else "0"
        run_env["STREAMCLIPS_OUT_DIR"] = str(out_folder)
        run_env["STREAMCLIPS_HOME"] = str(cache_home)
        run_env["PYTHONIOENCODING"] = "utf-8"
        run_env["PYTHONUTF8"] = "1"

        append_log(f"Output folder: {out_folder}")
        append_log(f"Download & cache folder: {cache_home}")
        append_log(f"Starting job ({num_clips} clips, {min_sec}s-{max_sec}s)...")

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
            append_log(clean)
            low = clean.lower()
            if "downloading" in low or "audio" in low:
                set_status(current_step="Downloading...", progress_pct=25)
            elif "listening" in low or "heard " in low or "whisper" in low:
                set_status(current_step="Transcribing...", progress_pct=50)
            elif "picking" in low or "picked " in low:
                set_status(current_step="Selecting moments...", progress_pct=72)
            elif "clip " in low and "saved " in low:
                with STATE_LOCK:
                    JOB_STATE["clips_created"] += 1
                set_status(current_step="Rendering clips...", progress_pct=88)

        rc = proc.wait()
        mp4_count = len(list(out_folder.glob("*.mp4")))
        txt_count = len(list(out_folder.glob("*.txt")))

        if rc == 0:
            append_log(f"Done: {mp4_count} .mp4 and {txt_count} .txt in {out_folder}")
            set_status(
                running=False,
                phase="completed",
                current_step="Completed",
                progress_pct=100,
                clips_created=mp4_count,
            )
        else:
            append_log(f"Exited with code {rc}.")
            set_status(running=False, phase="error", current_step="Error", progress_pct=0)

    except Exception as err:  # noqa: BLE001 - surface worker errors cleanly to UI logs
        append_log(f"Error: {err}")
        set_status(running=False, phase="error", current_step="Error", progress_pct=0)


def run_upload_worker(form: dict, video_paths: list[str], music_path: str | None, temp_folder: Path) -> None:
    """Process uploaded local videos using the existing StreamClipper engine."""
    try:
        from scripts.upload_processor import process_uploads

        output_folder = str(form.get("output_folder", "")).strip() or str(DEFAULT_CLIPS_DIR)
        set_status(
            running=True,
            phase="generating",
            logs=[],
            clips_created=0,
            current_step="Preparing uploaded videos...",
            progress_pct=5,
            output_folder=output_folder,
        )
        count = process_uploads(
            video_paths=video_paths,
            music_path=music_path,
            output_folder=output_folder,
            api_key=str(form.get("api_key", "")).strip(),
            num_clips=int(form.get("num_clips", 6)),
            min_sec=int(form.get("min_sec", 20)),
            max_sec=int(form.get("max_sec", 60)),
            beep_words=str(form.get("beep_words", "true")).lower() == "true",
            log=append_log,
            status=set_status,
        )
        set_status(
            running=False,
            phase="completed",
            current_step="Completed",
            progress_pct=100,
            clips_created=count,
        )
    except Exception as exc:  # noqa: BLE001
        append_log(f"Upload error: {exc}")
        set_status(running=False, phase="error", current_step="Upload failed", progress_pct=0)
    finally:
        shutil.rmtree(temp_folder, ignore_errors=True)


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
    padding: 28px 18px 68px;
    position: relative;
    overflow-x: hidden;
    background:
      radial-gradient(ellipse 85% 48% at 8% 100%, rgba(109, 40, 217, 0.24) 0%, rgba(76, 29, 149, 0.08) 45%, transparent 72%),
      radial-gradient(ellipse 80% 45% at 92% 94%, rgba(29, 78, 216, 0.22) 0%, rgba(88, 28, 135, 0.07) 45%, transparent 74%),
      radial-gradient(ellipse 70% 38% at 50% 0%, rgba(91, 33, 182, 0.14) 0%, transparent 65%),
      linear-gradient(170deg, #050510 0%, #070718 52%, #04081a 100%);
  }

  /* Subtle, dimmed edge waves keeping the center dark */
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
    gap: 16px;
    flex-wrap: wrap;
    background: var(--card-bg);
    backdrop-filter: blur(18px);
    border: 1px solid var(--border-soft);
    border-radius: 22px;
    padding: 16px 22px;
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
    font-size: 1.18rem;
    font-weight: 700;
    letter-spacing: -0.015em;
  }

  .top-actions {
    display: inline-flex;
    align-items: center;
    gap: 10px;
  }

  .status-pill {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 7px 15px;
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
    box-shadow: 0 0 8px rgba(56, 189, 248, 0.14);
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
    height: 36px;
    padding: 0 14px;
    border-radius: 999px;
    background: rgba(7, 9, 24, 0.88);
    border: 1px solid rgba(139, 92, 246, 0.34);
    color: #e2e8f0;
    font-size: 0.8rem;
    font-weight: 600;
    cursor: pointer;
    box-shadow: 0 0 8px rgba(124, 58, 237, 0.10);
    transition: border-color 0.16s ease, box-shadow 0.16s ease, transform 0.12s ease;
  }

  .settings-btn:hover {
    border-color: rgba(56, 189, 248, 0.55);
    box-shadow: 0 0 10px rgba(56, 189, 248, 0.18);
    transform: translateY(-1px);
  }

  .key-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--err);
  }

  .key-dot.ready {
    background: var(--ok);
    box-shadow: 0 0 5px rgba(52, 211, 153, 0.55);
  }

  .card {
    background: var(--card-bg);
    backdrop-filter: blur(18px);
    border: 1px solid var(--border-soft);
    border-radius: 22px;
    padding: 22px;
    box-shadow: 0 18px 42px rgba(2, 3, 12, 0.72), 0 0 14px rgba(124, 58, 237, 0.06);
  }

  .card-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 16px;
  }

  .card-title {
    font-size: 0.96rem;
    font-weight: 700;
    color: #f8fafc;
  }

  .form-grid {
    display: grid;
    grid-template-columns: 1.5fr 1fr;
    gap: 14px;
  }

  @media (max-width: 760px) {
    .form-grid { grid-template-columns: 1fr; }
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
    padding: 11px 14px;
    font-size: 0.9rem;
    outline: none;
    transition: border-color 0.18s ease, box-shadow 0.18s ease;
  }

  input[type="text"]:focus,
  input[type="password"]:focus {
    border-color: rgba(56, 189, 248, 0.55);
    box-shadow: 0 0 0 2px rgba(56, 189, 248, 0.12), 0 0 10px rgba(124, 58, 237, 0.16);
  }

  /* Master "Upload" toggle row */
  .upload-launch-row {
    margin-top: 14px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    flex-wrap: wrap;
  }

  /* Pill upload bar & status cards (hidden until "Upload" is clicked) */
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
    padding: 8px 16px;
    border-radius: 999px;
    background: rgba(9, 12, 30, 0.92);
    border: 1px solid rgba(139, 92, 246, 0.38);
    color: #e2e8f0;
    font-size: 0.83rem;
    font-weight: 600;
    cursor: pointer;
    box-shadow: 0 0 8px rgba(124, 58, 237, 0.10);
    transition: border-color 0.16s ease, box-shadow 0.16s ease, transform 0.12s ease, background 0.16s ease;
  }

  .pill-action:hover {
    border-color: rgba(56, 189, 248, 0.62);
    background: rgba(14, 20, 46, 0.96);
    box-shadow: 0 0 10px rgba(56, 189, 248, 0.18);
    transform: translateY(-1px);
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
    max-height: 112px;
    overflow-y: auto;
  }

  .file-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    font-size: 0.78rem;
    padding: 5px 8px;
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
    padding: 2px 0;
  }

  /* Dimmed, softer purple-to-blue border pill stepper bar */
  .neon-stepper-bar {
    margin-top: 16px;
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    align-items: center;
    border-radius: 22px;
    padding: 14px 8px;
    background: rgba(6, 8, 22, 0.78);
    border: 1px solid transparent;
    background-clip: padding-box;
    position: relative;
    box-shadow:
      0 0 12px rgba(124, 58, 237, 0.10),
      0 0 14px rgba(14, 165, 233, 0.08),
      inset 0 0 14px rgba(5, 6, 18, 0.85);
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

  @media (min-width: 821px) {
    .stepper-cell:not(:last-child)::after {
      content: "";
      position: absolute;
      right: 0;
      top: 12%;
      bottom: 12%;
      width: 1px;
      background: linear-gradient(180deg, transparent, rgba(139, 92, 246, 0.28), transparent);
    }
  }

  .stepper-heading {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    font-size: 0.92rem;
    font-weight: 700;
    color: #f1f5f9;
    letter-spacing: -0.01em;
  }

  .stepper-icon {
    width: 18px;
    height: 18px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    color: rgba(217, 70, 239, 0.82);
  }

  .stepper-cell:nth-child(2) .stepper-icon { color: rgba(56, 189, 248, 0.82); }
  .stepper-cell:nth-child(3) .stepper-icon { color: rgba(168, 85, 247, 0.85); }

  .pill-track {
    width: 100%;
    max-width: 245px;
    height: 46px;
    border-radius: 999px;
    background: rgba(5, 7, 19, 0.92);
    border: 1px solid rgba(139, 92, 246, 0.36);
    box-shadow:
      0 0 8px rgba(124, 58, 237, 0.10),
      inset 0 0 10px rgba(3, 5, 14, 0.9);
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
    line-height: 1;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    box-shadow: 0 0 6px rgba(168, 85, 247, 0.14);
    transition: transform 0.1s ease, box-shadow 0.16s ease, border-color 0.16s ease;
    user-select: none;
  }

  .pill-btn.plus-btn {
    border-color: rgba(56, 189, 248, 0.46);
    background: rgba(9, 22, 44, 0.78);
    box-shadow: 0 0 6px rgba(56, 189, 248, 0.14);
  }

  .pill-btn:hover {
    transform: scale(1.03);
    border-color: rgba(192, 132, 252, 0.7);
    box-shadow: 0 0 9px rgba(168, 85, 247, 0.25);
  }

  .pill-btn.plus-btn:hover {
    border-color: rgba(56, 189, 248, 0.75);
    box-shadow: 0 0 9px rgba(56, 189, 248, 0.26);
  }

  .pill-btn:active {
    transform: scale(0.95);
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
    padding: 6px 16px;
    border-radius: 999px;
    cursor: pointer;
    transition: all 0.15s ease;
  }

  .yn-btn.active {
    color: #ffffff;
    background: linear-gradient(135deg, rgba(124, 58, 237, 0.36), rgba(14, 165, 233, 0.28));
    border-color: rgba(168, 85, 247, 0.48);
    box-shadow: 0 0 7px rgba(124, 58, 237, 0.16);
  }

  .btn {
    background: linear-gradient(135deg, rgba(147, 51, 234, 0.88), rgba(79, 70, 229, 0.88) 55%, rgba(2, 132, 199, 0.88));
    color: #ffffff;
    border: 1px solid rgba(255, 255, 255, 0.18);
    border-radius: 999px;
    padding: 11px 24px;
    font-size: 0.88rem;
    font-weight: 700;
    cursor: pointer;
    box-shadow: 0 0 12px rgba(124, 58, 237, 0.22);
    transition: transform 0.14s ease, box-shadow 0.16s ease, opacity 0.16s ease;
  }

  .btn:hover:not(:disabled) {
    transform: translateY(-1px);
    box-shadow: 0 0 16px rgba(14, 165, 233, 0.28);
  }

  .btn:disabled {
    opacity: 0.45;
    cursor: not-allowed;
    box-shadow: none;
  }

  .btn-ghost {
    background: rgba(7, 9, 24, 0.82);
    color: var(--muted);
    border: 1px solid var(--border-soft);
    border-radius: 999px;
    padding: 6px 14px;
    font-size: 0.78rem;
    font-weight: 600;
    cursor: pointer;
  }

  .btn-ghost:hover {
    color: #ffffff;
    border-color: rgba(56, 189, 248, 0.5);
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
    font-size: 1rem;
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

  pre.console {
    background: rgba(3, 4, 14, 0.92);
    border: 1px solid var(--border-soft);
    border-radius: 16px;
    padding: 14px 16px;
    height: 270px;
    overflow-y: auto;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 0.8rem;
    line-height: 1.6;
    color: #cbd5e1;
    white-space: pre-wrap;
  }

  /* Settings Modal */
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
    box-shadow: 0 24px 60px rgba(0, 0, 0, 0.8), 0 0 18px rgba(124, 58, 237, 0.16);
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

  /* Fixed bottom-right animated signature badge ("Jineesh") */
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
    box-shadow: 0 6px 18px rgba(0, 0, 0, 0.55), 0 0 10px rgba(124, 58, 237, 0.10);
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
    text-shadow: 0 0 8px rgba(139, 92, 246, 0.22);
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
    0%, 100% {
      transform: translateY(0px);
      opacity: 0.86;
    }
    35% {
      transform: translateY(-3.5px);
      opacity: 1;
    }
    70% {
      transform: translateY(0.5px);
      opacity: 0.9;
    }
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
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <circle cx="12" cy="12" r="3"/>
          <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9c.26.6.77 1.02 1.42 1.15.19.04.39.06.58.06H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1Z"/>
        </svg>
        <span id="keyLabel">API Key</span>
      </button>
    </div>
  </header>

  <section class="card">
    <div class="card-head">
      <div class="card-title">New Clip Job</div>
    </div>

    <div class="form-grid">
      <div class="field">
        <label for="sourceInput">Video / Stream URL or File Path</label>
        <input id="sourceInput" type="text" placeholder="https://www.youtube.com/watch?v=..." />
      </div>
      <div class="field">
        <label for="folderInput">Output Folder</label>
        <input id="folderInput" type="text" value="C:\\Users\\tadvi\\StreamClips" />
      </div>
    </div>

    <input id="videoFiles" type="file" accept=".mp4,.mkv,.mov,.avi,.webm,.m4v,.wmv,video/*" multiple style="display:none;" onchange="handleFileSelection('video')" />
    <input id="musicFile" type="file" accept=".mp3,.wav,.m4a,.aac,.flac,.ogg,.opus,audio/*" style="display:none;" onchange="handleFileSelection('music')" />

    <div class="upload-launch-row">
      <button type="button" class="pill-action" id="uploadToggleBtn" onclick="toggleUploadPanel()" aria-expanded="false">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M12 16V4"/><path d="m7 9 5-5 5 5"/><path d="M4 20h16"/>
        </svg>
        <span>Upload</span>
      </button>
      <span class="media-summary" id="closedMediaBadge"></span>
    </div>

    <div class="media-bar" id="mediaBar">
      <div class="media-toolbar">
        <div class="pill-group">
          <button type="button" class="pill-action" id="pickVideoBtn" onclick="document.getElementById('videoFiles').click()">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="M12 16V4"/><path d="m7 9 5-5 5 5"/><path d="M4 20h16"/>
            </svg>
            <span>Upload video</span>
          </button>

          <button type="button" class="pill-action" id="pickMusicBtn" onclick="document.getElementById('musicFile').click()">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>
            </svg>
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
          <span class="stepper-icon" aria-hidden="true">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M4 11v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8"/>
              <path d="M4 11 2 6l16-3 2 5-16 3Z"/>
              <path d="m7 5 2 5"/><path d="m13 4 2 5"/>
            </svg>
          </span>
          <label for="numClipsInput" style="margin:0;color:inherit;font-size:inherit;font-weight:inherit;cursor:pointer;">Clips</label>
        </div>
        <div class="pill-track">
          <button type="button" class="pill-btn" aria-label="Decrease clips" onclick="stepField('numClipsInput', -1)">&minus;</button>
          <input id="numClipsInput" class="pill-val" type="number" value="6" min="1" max="30" onchange="syncBounds()" />
          <button type="button" class="pill-btn plus-btn" aria-label="Increase clips" onclick="stepField('numClipsInput', 1)">+</button>
        </div>
      </div>

      <div class="stepper-cell">
        <div class="stepper-heading">
          <span class="stepper-icon" aria-hidden="true">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="12" cy="13" r="8"/><path d="M12 9v4l2 2"/><path d="M10 2h4"/>
            </svg>
          </span>
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
          <span class="stepper-icon" aria-hidden="true">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
              <path d="M13 2 4 14h7l-1 8 10-12h-7l0-8Z"/>
            </svg>
          </span>
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
      <div class="card-title">Logs</div>
      <button class="btn-ghost" type="button" onclick="clearConsole()">Clear</button>
    </div>

    <div class="stats-grid">
      <div class="stat-box">
        <div class="stat-label">Stage</div>
        <div class="stat-val" id="stepVal">Idle</div>
      </div>
      <div class="stat-box">
        <div class="stat-label">Clips</div>
        <div class="stat-val" id="countVal" style="color: var(--ok);">0</div>
      </div>
      <div class="stat-box">
        <div class="stat-label">Folder</div>
        <div class="stat-val" id="folderVal" style="color: #cbd5e1; font-size: 0.88rem;">C:\\Users\\tadvi\\StreamClips</div>
      </div>
    </div>

    <div class="progress-wrap">
      <div class="progress-bar" id="progFill"></div>
    </div>

    <pre class="console" id="logBox">Ready.</pre>
  </section>
</div>

<!-- Fixed bottom-right animated signature -->
<div class="signature-badge" aria-label="Jineesh">
  <span class="sig-word" aria-hidden="true">
    <span class="sig-letter">J</span><span class="sig-letter">i</span><span class="sig-letter">n</span><span class="sig-letter">e</span><span class="sig-letter">e</span><span class="sig-letter">s</span><span class="sig-letter">h</span>
  </span>
</div>

<!-- Google API Key Settings Modal -->
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
const VIDEO_EXTS = [".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".wmv"];
const MUSIC_EXTS = [".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"];

let muteProfanity = true;
let localClearedAt = 0;
let videoEntries = [];
let musicEntry = null;

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
  if (!bytes || bytes <= 0) return "0 B";
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
      const valid = hasAllowedExt(file.name, VIDEO_EXTS) && file.size > 0;
      return {
        file,
        name: file.name,
        sizeLabel: formatSize(file.size),
        state: valid ? "ready" : "bad",
        label: valid ? `Ready (${formatSize(file.size)})` : (file.size <= 0 ? "Empty file" : "Unsupported"),
      };
    });
  } else {
    const file = (document.getElementById("musicFile").files || [])[0] || null;
    if (!file) {
      musicEntry = null;
    } else {
      const valid = hasAllowedExt(file.name, MUSIC_EXTS) && file.size > 0;
      musicEntry = {
        file,
        name: file.name,
        sizeLabel: formatSize(file.size),
        state: valid ? "ready" : "bad",
        label: valid ? `Ready (${formatSize(file.size)})` : (file.size <= 0 ? "Empty file" : "Unsupported"),
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
    summary.textContent = "Check highlighted file format/size before generating";
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
      alert("Please remove or replace any unsupported/empty file before generating clips.");
      return;
    }

    const prevText = startBtn.textContent;
    startBtn.textContent = "Uploading...";
    const form = new FormData();
    for (const item of validVideos) form.append("videos", item.file);
    if (musicEntry) form.append("music", musicEntry.file);
    form.append("api_key", apiKey);
    form.append("num_clips", document.getElementById("numClipsInput").value);
    form.append("min_sec", document.getElementById("minSecInput").value);
    form.append("max_sec", document.getElementById("maxSecInput").value);
    form.append("output_folder", document.getElementById("folderInput").value);
    form.append("beep_words", String(muteProfanity));

    try {
      const response = await fetch("/api/upload", { method: "POST", body: form });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        setMediaUploadStatus("bad", "Upload failed");
        startBtn.disabled = false;
        alert(data.error || "Upload failed.");
      } else {
        setMediaUploadStatus("uploaded", "Uploaded");
        document.getElementById("logBox").textContent = "Upload verified. Starting clip generation...";
      }
    } catch (_) {
      setMediaUploadStatus("bad", "Upload failed");
      startBtn.disabled = false;
      alert("Upload failed. Check that StreamClipper is running and try again.");
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
    output_folder: document.getElementById("folderInput").value,
    beep_words: muteProfanity,
    api_key: apiKey,
  };

  try {
    const res = await fetch("/api/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      startBtn.disabled = false;
      const err = await res.json().catch(() => ({}));
      alert(err.error || "Could not start job.");
    }
  } catch (_) {
    startBtn.disabled = false;
    alert("Network error.");
  }
}

async function pollStatus() {
  try {
    const res = await fetch("/api/status");
    const s = await res.json();

    const busy = !!s.running;
    document.getElementById("startBtn").disabled = busy;
    updateBadge(s.phase || "idle", busy);

    document.getElementById("stepVal").textContent = s.current_step || (busy ? "Running..." : "Idle");
    document.getElementById("countVal").textContent = s.clips_created ?? 0;
    document.getElementById("folderVal").textContent = s.output_folder || "";
    document.getElementById("progFill").style.width = `${s.progress_pct ?? 0}%`;

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
setInterval(pollStatus, 1000);
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
            with STATE_LOCK:
                snapshot = dict(JOB_STATE)
                snapshot["logs"] = list(JOB_STATE["logs"])
            self._send_json(200, snapshot)
        else:
            self._send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path == "/api/upload":
            length = int(self.headers.get("Content-Length", "0"))
            max_upload = 20 * 1024 * 1024 * 1024
            if length <= 0 or length > max_upload:
                self._send_json(400, {"error": "Upload is empty or exceeds the 20 GB limit."})
                return
            content_type = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in content_type.lower():
                self._send_json(400, {"error": "Please use the upload form to select files."})
                return

            temp_folder = DEFAULT_CLIPS_DIR / ".uploads" / uuid.uuid4().hex
            spool_path = temp_folder / "multipart_payload.bin"
            try:
                temp_folder.mkdir(parents=True, exist_ok=True)
                remaining = length
                with spool_path.open("wb") as out_f:
                    out_f.write(b"Content-Type: " + content_type.encode("utf-8") + b"\r\nMIME-Version: 1.0\r\n\r\n")
                    while remaining > 0:
                        chunk = self.rfile.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        out_f.write(chunk)
                        remaining -= len(chunk)

                with spool_path.open("rb") as in_f:
                    message = BytesParser(policy=default).parse(in_f)
                spool_path.unlink(missing_ok=True)

                fields: dict[str, str] = {}
                video_paths: list[str] = []
                music_path: str | None = None

                for part in message.iter_parts():
                    field_name = part.get_param("name", header="content-disposition")
                    filename = part.get_filename()
                    payload = part.get_payload(decode=True) or b""
                    if filename:
                        safe_filename = Path(filename.replace("\\", "/")).name
                        suffix = Path(safe_filename).suffix.lower()
                        if field_name == "videos":
                            allowed = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".wmv"}
                            if suffix not in allowed:
                                continue
                            destination = temp_folder / (uuid.uuid4().hex + suffix)
                            destination.write_bytes(payload)
                            video_paths.append(str(destination))
                        elif field_name == "music":
                            allowed = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
                            if suffix not in allowed:
                                continue
                            destination = temp_folder / ("background_music" + suffix)
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
                    self._send_json(400, {"error": "No supported video files were selected."})
                    return
                if int(fields.get("min_sec", 20)) <= 0 or int(fields.get("max_sec", 60)) < int(fields.get("min_sec", 20)):
                    shutil.rmtree(temp_folder, ignore_errors=True)
                    self._send_json(400, {"error": "Clip duration settings are invalid."})
                    return
                with STATE_LOCK:
                    if JOB_STATE["running"]:
                        shutil.rmtree(temp_folder, ignore_errors=True)
                        self._send_json(409, {"error": "Another clipping job is already running."})
                        return
                threading.Thread(
                    target=run_upload_worker,
                    args=(fields, video_paths, music_path, temp_folder),
                    daemon=True,
                ).start()
                self._send_json(200, {"started": True, "videos": len(video_paths), "music_added": bool(music_path)})
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
            with STATE_LOCK:
                if JOB_STATE["running"]:
                    self._send_json(409, {"error": "Pipeline is already running."})
                    return
            threading.Thread(target=run_clip_worker, args=(data,), daemon=True).start()
            self._send_json(200, {"started": True})
        else:
            self._send_json(404, {"error": "Not found"})


def main() -> int:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"StreamClipper Studio running at http://127.0.0.1:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())