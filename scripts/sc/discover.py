"""Find the biggest recent streams for a topic. No API keys:
1. YouTube search, filtered to long videos from the chosen time window, sorted by views.
2. The user's watchlist (~/StreamClips/watchlist.txt): each creator's newest matching video.
"""

import base64
import json
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from .common import DATA, Problem, run, say
from .media import ytdlp_args

WHEN = {"hour": 1, "today": 2, "week": 3, "month": 4}
SORT_BY_VIEWS = 3
LONG = 2  # YouTube's "over 20 minutes" duration filter


def _search_filter(when: Optional[str]) -> str:
    """YouTube's `sp` search parameter (a tiny protobuf): sort by views + date + long."""
    inner = bytes([0x08, WHEN.get(when or "today", 2), 0x18, LONG])
    raw = bytes([0x08, SORT_BY_VIEWS, 0x12, len(inner)]) + inner
    return base64.b64encode(raw).decode()


def _listing(url: str, limit: int, args: list[str]) -> list[dict]:
    try:
        out = run(
            ["yt-dlp", "--flat-playlist", "--playlist-end", str(limit), "-J", *args, url],
            timeout=150,
        )
        entries = json.loads(out or "{}").get("entries") or []
        return [e for e in entries if isinstance(e, dict)]
    except (Problem, ValueError, TypeError) as exc:
        say(f"could not read {url[:70]} ({str(exc)[:90]})", "warn")
        return []


def youtube(query: str, when: Optional[str], limit: int = 20) -> list[dict]:
    url = (
        "https://www.youtube.com/results?search_query="
        + urllib.parse.quote_plus(query)
        + "&sp="
        + urllib.parse.quote(_search_filter(when))
    )
    found: list[dict] = []
    for e in _listing(url, limit, ytdlp_args("youtube")):
        if not e.get("id") or e.get("ie_key", "Youtube") not in ("Youtube", None):
            continue
        found.append({
            "url": f"https://www.youtube.com/watch?v={e['id']}",
            "platform": "youtube",
            "name": e.get("channel") or e.get("uploader") or "?",
            "title": e.get("title") or "",
            "views": e.get("view_count") or 0,
            "seconds": e.get("duration") or 0,
            "live": e.get("live_status") == "is_live",
            "from": "youtube search",
        })
    return found


def watchlist_entries(topic: Optional[str]) -> list[tuple[str, str]]:
    path = DATA / "watchlist.txt"
    if not path.exists():
        return []
    out: list[tuple[str, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        who, _, only = line.partition("|")
        if only.strip() and topic and only.strip().lower() not in topic.lower():
            continue
        platform, _, handle = who.strip().partition(":")
        if platform in ("twitch", "youtube", "kick") and handle.strip():
            out.append((platform, handle.strip()))
    return out


def _newest(
    platform: str,
    handle: str,
    words: list[str],
    max_age_h: float,
    cookies: Optional[str],
) -> Optional[dict]:
    if platform == "twitch":
        page = f"https://www.twitch.tv/{handle}/videos?filter=archives&sort=time"
    elif platform == "youtube":
        page = f"https://www.youtube.com/{handle if handle.startswith('@') else '@' + handle}/streams"
    else:
        page = f"https://kick.com/{handle}/videos"
    args = ytdlp_args(page, cookies)
    for e in _listing(page, 4, args):
        title = str(e.get("title") or "")
        if words and not any(w in title.lower() for w in words):
            continue
        url = str(e.get("url") or e.get("webpage_url") or "")
        if not url.startswith("http"):
            url = f"https://www.youtube.com/watch?v={e.get('id')}"
        try:
            full = json.loads(run(["yt-dlp", "-J", *args, url], timeout=150))
        except (Problem, ValueError, TypeError):
            continue
        if not isinstance(full, dict):
            continue
        when_ts = float(full.get("timestamp") or full.get("release_timestamp") or 0.0)
        if when_ts and time.time() - when_ts > max_age_h * 3600.0:
            return None
        return {
            "url": url,
            "platform": platform,
            "name": full.get("channel") or full.get("uploader") or handle,
            "title": title,
            "views": full.get("view_count") or full.get("concurrent_view_count") or 0,
            "seconds": full.get("duration") or 0,
            "live": full.get("live_status") == "is_live",
            "from": "watchlist",
        }
    return None


def find(
    topic: Optional[str],
    words: list[str],
    cfg: dict,
    count: int = 5,
    search: Optional[str] = None,
    cookies: Optional[str] = None,
) -> list[dict]:
    """Biggest first, at most one per creator, titles must match the topic words."""
    query = search or " ".join(x for x in (topic, cfg["search_suffix"]) if x)
    say(f'searching YouTube for "{query}" ({cfg["search_when"]})...', "step")
    found = youtube(query, cfg.get("search_when"))
    people = watchlist_entries(topic)
    if people:
        say(f"checking {len(people)} creator(s) on your watchlist...", "step")
        with ThreadPoolExecutor(max_workers=6) as pool:
            found += [
                r
                for r in pool.map(
                    lambda p: _newest(p[0], p[1], words, float(cfg["max_age_hours"]), cookies),
                    people,
                )
                if r
            ]
    best: dict[str, dict] = {}
    for item in found:
        if words and not any(w in str(item["title"]).lower() for w in words):
            continue
        key = str(item["name"]).lower()
        if key not in best or int(item["views"] or 0) > int(best[key]["views"] or 0):
            best[key] = item
    return sorted(best.values(), key=lambda s: -int(s["views"] or 0))[:count]