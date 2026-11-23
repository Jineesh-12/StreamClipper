"""Everything that happens to one stream, start to finish."""

import datetime
import hashlib
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import highlights, listen, media, produce
from .common import (
    CACHE,
    DATA,
    Stopwatch,
    clock,
    human,
    parse_clock,
    probe,
    read_json,
    say,
    slug,
    tag,
    write_json,
)


def work_dir(url):
    key = hashlib.sha1(url.strip().encode()).hexdigest()[:10]
    tail = slug(url.split("://")[-1], 40)
    path = CACHE / f"{tail}_{key}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def section_bounds(opts, total, work):
    lo, hi = 0.0, total
    if opts.section:
        a, _, b = opts.section.replace(" ", "").lstrip("*").partition("-")
        lo = parse_clock(a) if a else 0.0
        hi = min(parse_clock(b), total) if b else total
    elif opts.new_only:
        done = (read_json(work / "progress.json") or {}).get("done_until", 0.0)
        lo = max(0.0, done - 60)
    if hi - lo < 5.0:
        raise RuntimeError(f"nothing new to clip ({clock(lo)}-{clock(hi)} is too short)")
    if hi - lo < 60:
        say(f"short input ({clock(lo)}-{clock(hi)} is under 60s); processing/editing directly", "warn")
    return lo, hi


def process(url, opts, cfg):
    watch = Stopwatch()
    args = media.ytdlp_args(url, opts.cookies)
    work = work_dir(url)
    with watch.lap("look up"):
        meta = media.metadata(url, args, work)
    tag(meta["name"][:18])
    live = meta.get("live_status") == "is_live"
    if live:
        say("this stream is live: clipping what has been streamed so far", "warn")

    with watch.lap("download audio"):
        audio = media.audio(url, args, work, live=live)
    total = probe(audio)["duration"] or float(meta.get("duration") or 0)
    lo, hi = section_bounds(opts, total, work)
    say(
        f"{meta['title'][:70]}  ({human(total)}"
        + (f", clipping {clock(lo)}-{clock(hi)})" if (lo, hi) != (0.0, total) else ")"),
        "good",
    )

    with watch.lap("listen"):
        heard = listen.scan(audio, total, lo, hi, work, cfg, live=live)
        spike_db = listen.spikes(heard["volume"])
        chat = listen.chat_speed(url, args, work, meta, cfg)

    video = {
        "title": meta["title"],
        "name": meta["name"],
        "url": url,
        "topic": opts.topic or "this video",
        "focus": opts.focus,
    }
    with watch.lap("pick"):
        picks = highlights.pick(
            heard,
            spike_db,
            chat,
            lo,
            hi,
            work,
            cfg,
            video,
            opts.brain_resolved,
            opts.model,
            redo=opts.repick,
        )

    forced_out = os.environ.get("STREAMCLIPS_OUT_DIR", "").strip()
    if forced_out:
        outdir = Path(forced_out)
    else:
        day = datetime.date.today().isoformat()
        outdir = (
            DATA
            / day
            / slug(opts.topic or "clips", 30)
            / f"{slug(meta['name'], 24)}__{slug(meta['title'], 40)}"
        )

    memory = read_json(work / "cam.json", {}) or {}
    ctx = {
        "url": url,
        "args": args,
        "audio": audio,
        "work": work,
        "outdir": outdir,
        "language": cfg["language"],
        "fast": opts.fast,
        "cam": opts.cam_box,
    }
    made = []
    style_by_type = {
        "reaction": {"cap_active": "FF3B30", "title_bg": "FF3B30", "title_fg": "FFFFFF", "cap_words": 2, "cap_size": 94},
        "skill":    {"cap_active": "39FF14", "title_bg": "111111", "title_fg": "39FF14", "cap_words": 2, "cap_size": 90},
        "funny":    {"cap_active": "FF9500", "title_bg": "FFF3E0", "title_fg": "111111", "cap_words": 2, "cap_size": 92},
        "reveal":   {"cap_active": "00E5FF", "title_bg": "0B132B", "title_fg": "00E5FF", "cap_words": 3, "cap_size": 88},
        "take":     {"cap_active": "FFD400", "title_bg": "FFFFFF", "title_fg": "111111", "cap_words": 3, "cap_size": 88},
        "tip":      {"cap_active": "00E676", "title_bg": "FFFFFF", "title_fg": "111111", "cap_words": 3, "cap_size": 86},
        "story":    {"cap_active": "FFD400", "title_bg": "1A1A1A", "title_fg": "FFFFFF", "cap_words": 3, "cap_size": 86},
    }
    fallback_styles = list(style_by_type.values())

    with watch.lap("make clips"):
        def _build_one(item):
            i, pick = item
            clip_cfg = dict(cfg)
            moment_type = str(pick.get("type") or "").lower().strip()
            chosen_style = style_by_type.get(moment_type, fallback_styles[(i - 1) % len(fallback_styles)])
            clip_cfg.update(chosen_style)
            for attempt in range(1, 4):
                try:
                    return produce.make(i, pick, ctx, clip_cfg, memory)
                except Exception as exc:  # noqa: BLE001 - one bad clip must not stop the rest
                    if attempt < 3:
                        say(f"clip {i} attempt {attempt} failed, retrying in 3s...", "warn")
                        time.sleep(3)
                    else:
                        say(f"clip {i} failed: {str(exc)[:300]}", "bad")
            return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            for res in pool.map(_build_one, enumerate(picks, 1)):
                if res:
                    made.append(res)

    write_json(work / "cam.json", memory)

    summary = (
        f"{human(hi - lo)} of stream -> {len(made)} clip(s) in {human(watch.total())} ("
        + ", ".join(f"{k} {human(v)}" for k, v in watch.parts.items())
        + ")"
    )
    if made:
        posting_sheet(outdir, video, made, summary, lo, hi)
        write_json(work / "progress.json", {"done_until": hi, "at": datetime.datetime.now().isoformat()})
        if not opts.keep_audio and not live:
            audio.unlink(missing_ok=True)
    say(summary, "good")
    tag("")
    return {
        "name": meta["name"],
        "title": meta["title"],
        "outdir": outdir,
        "clips": made,
        "summary": summary,
    }


def posting_sheet(outdir, video, clips, summary, lo, hi):
    for idx, c in enumerate(clips, 1):
        caption = (c.get("caption") or "").strip()
        hashtags = " ".join(c.get("hashtags") or []).strip()
        score = f"{c['score']}/100" if c.get("score") is not None else "N/A/100"
        if c.get("start") is not None and c.get("seconds") is not None:
            ts = f"{clock(c['start'])} - {clock(c['start'] + c['seconds'])} ({c['seconds']:.0f}s)"
        else:
            ts = "Auto-detected"

        desc_parts = [p for p in [caption or f"Peak moment from '{video['title']}'.", hashtags] if p]
        description = "\n\n".join(desc_parts)
        what_happens = (c.get("why") or f"{c.get('title') or ''} (Captions burned directly into the clip).").strip()

        txt_content = "\n".join([
            f"SOURCE VIDEO : {video['title']}",
            f"SOURCE LINK  : {video['url']}",
            f"CLIP #{idx:02d}     : {c['file']}",
            "=" * 60,
            f"TITLE        : {c.get('title') or ''}",
            f"VIRAL SCORE  : {score}",
            f"TIMESTAMPS   : {ts}",
            "-" * 60,
            "DESCRIPTION  :",
            description,
            "-" * 60,
            "SPOKEN CAPTIONS / WHAT HAPPENS IN THIS CLIP :",
            what_happens,
            "",
        ])
        (outdir / c["file"]).with_suffix(".txt").write_text(txt_content, encoding="utf-8")

    if outdir.exists():
        for entry in outdir.iterdir():
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            elif entry.suffix.lower() not in (".mp4", ".txt"):
                entry.unlink(missing_ok=True)