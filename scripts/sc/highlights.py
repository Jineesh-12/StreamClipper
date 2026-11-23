"""Turn the transcript + signals into a short list of clip-worthy moments.

The transcript is split into overlapping windows. Each window becomes one prompt for the
chosen brain (see brains.py). Answers are cached per window, so a stopped run resumes and
agent mode can answer the windows one by one.
"""

import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import numpy as np

from . import brains
from .common import Problem, read_json, say, write_json

PROMPT = """You are an editor who cuts short vertical clips (TikTok, Instagram Reels, YouTube \
Shorts) out of long videos. You are paid by views, so you only pick moments that grab a \
stranger in the first second and keep them to the end.

Video: "<<TITLE>>" by <<CREATOR>>
Topic: <<TOPIC>><<FOCUS>>

Below is part of the transcript. Each line starts with its time in seconds. Some lines \
carry signals:
  <<VOLUME +N dB>>  the audio got N dB louder than the minutes around it (shouting, laughing, hype)
  <<CHAT Nx>>       the live chat moved N times faster than usual right then (the audience reacted)
Signals are hints, not proof: gunshots, music and explosions are loud too. The transcript \
can include other voices (game characters, a video being watched), so judge who is talking \
from context.

Moment types that do well for <<TOPIC>>:
  reaction  a big emotional reaction: shock, rage, screaming, laughing fit, speechless
  skill     an impressive play, clutch, outplay or win, together with the reaction to it
  funny     a fail, a chaotic accident, a joke or roast people send to a friend
  reveal    seeing, finding or learning something new or surprising for the first time
  take      a bold opinion or prediction people will argue about in the comments
  tip       a specific, useful tip or insight a viewer can use right away
  story     a short story with a clear punchline

Pick at most <<K>> moments from this part. Each moment must:
- start just before its hook, never on filler ("uh", "anyway", "so yeah", "okay so")
- make sense with zero context and pay off before it ends
- be <<MIN>>-<<MAX>> seconds long, lie inside <<LO>>-<<HI>>, and not overlap another pick
Skip sponsor reads, reading donations or subs, menus, loading screens, silence, and \
anything whose payoff falls outside this part. An empty list [] is a fine answer when \
nothing is strong.

Reply with a JSON array and nothing else (no prose, no markdown fence). Example shape:
[{"start": 1204.0, "end": 1239.5, "type": "reaction", "title": "...", "caption": "...", \
"hashtags": ["#..."], "score": 72, "why": "..."}]

title: the text bar shown over the clip. 3-10 words, names the creator, CAPITALS on the 1-3 \
words that carry the drama, teases the payoff without giving it away, at most one emoji.
caption: the post text, under 140 characters, ending with a question or a nudge to comment.
hashtags: 3-6 tags: the topic, the creator, one broad tag.
score: 1-100, how likely a viewer watches to the end. Be strict: 80+ only for clips you \
would bet money on.
why: one short sentence.

TRANSCRIPT <<LO>>-<<HI>>:
<<TEXT>>
"""


def _fill(**values: object) -> str:
    text = PROMPT
    for key, value in values.items():
        text = text.replace(f"<<{key}>>", str(value))
    return text


def transcript_lines(
    words: list[dict],
    spike_db: np.ndarray,
    chat: Optional[list[int]],
    cfg: dict,
) -> list[tuple[float, str]]:
    """Group words into short lines (~7 s, broken at pauses) and attach signals."""
    chat_base = float(np.median([c for c in chat if c > 0])) if chat and any(chat) else 0.0
    lines: list[tuple[float, str]] = []
    cur: list[dict] = []

    def close() -> None:
        if not cur:
            return
        t0, t1 = float(cur[0]["s"]), float(cur[-1]["e"])
        marks: list[str] = []
        if len(spike_db):
            a = min(int(t0), len(spike_db) - 1)
            b = max(a + 1, min(int(np.ceil(t1)) + 1, len(spike_db)))
            peak = float(np.max(spike_db[a:b]))
            if peak >= float(cfg["volume_jump_db"]):
                marks.append(f"<<VOLUME +{peak:.0f} dB>>")
        if chat_base and chat is not None:
            seg = chat[int(t0 // 10) : int(t1 // 10) + 2]
            if seg and max(seg) / chat_base >= float(cfg["chat_jump"]):
                marks.append(f"<<CHAT {max(seg) / chat_base:.0f}x>>")
        text = " ".join(str(w["w"]) for w in cur)
        lines.append((t0, (" ".join(marks) + " " if marks else "") + text))
        cur.clear()

    for i, w in enumerate(words):
        cur.append(w)
        gap = float(words[i + 1]["s"]) - float(w["e"]) if i + 1 < len(words) else 99.0
        span = float(w["e"]) - float(cur[0]["s"])
        if span >= 7.0 or (gap > 1.2 and span >= 2.0) or gap > 3.0:
            close()
    close()
    return lines


def windows(
    lines: list[tuple[float, str]],
    lo: float,
    hi: float,
    cfg: dict,
) -> list[tuple[float, float, str]]:
    size = float(cfg["part_minutes"]) * 60.0
    overlap = float(cfg["part_overlap"])
    out: list[tuple[float, float, str]] = []
    start = lo
    while start < hi:
        end = min(hi, start + size)
        if hi - end < 120.0:
            end = hi
        body = [f"[{t:.1f}] {text}" for t, text in lines if start <= t < end]
        if sum(len(b.split()) for b in body) >= 40:
            out.append((start, end, "\n".join(body)))
        if end >= hi:
            break
        start = end - overlap
    return out


def parse_reply(raw: Optional[str]) -> Optional[list]:
    text = (raw or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    if not text.startswith("["):
        found = re.search(r"\[\s*(?:\{.*\})?\s*\]", text, re.S)
        if not found:
            return None
        text = found.group(0)
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, list) else None


def tidy(pick_item: dict, words: list[dict], cfg: dict) -> Optional[dict]:
    """Move the AI's times onto clean word boundaries and keep the length in range."""
    try:
        s, e = float(pick_item["start"]), float(pick_item["end"])
    except (KeyError, TypeError, ValueError):
        return None
    if e <= s:
        return None
    pool = [w for w in words if s - 4.0 <= float(w["s"]) <= e + 4.0]
    if not pool:
        return None

    where = {id(w): i for i, w in enumerate(words)}

    def gap_before(w: dict) -> float:
        i = where[id(w)]
        return float(w["s"]) - float(words[i - 1]["e"]) if i else 9.0

    def gap_after(w: dict) -> float:
        i = where[id(w)]
        return float(words[i + 1]["s"]) - float(w["e"]) if i + 1 < len(words) else 9.0

    near = [w for w in pool if abs(float(w["s"]) - s) <= 2.0] or [
        min(pool, key=lambda w: abs(float(w["s"]) - s))
    ]
    first = min(
        near,
        key=lambda w: abs(float(w["s"]) - s) - (0.8 if gap_before(w) > 0.35 else 0.0),
    )

    tail = [w for w in pool if float(w["s"]) >= float(first["s"]) and abs(float(w["e"]) - e) <= 2.5]
    if not tail:
        tail = [w for w in pool if float(w["s"]) >= float(first["s"])]
    last = min(
        tail,
        key=lambda w: abs(float(w["e"]) - e)
        - (0.9 if str(w["w"]).endswith((".", "!", "?")) else 0.0)
        - (0.6 if gap_after(w) > 0.5 else 0.0),
    )

    lo_len, hi_len = float(cfg["min_len"]), float(cfg["max_len"])
    if float(last["e"]) - float(first["s"]) > hi_len:
        fits = [
            w
            for w in words
            if float(w["s"]) >= float(first["s"]) and float(w["e"]) - float(first["s"]) <= hi_len - 1.0
        ]
        if not fits:
            return None
        enders = [
            w
            for w in fits
            if str(w["w"]).endswith((".", "!", "?")) and float(w["e"]) - float(first["s"]) >= lo_len
        ]
        last = enders[-1] if enders else fits[-1]

    if float(last["e"]) - float(first["s"]) < lo_len:
        more = [
            w
            for w in words
            if float(w["e"]) > float(last["e"]) and lo_len <= float(w["e"]) - float(first["s"]) <= hi_len - 1.0
        ]
        if not more:
            return None
        last = more[0]

    out = dict(pick_item)
    out["start"] = round(max(0.0, float(first["s"]) - 0.35), 2)
    out["end"] = round(min(float(last["e"]) + 0.55, float(out["start"]) + hi_len), 2)
    try:
        out["score"] = int(float(pick_item.get("score", 50)))
    except (TypeError, ValueError):
        out["score"] = 50
    out["type"] = str(pick_item.get("type") or "moment")
    out["title"] = str(pick_item.get("title") or "").strip()
    out["caption"] = str(pick_item.get("caption") or "").strip()
    tags = pick_item.get("hashtags") or []
    out["hashtags"] = [
        t if str(t).startswith("#") else f"#{t}" for t in tags if str(t).strip()
    ][:6]
    return out


def best(cands: list[dict], n: int, min_score: int) -> list[dict]:
    """Highest scores first; no two picks share more than a sliver of time."""
    chosen: list[dict] = []
    for c in sorted(cands, key=lambda item: -int(item["score"])):
        if int(c["score"]) < min_score:
            continue
        if all(
            min(float(c["end"]), float(k["end"])) - max(float(c["start"]), float(k["start"])) < 2.0
            for k in chosen
        ):
            chosen.append(c)
        if len(chosen) >= n:
            break
    return sorted(chosen, key=lambda item: float(item["start"]))


def pick(
    heard: dict,
    spike_db: np.ndarray,
    chat: Optional[list[int]],
    lo: float,
    hi: float,
    work: Path,
    cfg: dict,
    video: dict,
    brain: str,
    model: Optional[str] = None,
    redo: bool = False,
) -> list[dict]:
    folder = work / "picks"
    folder.mkdir(exist_ok=True)
    final = folder / f"chosen_{int(lo)}_{int(hi)}.json"
    if redo:
        for f in folder.glob("*"):
            if f.suffix == ".json" or f.name.endswith(".prompt.txt"):
                f.unlink(missing_ok=True)
    if final.exists():
        chosen = read_json(final, []) or []
        say(f"reusing {len(chosen)} picked moment(s)", "good")
        return chosen

    words = heard["words"]
    span = max(0.0, float(hi) - float(lo))
    if 0 < span <= max(120.0, float(cfg.get("max_len", 60))):
        say(f"short video detected ({span:.0f}s) — editing full clip directly", "step")
        chosen = [{
            "start": round(float(lo), 2),
            "end": round(float(hi), 2),
            "type": "highlight",
            "title": (video.get("title") or video.get("topic") or "Short Clip")[:60],
            "caption": video.get("focus") or f"Best moment from {video.get('topic') or 'stream'}.",
            "hashtags": [
                f"#{re.sub(r'[^A-Za-z0-9]', '', str(video.get('topic') or 'Shorts'))}",
                "#Shorts",
                "#Viral",
            ],
            "score": 90,
            "why": "Short source video edited directly as a full vertical clip.",
        }]
        write_json(final, chosen)
        return chosen

    parts = windows(transcript_lines(words, spike_db, chat, cfg), lo, hi, cfg)
    if not parts:
        raise Problem("there's not enough talking in this stream to clip")

    need_per_part = (int(cfg["clips"]) + len(parts) - 1) // len(parts)
    k_per_part = max(int(cfg["picks_per_part"]), int(need_per_part * 1.6) + 2)

    def prompt_for(a: float, b: float, body: str) -> str:
        return _fill(
            TITLE=video["title"],
            CREATOR=video["name"],
            TOPIC=video["topic"],
            FOCUS=f"\nWhat the clipper wants: {video['focus']}" if video.get("focus") else "",
            K=k_per_part,
            MIN=cfg["min_len"],
            MAX=cfg["max_len"],
            LO=f"{a:.0f}",
            HI=f"{b:.0f}",
            TEXT=body,
        )

    def answer_file(a: float) -> Path:
        return folder / f"part_{int(a):06d}.answer.json"

    if brain == "agent":
        waiting: list[tuple[Path, Path]] = []
        for a, b, body in parts:
            ans = answer_file(a)
            if not ans.exists():
                q = folder / f"part_{int(a):06d}.prompt.txt"
                q.write_text(prompt_for(a, b, body), encoding="utf-8")
                waiting.append((q, ans))
        if waiting:
            raise brains.NeedAgent(waiting)
    else:
        say(
            f"{brain} is reading {len(parts)} part(s) of the transcript, "
            f"{cfg['brain_parallel']} at a time...",
            "step",
        )

    def work_on(part: tuple[float, float, str]) -> list[dict]:
        a, b, body = part
        ans = answer_file(a)
        got = read_json(ans) if ans.exists() else None
        if got is None and brain != "agent":
            reply = None
            for _ in range(2):
                try:
                    reply = parse_reply(brains.ask(brain, prompt_for(a, b, body), model, folder))
                except brains.SignedOut:
                    raise
                except Problem as exc:
                    say(f"  part at {a / 60:.0f} min: {str(exc)[:160]}", "warn")
                    reply = None
                if reply is not None:
                    break
            if reply is None:
                return []
            got = reply
            write_json(ans, got)
        if not isinstance(got, list):
            return []
        good: list[dict] = []
        for m in got:
            if isinstance(m, dict):
                t = tidy(m, words, cfg)
                if t and a - 5.0 <= float(t["start"]) < b + 5.0:
                    good.append(t)
        return good

    cands: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, int(cfg["brain_parallel"]))) as pool:
        for got in pool.map(work_on, parts):
            cands += got
    chosen = best(cands, int(cfg["clips"]), int(cfg["min_score"]))
    write_json(folder / f"candidates_{int(lo)}_{int(hi)}.json", cands)
    if not chosen:
        raise Problem(
            "the AI found nothing strong enough to clip in this stream "
            "(try another stream, or lower min_score in settings.json)"
        )
    write_json(final, chosen)
    say(f"picked {len(chosen)} of {len(cands)} candidate moments", "good")
    return chosen