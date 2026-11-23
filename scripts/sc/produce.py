"""Build one finished clip: fetch its video, sync it to the stream audio, caption it,
frame it 9:16, put the title bar on, encode, and save a check image."""

import os
import re
import shutil
import subprocess
from functools import lru_cache

import numpy as np

from . import frame, listen, media, subs, titlecard
from .common import Problem, clock, decode, probe, run, say, slug

PAD = 5.0          # extra seconds fetched on each side; trimmed after syncing
ENV_RATE = 100     # envelope samples per second used for syncing


def _envelope(samples, rate=8000):
    hop = rate // ENV_RATE
    n = len(samples) // hop
    if not n:
        return np.zeros(0, np.float32)
    e = np.log(np.sqrt(np.mean(samples[: n * hop].reshape(n, hop) ** 2, axis=1)) + 1e-4)
    k = ENV_RATE                                   # remove the slow trend (1 s mean)
    trend = np.convolve(np.pad(e, k // 2, mode="edge"), np.ones(k) / k, mode="valid")[: len(e)]
    return (e - trend).astype(np.float32)


def sync(part, audio, guess, reach=25.0):
    """Stream time of the part's first frame, by matching its sound to the stream audio.
    Returns (seconds, confidence 0-1)."""
    probe_len = 20.0
    a = _envelope(decode(part, 8000, 0.5, probe_len))
    lo = max(0.0, guess + 0.5 - reach)
    b = _envelope(decode(audio, 8000, lo, probe_len + 2 * reach))
    if len(a) < ENV_RATE * 5 or len(b) <= len(a):
        return guess, 0.0
    a = (a - a.mean()) / (a.std() + 1e-6)
    win = np.lib.stride_tricks.sliding_window_view(b, len(a))
    mu, sd = win.mean(axis=1), win.std(axis=1) + 1e-6
    score = (win @ a - mu * a.sum()) / (len(a) * sd)
    k = int(np.argmax(score))
    return lo + k / ENV_RATE - 0.5, float(score[k])


@lru_cache(maxsize=1)
def video_encoder(fast):
    """libx264, or a hardware encoder that actually works on this machine when fast=True."""
    if not fast:
        return "libx264"
    for enc in ("h264_videotoolbox", "h264_nvenc", "h264_qsv", "h264_amf"):
        test = [
            "ffmpeg", "-v", "error", "-nostdin",
            "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.2",
            "-c:v", enc, "-f", "null", "-",
        ]
        if subprocess.run(test, capture_output=True).returncode == 0:
            return enc
    return "libx264"


def _encode_args(enc, cfg):
    hd_crf = min(18, int(cfg.get("crf", 18)))
    if enc == "libx264":
        return [
            "-c:v", "libx264",
            "-crf", str(hd_crf),
            "-preset", cfg.get("x264_preset", "medium"),
            "-profile:v", "high",
            "-pix_fmt", "yuv420p",
        ]
    if enc == "h264_nvenc":
        return [
            "-c:v", enc,
            "-preset", "p4",
            "-rc", "vbr",
            "-cq", "19",
            "-b:v", "10M",
            "-maxrate", "14M",
            "-bufsize", "20M",
            "-pix_fmt", "yuv420p",
        ]
    return [
        "-c:v", enc,
        "-b:v", "10M",
        "-maxrate", "14M",
        "-bufsize", "20M",
        "-pix_fmt", "yuv420p",
    ]


def check_image(clip, length, dest):
    """Three frames side by side, so a person (or an agent that can see images) can check
    the framing, the title and the captions without playing the video."""
    from PIL import Image
    shots = []
    for i, f in enumerate((0.2, 0.5, 0.8)):
        tmp = dest.with_name(dest.stem + f"_{i}.jpg")
        run([
            "ffmpeg", "-y", "-v", "error", "-nostdin",
            "-ss", f"{length * f:.2f}", "-i", str(clip),
            "-frames:v", "1", "-vf", "scale=360:-2", str(tmp),
        ])
        with Image.open(tmp) as im:
            shots.append(im.convert("RGB"))
        tmp.unlink(missing_ok=True)
    sheet = Image.new("RGB", (sum(s.width for s in shots) + 20, shots[0].height), (20, 20, 20))
    x = 0
    for s in shots:
        sheet.paste(s, (x, 0))
        x += s.width + 10
    sheet.save(dest, quality=85)
    return dest


_BAD_WORDS = re.compile(
    r"\b(fuck\w*|shit\w*|bitch\w*|asshole\w*|bastard\w*|motherfuck\w*|cunt\w*|dick\w*|pussy\w*|bullshit\w*)\b",
    re.IGNORECASE,
)


def _censor_words(words):
    """Censor swear words in captions and return [(start, end), ...] bleep windows."""
    bleeps = []
    for w in words:
        text = str(w.get("w", ""))
        if _BAD_WORDS.search(text):
            w["w"] = _BAD_WORDS.sub(lambda m: m.group(0)[0] + "*" * (len(m.group(0)) - 1), text)
            s = max(0.0, float(w["s"]) - 0.04)
            e = float(w["e"]) + 0.04
            bleeps.append((s, e))
    return bleeps


def _find_hype_spikes(pcm, rate, length, max_zooms=3):
    """Find loud reaction spikes in clip audio for smart punch-in zooms."""
    hop = int(rate * 0.25)
    n = len(pcm) // hop
    if n < 12 or length < 6.0:
        return []
    rms = np.sqrt(np.mean(pcm[: n * hop].reshape(n, hop) ** 2, axis=1))
    thresh = max(float(rms.mean() + 1.6 * rms.std()), 0.05)
    windows = []
    for idx in np.argsort(rms)[::-1]:
        if rms[idx] < thresh:
            break
        t = idx * 0.25
        if t < 1.2 or t > length - 1.5:
            continue
        if any(abs(t - s) < 6.0 for s, _ in windows):
            continue
        windows.append((max(0.5, t - 0.15), min(length - 0.3, t + 1.45)))
        if len(windows) >= max_zooms:
            break
    return sorted(windows)


def _detect_punchline(words, length, zooms, pick):
    """Return a short end-clip punchline only when the ending has a strong payoff cue or spike."""
    if length < 10.0:
        return None
    tail_start = max(0.0, length - 4.0)
    near_end_spike = any(e >= tail_start for _, e in (zooms or []))
    tail_words = " ".join(
        str(w.get("w", "")).lower()
        for w in (words or [])
        if float(w.get("e", 0.0)) >= tail_start
    )
    if any(k in tail_words for k in ("busted", "wasted")):
        return "BUSTED 😂"
    if any(k in tail_words for k in ("clutch", "insane", "crazy")):
        return "INSANE 🔥"
    if near_end_spike and any(k in tail_words for k in ("cop", "police", "run", "oh", "no", "bro")):
        return "WHAT JUST HAPPENED 😭"
    if near_end_spike:
        label = str(pick.get("type") or "").strip().upper()
        return f"{label} MOMENT 🔥" if label and label != "CLIP" else "PEAK MOMENT 🔥"
    return None


def _write_clip_txt(dest_mp4, index: int, pick: dict, ctx: dict, start: float, length: float):
    """Write a companion .txt file next to each generated clip."""
    caption = (pick.get("caption") or "").strip()
    hashtags = " ".join(pick.get("hashtags") or []).strip()
    score = f"{pick['score']}/100" if pick.get("score") is not None else "N/A/100"
    ts = f"{clock(start)} - {clock(start + length)} ({length:.0f}s)"
    desc_parts = [p for p in [caption or f"Peak moment from '{ctx['url']}'.", hashtags] if p]
    description = "\n\n".join(desc_parts)
    what_happens = (pick.get("why") or f"{pick.get('title') or ''} (Captions burned directly into the clip).").strip()

    txt_content = "\n".join([
        f"SOURCE LINK  : {ctx['url']}",
        f"CLIP #{index:02d}     : {dest_mp4.name}",
        "=" * 60,
        f"TITLE        : {pick.get('title') or ''}",
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
    dest_mp4.with_suffix(".txt").write_text(txt_content, encoding="utf-8")


def make(index, pick, ctx, cfg, memory):
    """ctx: url, args, audio, work, outdir, language, fast, cam. Returns the clip record."""
    start, end = pick["start"], pick["end"]
    length = end - start
    name = f"{index:02d}_{slug(pick.get('title') or pick.get('type'), 48)}"
    say(
        f"clip {index}: {pick.get('title') or pick.get('type')}  ({length:.0f}s at "
        f"{int(start // 3600)}:{int(start // 60 % 60):02d}:{int(start % 60):02d})",
        "step",
    )
    clips_dir = ctx["work"] / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    part = media.video_slice(
        ctx["url"], ctx["args"], start - PAD, end + PAD,
        clips_dir / f"{name}.src.mp4", max(1080, int(cfg.get("video_height", 1080))),
    )
    info = probe(part)
    if not info["width"]:
        raise Problem("the downloaded video has no picture")

    at, conf = sync(part, ctx["audio"], start - PAD)
    offset = start - at if conf >= 0.45 else min(PAD, max(0.0, start))
    offset = min(max(0.0, offset), max(0.0, info["duration"] - length))
    if conf < 0.45:
        say(f"  weak sound match ({conf:.2f}); using the requested times, may be off by ~1 s", "warn")

    clip_pcm = decode(part, 16000, offset, length)
    words, _ = listen.transcribe(cfg["caption_model"], clip_pcm, ctx["language"])
    bleeps = _censor_words(words) if os.environ.get("STREAMCLIPS_BLEEP", "1") == "1" else []
    auto_fx = os.environ.get("STREAMCLIPS_AUTO_FX", "1") == "1"
    zooms = _find_hype_spikes(clip_pcm, 16000, length)
    ass = subs.write(words, length, clips_dir / f"{name}.ass", cfg)
    graph, look, seam = frame.plan(part, offset, length, info, cfg, memory, ctx.get("cam"))

    W, H = int(cfg["width"]), int(cfg["height"])
    inputs = ["-ss", f"{offset:.3f}", "-t", f"{length:.3f}", "-i", str(part)]
    next_input_idx = 1

    if zooms:
        z_expr = "+".join(f"between(t,{s:.2f},{e:.2f})" for s, e in zooms)
        if auto_fx:
            shake_x = f"(iw-{W})/2 + 4*sin(t*42)*min(1,{z_expr})"
            shake_y = f"(ih-{H})/2 + 3*cos(t*37)*min(1,{z_expr})"
            flash_expr = "+".join(
                f"between(t,{s:.2f},{min(length, s + 0.12):.2f})" for s, _ in zooms
            )
            chain = (
                f"{graph};[framed]scale=w='trunc(iw*(1+0.12*min(1,{z_expr}))/2)*2':"
                f"h='trunc(ih*(1+0.12*min(1,{z_expr}))/2)*2':eval=frame,"
                f"crop={W}:{H}:'{shake_x}':'{shake_y}',"
                f"eq=brightness='0.14*min(1,{flash_expr})':eval=frame[zoomed];"
                f"[zoomed]ass={ass.name}[subbed]"
            )
        else:
            chain = (
                f"{graph};[framed]scale=w='trunc(iw*(1+0.12*min(1,{z_expr}))/2)*2':"
                f"h='trunc(ih*(1+0.12*min(1,{z_expr}))/2)*2':eval=frame,"
                f"crop={W}:{H}:(iw-{W})/2:(ih-{H})/2[zoomed];[zoomed]ass={ass.name}[subbed]"
            )
    else:
        chain = f"{graph};[framed]ass={ass.name}[subbed]"
    out_label = "subbed"

    title = pick.get("title")
    if title:
        try:
            png, _, th = titlecard.render(title, cfg, clips_dir / f"{name}.title.png")
            y = int(seam - th / 2) if seam else int(H * cfg["title_top"])
            t_idx = next_input_idx
            next_input_idx += 1
            inputs += ["-loop", "1", "-t", "4.2", "-i", str(png)]
            chain += (
                f";[{t_idx}:v]format=rgba,fade=t=out:st=3.5:d=0.5:alpha=1[tbar]"
                f";[{out_label}][tbar]overlay=(W-w)/2:{max(0, y)}:eof_action=pass[titled]"
            )
            out_label = "titled"
        except (OSError, ValueError) as exc:
            say(f"  title bar skipped ({exc})", "warn")

    punchline = _detect_punchline(words, length, zooms, pick) if auto_fx else None
    if punchline:
        try:
            punch_png, _, ph = titlecard.render(punchline, cfg, clips_dir / f"{name}.punch.png")
            p_idx = next_input_idx
            next_input_idx += 1
            p_start = max(0.0, length - 1.5)
            p_y = min(H - ph - 40, int(H * 0.72))
            inputs += ["-loop", "1", "-t", "1.6", "-i", str(punch_png)]
            chain += (
                f";[{p_idx}:v]format=rgba[pcard]"
                f";[{out_label}][pcard]overlay=(W-w)/2:{max(0, p_y)}:enable='gte(t,{p_start:.2f})':eof_action=pass[punched]"
            )
            out_label = "punched"
        except (OSError, ValueError) as exc:
            say(f"  punchline skipped ({exc})", "warn")

    bar_hex = re.sub(r"[^0-9A-Fa-f]", "", str(cfg.get("cap_active", "FFD400")))
    if len(bar_hex) != 6:
        bar_hex = "FFD400"
    chain += (
        f";color=c=0x{bar_hex}:s={W}x10:d={length:.2f}[pbar]"
        f";[{out_label}][pbar]overlay=x='-W+(t/{max(0.1, length):.3f})*W':y=0:shortest=1[final]"
    )
    out_label = "final"

    aud_base = (
        f"loudnorm=I={cfg['loudness']}:TP=-1.5:LRA=11,aresample=48000"
        if cfg["loudness"] is not None
        else "aresample=48000"
    )
    if bleeps:
        expr = "+".join(f"between(t,{s:.2f},{e:.2f})" for s, e in bleeps)
        chain += (
            f";[0:a]{aud_base},volume=0:enable='{expr}'[a_muted]"
            f";sine=frequency=1000:sample_rate=48000:duration={length:.2f},"
            f"volume='if({expr},0.22,0)':eval=frame[a_beep]"
            f";[a_muted][a_beep]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aud]"
        )
    else:
        chain += f";[0:a]{aud_base}[aud]"

    ctx["outdir"].mkdir(parents=True, exist_ok=True)
    dest = ctx["outdir"] / f"{name}.mp4"
    enc = video_encoder(ctx.get("fast", False))
    run(
        [
            "ffmpeg", "-y", "-v", "error", "-nostdin",
            *inputs,
            "-filter_complex", chain,
            "-map", f"[{out_label}]",
            "-map", "[aud]",
            *_encode_args(enc, cfg),
            "-r", f"{min(60.0, info['fps']):.3f}",
            "-c:a", "aac",
            "-b:a", f"{max(192, int(cfg.get('audio_kbps', 192)))}k",
            "-movflags", "+faststart",
            dest.name,
        ],
        timeout=3600,
        cwd=str(clips_dir),
    )
    produced = clips_dir / dest.name
    shutil.move(str(produced), str(dest))
    _write_clip_txt(dest, index, pick, ctx, start, length)

    checks = ctx["outdir"] / "checks"
    checks.mkdir(exist_ok=True)
    try:
        check_image(dest, length, checks / f"{name}.jpg")
    except Problem:
        pass
    part.unlink(missing_ok=True)
    say(f"  saved {dest.name} ({look})", "good")
    return {
        "file": dest.name,
        "type": pick.get("type"),
        "title": title,
        "caption": pick.get("caption"),
        "hashtags": pick.get("hashtags", []),
        "score": pick.get("score"),
        "why": pick.get("why"),
        "look": look,
        "start": round(start, 1),
        "end": round(end, 1),
        "seconds": round(length, 1),
        "sync": round(conf, 2),
    }