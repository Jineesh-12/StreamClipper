import os
import re
import shutil
import subprocess
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLIP_SCRIPT = PROJECT_ROOT / "scripts" / "streamclips.py"


def _log(callback, message):
    if callback:
        callback(message)
    else:
        print(message, flush=True)


def _status(callback, **values):
    if callback:
        callback(**values)


def _find_python():
    """Use the same private Python environment as scripts/streamclips.py."""
    if os.name == "nt":
        candidate = Path.home() / ".streamclips" / "venv" / "Scripts" / "python.exe"
    else:
        candidate = Path.home() / ".streamclips" / "venv" / "bin" / "python"

    if not candidate.is_file():
        raise RuntimeError(
            "StreamClips Python environment was not found. "
            "Run the StreamClipper setup first."
        )
    return str(candidate)


def _probe_duration(media_path: Path) -> float:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return 0.0
    res = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(media_path),
        ],
        capture_output=True,
        text=True,
    )
    try:
        return max(0.0, float((res.stdout or "0").strip()))
    except ValueError:
        return 0.0


def _has_audio(video_path: Path) -> bool:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError(
            "FFprobe was not found. Install FFmpeg and make sure ffprobe is on PATH."
        )

    result = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=index",
            "-of", "csv=p=0",
            str(video_path),
        ],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _pick_best_music_offset(music_path: Path, clip_dur: float, clip_index: int) -> float:
    """Pick a high-energy section from a long music track for each clip."""
    ffmpeg = shutil.which("ffmpeg")
    total_dur = _probe_duration(music_path)
    if not ffmpeg or total_dur <= max(12.0, clip_dur + 2.0):
        return 0.0

    max_start = max(0.0, total_dur - clip_dur)
    num_samples = min(8, max(3, int(total_dur // max(15.0, clip_dur))))
    win_len = min(max(4.0, clip_dur), 25.0)
    scored: list[tuple[float, float]] = []

    for i in range(num_samples):
        start_t = (max_start * i) / max(1, num_samples - 1)
        proc = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-nostdin",
                "-ss", f"{start_t:.2f}",
                "-t", f"{win_len:.2f}",
                "-i", str(music_path),
                "-af", "volumedetect",
                "-f", "null", "-",
            ],
            capture_output=True,
            text=True,
            timeout=25,
        )
        match = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", proc.stderr or "")
        mean_db = float(match.group(1)) if match else -40.0
        scored.append((mean_db, start_t))

    scored.sort(key=lambda item: item[0], reverse=True)
    top_pool = scored[: min(3, len(scored))]
    return top_pool[(clip_index - 1) % len(top_pool)][1]


def _add_music(video_path: Path, music_path: Path, clip_index: int, log):
    """Mix the best part of background music cleanly under the clip's audio."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError(
            "FFmpeg was not found. Install FFmpeg and make sure ffmpeg is on PATH."
        )

    clip_dur = _probe_duration(video_path)
    music_dur = _probe_duration(music_path)
    temp_path = video_path.with_name(video_path.stem + "_music_temp.mp4")

    should_loop = music_dur > 0 and clip_dur > 0 and music_dur < clip_dur
    offset = 0.0 if should_loop else _pick_best_music_offset(music_path, max(5.0, clip_dur or 30.0), clip_index)
    fade_out_start = max(0.0, clip_dur - 1.2) if clip_dur > 2.0 else 0.0

    music_in = (
        ["-stream_loop", "-1", "-i", str(music_path)]
        if should_loop
        else (["-ss", f"{offset:.2f}", "-i", str(music_path)] if offset > 0 else ["-i", str(music_path)])
    )

    music_fx = (
        f"[1:a]volume=0.14,afade=t=in:st=0:d=0.8"
        + (f",afade=t=out:st={fade_out_start:.2f}:d=1.2" if fade_out_start > 0 else "")
        + "[music]"
    )

    if _has_audio(video_path):
        command = [
            ffmpeg, "-y", "-v", "error", "-nostdin",
            "-i", str(video_path),
            *music_in,
            "-filter_complex",
            f"[0:a]volume=1.0[original];{music_fx};"
            "[original][music]amix=inputs=2:duration=first:dropout_transition=2:normalize=0,"
            "alimiter=limit=0.95[aout]",
            "-map", "0:v:0",
            "-map", "[aout]",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "192k",
            "-shortest",
            str(temp_path),
        ]
    else:
        command = [
            ffmpeg, "-y", "-v", "error", "-nostdin",
            "-i", str(video_path),
            *music_in,
            "-filter_complex",
            f"{music_fx},alimiter=limit=0.95[aout]",
            "-map", "0:v:0",
            "-map", "[aout]",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "192k",
            "-shortest",
            str(temp_path),
        ]

    _log(log, f"Mixing background music ({offset:.1f}s) into: {video_path.name}")
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        temp_path.unlink(missing_ok=True)
        detail = (result.stderr or result.stdout or "Unknown FFmpeg error").strip()
        raise RuntimeError(f"Could not add music to {video_path.name}: {detail[-1800:]}")

    temp_path.replace(video_path)


def process_uploads(
    video_paths,
    music_path=None,
    output_folder=None,
    api_key="",
    num_clips=6,
    min_sec=20,
    max_sec=60,
    beep_words=True,
    log=None,
    status=None,
):
    """Generate clips from uploaded local videos. Returns the number of MP4 clips."""
    if not api_key or not str(api_key).strip():
        raise RuntimeError("Gemini API key is missing.")

    if not video_paths:
        raise RuntimeError("No video files were supplied.")

    if not CLIP_SCRIPT.is_file():
        raise RuntimeError(f"Clip engine was not found: {CLIP_SCRIPT}")

    if music_path and not Path(music_path).is_file():
        raise RuntimeError("The selected background music file could not be found.")

    output_root = Path(output_folder).expanduser() if output_folder else (
        Path.home() / "StreamClips"
    )
    output_root.mkdir(parents=True, exist_ok=True)

    upload_folder = output_root / (time.strftime("%Y-%m-%d") + "_Uploads")
    upload_folder.mkdir(parents=True, exist_ok=True)

    python_exe = _find_python()
    total_clips = 0

    for index, raw_video in enumerate(video_paths, start=1):
        video = Path(raw_video)
        if not video.is_file():
            _log(log, f"Skipping missing video: {video}")
            continue

        job_folder = upload_folder / f"{index:02d}_{video.stem}"
        job_folder.mkdir(parents=True, exist_ok=True)

        _log(log, f"Processing video {index}/{len(video_paths)}: {video.name}")
        _status(
            status,
            running=True,
            phase="generating",
            current_step=f"Processing {video.name}",
            progress_pct=min(10 + int((index - 1) * 70 / len(video_paths)), 80),
        )

        env = os.environ.copy()
        env["STREAMCLIPS_NO_ENV_FALLBACK"] = "1"
        env["GEMINI_API_KEY"] = str(api_key).strip()
        env["GOOGLE_API_KEY"] = str(api_key).strip()
        env["STREAMCLIPS_OUT_DIR"] = str(job_folder)
        env["STREAMCLIPS_HOME"] = str(output_root)
        env["STREAMCLIPS_BLEEP"] = "1" if beep_words else "0"
        env["STREAMCLIPS_AUTO_FX"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"

        command = [
            python_exe,
            "-u",
            str(CLIP_SCRIPT),
            "clip",
            str(video.resolve()),
            "--brain", "gemini-api",
            "--topic", video.stem,
            "--clips", str(int(num_clips)),
            "--layout", "auto",
            "--min", str(int(min_sec)),
            "--max", str(int(max_sec)),
            "--language", "en",
            "--fast",
            "--repick",
        ]

        _log(log, "Starting the existing StreamClipper engine...")
        result = subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        if result.stdout:
            for line in result.stdout.splitlines()[-80:]:
                _log(log, line)

        if result.returncode != 0:
            detail = (result.stderr or "The clip engine returned an error.").strip()
            raise RuntimeError(
                f"Could not process {video.name}.\n{detail[-2500:]}"
            )

        clips = sorted(
            p for p in job_folder.rglob("*.mp4")
            if p.is_file() and not p.name.endswith("_music_temp.mp4")
        )

        if music_path:
            for clip_idx, clip in enumerate(clips, start=1):
                _add_music(clip, Path(music_path), clip_idx, log)

        total_clips += len(clips)
        _log(log, f"Finished {video.name}: {len(clips)} clip(s) saved in {job_folder}")

    _log(log, f"All uploaded videos finished. Total clips: {total_clips}")
    return total_clips