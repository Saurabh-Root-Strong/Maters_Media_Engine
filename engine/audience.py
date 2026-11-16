"""Who the channel posts for, and when a new post would hurt the last one.

Three checks, each from evidence on this channel (Nov 2026: English-text Shorts
got 2-21 views while Hinglish ones got 172-1,368; a Short posted 6 hours after
one still being pushed got 6 views):

- default_language: the language the AUDIENCE reads on each platform, from
  config/brand.yaml. The video's own on-screen text used to decide this, so
  an English quote reel got English copy for a Hinglish audience.
- text_language: what language the text IN the video is in. Copy cannot fix
  that; it is a note to re-edit before posting.
- posting_gap: whether the channel's last Short is under a day old and still
  gaining, so a second one now would compete with it for the same feed.

Nothing here blocks writing. Every failure (no key, no channel, network) means
no note, never an error.
"""

from __future__ import annotations

import os
import re
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone

from . import memory

LANGS = ("english", "hindi", "hinglish")
GAP_HOURS = 24             # a Short is usually still being tested in the feed for about a day
STILL_GROWING = 0.03       # >= 3% more views between two reads = still being pushed
DEAD_VIEWS = 50            # under this after 6 hours, the feed never picked the Short up
_IST = timezone(timedelta(hours=5, minutes=30))
_CHANNEL_ID = re.compile(r"^UC[\w-]{22}$")

# Words that only appear in Hindi written in Roman letters. Short English words
# that collide ("the", "main", "log", "tab", "jab", "mat", "fir", "kar") are left out on purpose.
_HINGLISH = frozenset("""
hai hain ho hota hoti hote tha thi kya kyu kyun kyon kaise kaisa kaisi kab kahan
aur bhi nahi nahin ka ki ke ko se mein mai hum tum aap apna apni apne mera meri
mere tera teri tumhara tumhari yeh ye woh wo agar toh bas sirf bilkul jaisa jaise
chahiye chahte karo karna karte karta raha rahi rahe gaya gayi wala wali wale
paisa paise sukoon dil zindagi bhai yaar acha accha kuch kuchh abhi phir
lekin magar asli naya nayi hamesha kabhi samay waqt dekho suno socho kamao kamana
""".split())
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_WORD = re.compile(r"[a-z]+")


# --- language -----------------------------------------------------------------

def _config() -> dict:
    langs = (memory.brand_profile() or {}).get("languages")
    return langs if isinstance(langs, dict) else {}


def configured() -> bool:
    """Has the creator said what their audience reads? Without it, nothing is assumed."""
    return any(str(v).lower() in LANGS for v in _config().values())


def default_language(platform: str | None) -> str:
    """What this channel's audience on `platform` reads. English when not configured
    (the old behaviour), so a fresh install changes nothing."""
    cfg = {str(k): str(v).lower() for k, v in _config().items()}
    lang = cfg.get(platform or "") or cfg.get("default")
    return lang if lang in LANGS else "english"


def defaults(platforms: list[str]) -> dict:
    return {p: default_language(p) for p in platforms}


def text_language(lines) -> str | None:
    """'hindi', 'hinglish' or 'english' for on-screen text; None when there is too
    little to judge (a single word, numbers, emoji)."""
    text = " ".join(x for x in (lines or []) if isinstance(x, str))
    if not text.strip():
        return None
    if len(_DEVANAGARI.findall(text)) >= 3:
        return "hindi"
    words = _WORD.findall(text.lower())
    if len(words) < 4:
        return None
    hits = sum(w in _HINGLISH for w in words)
    # one Hindi word in an English sentence ("Bro, paisa is everything") is still English to the reader
    return "hinglish" if hits >= 2 else "english"


def notes(platform: str, language: str, seen: dict | None, owner: str = "mine") -> dict:
    """{"video": [...], "copy": [...]} — what to change in the video, and a
    warning when the copy's language is not what this audience reads."""
    out = {"video": [], "copy": []}
    if not configured():
        return out
    want = default_language(platform)
    names = {"english": "English", "hindi": "Hindi", "hinglish": "Hinglish"}
    if language != want:
        out["copy"].append(
            f"This package is in {names.get(language, language)}, but your {platform.replace('_', ' ')} audience "
            f"reads {names[want]} (config/brand.yaml). Viewers swipe past copy in a language they do not read; "
            f"pick {names[want]} unless this post is meant for a different audience.")
    if owner == "mine" and seen:
        on_screen = text_language(seen.get("on_screen_text"))
        if on_screen and on_screen != want and not (want == "hinglish" and on_screen == "hindi"):
            out["video"].append(
                f"The text in the video is {names[on_screen]}, but your audience reads {names[want]}. The title "
                f"cannot fix that: viewers read the screen first. Re-edit the on-screen text in {names[want]} "
                f"before posting.")
    return out


# --- posting gap ----------------------------------------------------------------

_CACHE: dict[str, tuple[float, list]] = {}
_CACHE_SECONDS = 600       # several packages in a row cost one lookup


def _uploads(channel_id: str, fetch, now: float) -> list[dict]:
    hit = _CACHE.get(channel_id)
    if fetch is None and hit and now - hit[0] < _CACHE_SECONDS:
        return hit[1]
    from .trends import sources
    key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    recs = sources.fetch_youtube_api(channel_id, key, "", fetch or (lambda u: sources.http_get(u, timeout=15)))
    if fetch is None:
        _CACHE[channel_id] = (now, recs)
    return recs


def _growth(video_id: str, now: float) -> tuple[int, float] | None:
    """(views gained, over hours) between the two latest reads, from the follower."""
    from . import tracking
    try:
        with closing(tracking._db()) as conn:
            rows = conn.execute("SELECT ts, views FROM tracked_stats WHERE video_id=? AND views IS NOT NULL"
                                " ORDER BY ts DESC LIMIT 2", (video_id,)).fetchall()
    except Exception:  # noqa: BLE001
        return None
    if len(rows) < 2 or rows[0]["ts"] - rows[1]["ts"] < 3600 or now - rows[0]["ts"] > 12 * 3600:
        return None
    return rows[0]["views"] - rows[1]["views"], (rows[0]["ts"] - rows[1]["ts"]) / 3600


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts, _IST).strftime("%a %d %b, %H:%M IST").replace(" 0", " ")


def posting_gap(platform: str, fetch=None, now: float | None = None) -> list[str]:
    """A note when the channel's last Short is still being pushed. Shorts only:
    Instagram and Facebook do not show their numbers to the API."""
    now = time.time() if now is None else now
    if platform != "youtube_shorts" or not os.environ.get("YOUTUBE_API_KEY", "").strip():
        return []
    from . import tracking
    channel = tracking.own_channel()
    if not channel:
        return []
    try:
        recs = _uploads(channel, fetch, now)
    except Exception:  # noqa: BLE001 — a nice-to-have; never stops the package
        return []
    shorts = sorted((r for r in recs if r.get("kind") == "short" and r.get("published_at")
                     and r["published_at"] <= now + 300), key=lambda r: -r["published_at"])
    if not shorts:
        return []
    last = shorts[0]
    age_h = (now - last["published_at"]) / 3600
    if age_h >= GAP_HOURS:
        return []
    views = int(last["value"]) if last.get("value") is not None else None
    seen = f" ({views:,} views so far)" if views is not None else ""
    head = f"Your last Short “{last['title'][:70]}” went up {max(age_h, 0):.0f} h ago{seen}."
    if views is not None and age_h >= 6 and views < DEAD_VIEWS:
        return [head + " It was not picked up by the feed, so posting now will not take reach from it."]
    grew = _growth(last["ext_id"], now)
    if grew and grew[0] <= max((views or 0) * STILL_GROWING, 2):
        return [head + f" It gained only {grew[0]:,} views in the last {grew[1]:.0f} h, so it has stopped "
                       "being pushed and posting now will not take reach from it."]
    trend = f" It is still gaining (+{grew[0]:,} in {grew[1]:.0f} h)." if grew else ""
    return [head + trend + " For a channel this size YouTube usually pushes one Short at a time, so a second "
            f"one today competes with it for the same viewers. Post this one after {_when(last['published_at'] + GAP_HOURS * 3600)}, "
            "or once the last one stops gaining. (Scheduled and private uploads are not visible here.)"]
