"""Paths, logging, subprocess and media helpers shared by every module."""

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np

os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")  # hide OpenCV's harmless DNN warnings

IS_WIN = os.name == "nt"
IS_MAC = os.sys.platform == "darwin"

DATA = Path(os.environ.get("STREAMCLIPS_HOME", str(Path.home() / "StreamClips")))
CACHE = DATA / ".cache"
ENV_DIR = Path.home() / ".streamclips"
MODELS = ENV_DIR / "models"

_print_lock = threading.Lock()
_thread = threading.local()
_t0 = time.time()

ICONS = {"info": " ", "step": ">", "good": "+", "warn": "!", "bad": "x"}


class Problem(RuntimeError):
    """An error worth showing to the user as-is."""


def tag(name: str) -> None:
    """Label every log line printed by this thread (one thread per stream)."""
    _thread.tag = name


def say(msg: str, kind: str = "info") -> None:
    elapsed = int(time.time() - _t0)
    label = getattr(_thread, "tag", "")
    prefix = f"[{elapsed // 60:02d}:{elapsed % 60:02d}] {ICONS.get(kind, ' ')} "
    with _print_lock:
        print(prefix + (f"{label}: " if label else "") + msg, flush=True)


def run(
    cmd: list,
    timeout: Optional[float] = None,
    cwd: Optional[str] = None,
    stdin: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
    quiet_fail: bool = False,
) -> Optional[str]:
    """Run a command and return its stdout as text. Raises Problem on failure."""
    try:
        proc = subprocess.run(
            [str(c) for c in cmd],
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=cwd,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise Problem(f"{Path(str(cmd[0])).name} took longer than {timeout}s") from exc
    except FileNotFoundError as exc:
        raise Problem(f"{cmd[0]} is not installed") from exc
    if proc.returncode != 0:
        if quiet_fail:
            return None
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise Problem(f"{Path(str(cmd[0])).name} failed: " + " | ".join(detail[-6:])[:900])
    return proc.stdout


def need(binary: str, fix: str) -> None:
    if not shutil.which(binary):
        raise Problem(f"'{binary}' was not found. {fix}")


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, data) -> None:
    target = Path(path)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, target)


def decode(
    path,
    rate: int = 16000,
    start: Optional[float] = None,
    length: Optional[float] = None,
) -> np.ndarray:
    """Decode audio from any media file to mono float32 samples (via ffmpeg, in memory)."""
    cmd = ["ffmpeg", "-nostdin", "-v", "error"]
    if start:
        cmd += ["-ss", f"{max(0.0, float(start)):.3f}"]
    cmd += ["-i", str(path)]
    if length:
        cmd += ["-t", f"{float(length):.3f}"]
    cmd += ["-map", "0:a:0", "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise Problem(
            "ffmpeg could not read the audio: "
            + proc.stderr.decode("utf-8", "replace").strip()[-300:]
        )
    return np.frombuffer(proc.stdout, dtype="<i2").astype(np.float32) / 32768.0


def probe(path) -> dict:
    """width, height, fps, duration of a media file (width 0 for audio-only)."""
    raw = run([
        "ffprobe",
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(path),
    ])
    data = json.loads(raw or "{}")
    video = next(
        (s for s in data.get("streams", []) if isinstance(s, dict) and s.get("codec_type") == "video"),
        None,
    )
    fps = 30.0
    if video:
        num, _, den = (str(video.get("avg_frame_rate") or "30/1")).partition("/")
        try:
            fps = float(num) / float(den or 1) or 30.0
        except (ValueError, ZeroDivisionError):
            fps = 30.0
    fmt = data.get("format") if isinstance(data.get("format"), dict) else {}
    return {
        "width": int(video["width"]) if video and video.get("width") else 0,
        "height": int(video["height"]) if video and video.get("height") else 0,
        "fps": fps,
        "duration": float(fmt.get("duration") or 0.0),
    }


def clock(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"


def parse_clock(text: str) -> float:
    total = 0.0
    for piece in text.strip().split(":"):
        total = total * 60.0 + float(piece or 0.0)
    return total


def human(seconds: float) -> str:
    s = int(round(seconds))
    if s < 90:
        return f"{s}s"
    h, m = divmod(s // 60, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def slug(text: Optional[str], limit: int = 40) -> str:
    cleaned = re.sub(r"[^\w\s-]", "", (text or "").lower(), flags=re.UNICODE)
    cleaned = re.sub(r"[\s_-]+", "-", cleaned).strip("-")
    ascii_only = cleaned.encode("ascii", "ignore").decode() or "clip"
    return ascii_only[:limit].strip("-") or "clip"


def even(x: float) -> int:
    val = int(round(x))
    return val - (val % 2)


def keywords_for(topic: Optional[str], extra: Optional[str] = None) -> list[str]:
    """Words a title must contain (any one) to count as being about the topic."""
    words: set[str] = set()
    if extra:
        words |= {w.strip().lower() for w in extra.split(",") if w.strip()}
    if topic:
        t = topic.lower().strip()
        words |= {t, t.replace(" ", "")}
    return sorted(words)


FONT_FILES = {
    "bold": [
        "/System/Library/Fonts/Supplemental/Arial Black.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        "C:/Windows/Fonts/ariblk.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/segoeuib.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ],
    "emoji": [
        "/System/Library/Fonts/Apple Color Emoji.ttc",
        "C:/Windows/Fonts/seguiemj.ttf",
        "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
    ],
}


def font_file(kind: str, override: Optional[str] = None) -> Optional[str]:
    for candidate in ([override] if override else []) + FONT_FILES.get(kind, []):
        if candidate and Path(candidate).exists():
            return candidate
    return None


class Stopwatch:
    def __init__(self) -> None:
        self.t0 = time.time()
        self.parts: dict[str, float] = {}

    def lap(self, name: str):
        watch = self

        class _Lap:
            def __init__(self) -> None:
                self.t = 0.0

            def __enter__(self) -> None:
                self.t = time.time()

            def __exit__(self, *_) -> None:
                watch.parts[name] = watch.parts.get(name, 0.0) + (time.time() - self.t)

        return _Lap()

    def total(self) -> float:
        return time.time() - self.t0