"""Listen to the stream locally: Whisper transcript, a per-second volume curve and (on
YouTube replays) how fast chat moved. The audio is handled in fixed blocks aligned to
stream time, each cached on disk, so re-runs and --section runs reuse earlier work.
"""

import json
import os
import threading
from pathlib import Path
from typing import Any, Optional

import numpy as np

from .common import MODELS, Problem, decode, read_json, run, say, write_json

os.environ.setdefault("HF_HUB_VERBOSITY", "error")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

_models: dict[str, Any] = {}
_models_lock = threading.Lock()
RATE = 16000


def whisper(name: str) -> Any:
    """One shared model per size; num_workers lets parallel streams transcribe together."""
    with _models_lock:
        if name not in _models:
            from faster_whisper import WhisperModel  # type: ignore[import-not-found]

            say(f"loading Whisper '{name}'...", "step")
            _models[name] = WhisperModel(
                name,
                device="cpu",
                compute_type="int8",
                num_workers=2,
                download_root=str(MODELS / "whisper"),
            )
        return _models[name]


def transcribe(
    model_name: str,
    samples: np.ndarray,
    language: Optional[str] = None,
    offset: float = 0.0,
) -> tuple[list[dict], Optional[str]]:
    """[{s, e, w}] word timings (seconds + offset) and the detected language."""
    np.seterr(all="ignore")
    model = whisper(model_name)
    segments, info = model.transcribe(
        samples,
        language=language,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 400},
        beam_size=1 if model_name == "tiny" else 4,
        condition_on_previous_text=False,
    )
    words: list[dict] = []
    for seg in segments:
        for w in seg.words or []:
            text = (w.word or "").strip()
            if text:
                words.append({
                    "s": round(float(w.start or 0.0) + offset, 2),
                    "e": round(float(w.end or 0.0) + offset, 2),
                    "w": text,
                })
    return words, getattr(info, "language", None)


def _volume(samples: np.ndarray) -> list[float]:
    """dB per whole second."""
    n = len(samples) // RATE
    if not n:
        return []
    sec = samples[: n * RATE].reshape(n, RATE)
    return (20 * np.log10(np.sqrt(np.mean(sec ** 2, axis=1)) + 1e-5)).round(1).tolist()


def scan(
    audio: Path,
    total: float,
    lo: float,
    hi: float,
    work: Path,
    cfg: dict,
    live: bool = False,
) -> dict:
    """Words and per-second volume for [lo, hi) of the stream."""
    size = int(cfg["block_minutes"]) * 60
    folder = work / "blocks"
    folder.mkdir(exist_ok=True)
    first, last = int(lo // size), int(max(lo, hi - 0.01) // size)
    language = cfg["language"]
    words: list[dict] = []
    volume: dict[int, float] = {}
    count = last - first + 1
    say(
        f"listening to {(hi - lo) / 60:.0f} min of audio ({count} block(s) of "
        f"{cfg['block_minutes']} min)...",
        "step",
    )
    for n, b in enumerate(range(first, last + 1), 1):
        path = folder / f"{b:04d}.json"
        start = b * size
        length = min(size, total - start)
        if length <= 1:
            continue
        block = read_json(path)
        if not isinstance(block, dict) or (float(block.get("length", 0)) + 1 < length):
            samples = decode(audio, RATE, start, length)
            got, lang = transcribe(str(cfg["scan_model"]), samples, language, offset=float(start))
            block = {
                "length": length,
                "words": got,
                "volume": _volume(samples),
                "language": lang,
            }
            if not (live and length < size):
                write_json(path, block)
        words += [w for w in (block.get("words") or []) if lo <= float(w.get("s", 0.0)) < hi]
        for i, db in enumerate(block.get("volume") or []):
            volume[int(start + i)] = float(db)
        say(f"  heard {n}/{count}", "info")
    if not words:
        raise Problem("no speech was found in this part of the stream")
    secs = int(np.ceil(total))
    typical = float(np.median(list(volume.values()))) if volume else -40.0
    curve = np.full(secs, typical, dtype=np.float32)
    for t, db in volume.items():
        if 0 <= t < secs:
            curve[t] = db
    return {"words": words, "language": language, "volume": curve}


def spikes(curve: np.ndarray, window: int = 180) -> np.ndarray:
    """How many dB each second sits above the typical level of the minutes around it."""
    v = np.asarray(curve, dtype=np.float32)
    if len(v) < 10:
        return np.zeros_like(v)
    step = 5
    coarse = v[::step]
    half = max(1, window // step // 2)
    pad = np.pad(coarse, half, mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(pad, 2 * half + 1)
    base = np.repeat(np.percentile(windows, 60, axis=1), step)[: len(v)]
    return np.clip(v - base, 0, None)


def chat_speed(url: str, args: list[str], work: Path, meta: dict, cfg: dict) -> Optional[list[int]]:
    """Chat messages per 10 s from a YouTube live replay, or None."""
    if not cfg["chat_replay"] or "youtube" not in str(meta.get("extractor_key", "")).lower():
        return None
    if meta.get("live_status") not in ("was_live", "post_live"):
        return None
    cached = work / "chat.json"
    if cached.exists():
        loaded = read_json(cached)
        return loaded if isinstance(loaded, list) else None
    say("reading the chat replay (shows where the audience reacted)...", "step")
    try:
        run(
            [
                "yt-dlp",
                *args,
                "--skip-download",
                "--write-subs",
                "--sub-langs",
                "live_chat",
                "-o",
                str(work / "chat"),
                url,
            ],
            timeout=cfg["chat_timeout"],
        )
    except Problem as exc:
        say(f"chat replay skipped ({str(exc)[:80]})", "warn")
    files = sorted(work.glob("chat*.live_chat.json*"))
    if not files:
        write_json(cached, None)
        return None
    counts: dict[int, int] = {}
    with open(files[0], encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            try:
                item = json.loads(line)["replayChatItemAction"]
                bucket = int(item.get("videoOffsetTimeMsec", 0)) // 10000
            except (ValueError, KeyError, TypeError):
                continue
            counts[bucket] = counts.get(bucket, 0) + len(item.get("actions") or [1])
    for f in files:
        f.unlink(missing_ok=True)
    if not counts:
        write_json(cached, None)
        return None
    rate = [0] * (max(counts) + 1)
    for k, n in counts.items():
        rate[k] = n
    write_json(cached, rate)
    say(f"chat replay: {sum(rate):,} messages", "good")
    return rate