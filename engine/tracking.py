"""Your own posts, followed after you publish them by hand.

Paste the link once. On every trend collection the engine re-reads the video's
public numbers (YouTube Data API, 1 unit a read) and compares its pace with
your channel's usual pace for the same kind of video (Short vs long). Linked to
the package it was posted with, this is how the engine learns what works for
THIS channel instead of guessing.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
from contextlib import closing
from datetime import datetime, timedelta, timezone

from . import analytics, media

FOLLOW_DAYS = 30            # a video's numbers are re-read for its first month
REREAD_HOURS = 6            # 30 videos x 4 reads a day = 120 quota units
BASELINE_HOURS = 24         # the channel's usual pace (3 units) once a day per video
MIN_FOR_LESSONS = 5         # below this, per-group numbers are anecdotes, not findings
_IST = timezone(timedelta(hours=5, minutes=30))

_SQL = """
CREATE TABLE IF NOT EXISTS tracked (
    video_id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    platform TEXT,
    package_id INTEGER,
    media_id TEXT,
    content_type TEXT,
    title TEXT,
    channel TEXT,
    channel_id TEXT,
    kind TEXT,
    published_at REAL,
    added_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tracked_stats (
    video_id TEXT NOT NULL,
    ts REAL NOT NULL,
    views INTEGER, likes INTEGER, comments INTEGER,
    channel_usual REAL,
    PRIMARY KEY (video_id, ts)
);
"""


def _db():
    conn = analytics.connect()
    conn.executescript(_SQL)
    return conn


# --- finding your own uploads without being told -----------------------------

DISCOVER_HOURS = 6          # the uploads list is checked this often (1 quota unit a check)
_CHANNEL_ID = re.compile(r"^UC[\w-]{22}$")
_HASHTAG = re.compile(r"#\w+")
_TOKEN = re.compile(r"[a-z0-9ऀ-ॿ]+")
_NOISE = {"shorts", "short", "reels", "reel", "the", "a", "an"}


def own_channel() -> str | None:
    """Your channel: MEDIA_ENGINE_YT_CHANNEL in .env, else the channel of the
    videos you followed by hand (the follower is for your own posts)."""
    env = os.environ.get("MEDIA_ENGINE_YT_CHANNEL", "").strip()
    if _CHANNEL_ID.match(env):
        return env
    with closing(_db()) as conn:
        row = conn.execute("SELECT channel_id, COUNT(*) AS n FROM tracked WHERE channel_id IS NOT NULL"
                           " GROUP BY channel_id ORDER BY n DESC, MAX(added_at) DESC LIMIT 1").fetchone()
    return row["channel_id"] if row and _CHANNEL_ID.match(row["channel_id"] or "") else None


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(_HASHTAG.sub(" ", (text or "").lower())) if t not in _NOISE]


def _similar(a: str, b: str) -> float:
    """1.0 when one text's words appear in order inside the other (a title pasted
    with a hashtag or emoji added), else the share of words in common."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    ja, jb = " ".join(ta), " ".join(tb)
    if len(min(ja, jb, key=len)) >= 8 and (ja in jb or jb in ja):
        return 1.0
    sa, sb = set(ta), set(tb)
    return len(sa & sb) / len(sa | sb)


def match_package(title: str, description: str, published_at: float | None) -> int | None:
    """The package a posted video was made from: written before it went up (up
    to a week before), for YouTube, whose title or description it carries."""
    if not published_at:
        return None
    from . import content
    with closing(content._db()) as conn:
        rows = conn.execute("SELECT id, created_at, package FROM content_packages WHERE platform LIKE 'youtube%'"
                            " AND created_at BETWEEN ? AND ? ORDER BY created_at DESC",
                            (published_at - 7 * 86400, published_at + 2 * 3600)).fetchall()
    best, best_score = None, 0.0
    lead = (description or "").split("\n\n")[0][:400]
    for r in rows:
        try:
            p = json.loads(r["package"])
        except (TypeError, ValueError):
            continue
        t = max((_similar(title, (x or {}).get("text", "")) for x in p.get("titles") or []), default=0.0)
        d = _similar(lead, p.get("description") or "") if lead else 0.0
        if t < 0.6 and d < 0.5:
            continue
        score = t + d
        if score > best_score + 1e-9:   # rows are newest first: a tie keeps the later package
            best, best_score = r["id"], score
    return best


def _meta(conn, key: str, value: float | None = None) -> float | None:
    conn.execute("CREATE TABLE IF NOT EXISTS tracking_meta (key TEXT PRIMARY KEY, value REAL)")
    if value is not None:
        conn.execute("INSERT OR REPLACE INTO tracking_meta (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
        return value
    row = conn.execute("SELECT value FROM tracking_meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def discover(fetch=None, now: float | None = None, force: bool = False) -> int:
    """Start following every upload on your channel from its first month, linked
    to the package it was posted with. Videos you followed by hand keep the
    package you picked. Returns how many were added; never raises."""
    now = time.time() if now is None else now
    key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    channel = own_channel()
    if not key or not channel:
        return 0
    with closing(_db()) as conn:
        last = _meta(conn, "discover_at")
        if not force and last and now - last < DISCOVER_HOURS * 3600:
            return 0
        _meta(conn, "discover_at", now)       # also on failure: a broken call is not retried every page view
        known = {r["video_id"] for r in conn.execute("SELECT video_id FROM tracked")}
    from .trends import sources
    get = fetch or (lambda u: sources.http_get(u, timeout=15))
    try:
        items = json.loads(get("https://www.googleapis.com/youtube/v3/playlistItems?" + urllib.parse.urlencode(
            {"part": "snippet", "maxResults": 25, "playlistId": "UU" + channel[2:], "key": key}))).get("items", [])
    except Exception:  # noqa: BLE001
        return 0
    added = 0
    for it in items:
        sn = it.get("snippet") or {}
        vid = (sn.get("resourceId") or {}).get("videoId")
        pub = sources._iso(sn.get("publishedAt"))
        if not vid or vid in known or not pub or now - pub > FOLLOW_DAYS * 86400:
            continue
        pkg = match_package(sn.get("title", ""), sn.get("description", ""), pub)
        try:
            track(f"https://www.youtube.com/watch?v={vid}", pkg, fetch, now)
            added += 1
        except Exception:  # noqa: BLE001 — a premiere or a just-deleted video is tried at the next check
            continue
    return added


def _package(package_id: int | None) -> dict:
    if not package_id:
        return {}
    from . import content
    with closing(content._db()) as conn:
        r = conn.execute("SELECT * FROM content_packages WHERE id=?", (package_id,)).fetchone()
    return dict(r) if r else {}


def track(url: str, package_id: int | None = None, fetch=None, now: float | None = None) -> dict:
    """Start following a posted video. Raises media.MediaError with a plain reason."""
    now = time.time() if now is None else now
    link = media.parse_link(url)
    if link["kind"] != "youtube":
        raise media.MediaError("Only YouTube videos and Shorts can be followed automatically. Instagram and "
                               "Facebook show their numbers only to the account owner.")
    if not os.environ.get("YOUTUBE_API_KEY", "").strip():
        raise media.MediaError("Following results needs YOUTUBE_API_KEY in .env.")
    try:
        facts = media.youtube_facts(link, fetch)
    except media.MediaError:
        raise media.MediaError("YouTube does not show this video publicly yet. If it is private or scheduled, "
                               "follow it again once it is public.") from None
    if not facts.get("numbers") or facts.get("views") is None:
        raise media.MediaError("YouTube does not show this video's numbers yet. If it is private or scheduled, "
                               "add it again once it is public.")
    pkg = _package(package_id)
    ctype = None
    if pkg.get("media_id"):
        seen = (media.load(pkg["media_id"]) or {}).get("seen") or {}
        ctype = seen.get("content_type")
    with closing(_db()) as conn:
        conn.execute("INSERT OR REPLACE INTO tracked (video_id, url, platform, package_id, media_id, content_type,"
                     " title, channel, channel_id, kind, published_at, added_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (link["id"], link["url"], link.get("platform") or "youtube", package_id or None,
                      pkg.get("media_id"), ctype, facts.get("title"), facts.get("channel"), facts.get("channel_id"),
                      facts.get("kind"), facts.get("published_at"), now))
        conn.commit()
    _read(link["id"], facts, fetch, now, baseline=True)
    return next(v for v in followed(now) if v["video_id"] == link["id"])


def _read(video_id: str, facts: dict, fetch, now: float, baseline: bool) -> None:
    usual = None
    if baseline:
        perf = media.performance(facts, video_id, fetch, now) or {}
        usual = perf.get("channel_usual")
    with closing(_db()) as conn:
        if usual is None:          # keep the last known baseline between daily refreshes
            row = conn.execute("SELECT channel_usual FROM tracked_stats WHERE video_id=? AND channel_usual IS NOT NULL"
                               " ORDER BY ts DESC LIMIT 1", (video_id,)).fetchone()
            usual = row["channel_usual"] if row else None
        conn.execute("INSERT OR REPLACE INTO tracked_stats (video_id, ts, views, likes, comments, channel_usual)"
                     " VALUES (?,?,?,?,?,?)", (video_id, now, facts.get("views"), facts.get("likes"),
                                               facts.get("comments"), usual))
        conn.commit()


def refresh(fetch=None, now: float | None = None) -> int:
    """Re-read every video still in its first month. Returns how many were read."""
    now = time.time() if now is None else now
    if not os.environ.get("YOUTUBE_API_KEY", "").strip():
        return 0
    discover(fetch, now)          # new uploads first; they are read fresh, so the loop below skips them
    with closing(_db()) as conn:
        rows = conn.execute(
            "SELECT t.video_id, t.url, t.published_at, MAX(s.ts) AS last,"
            " MAX(CASE WHEN s.channel_usual IS NOT NULL THEN s.ts END) AS last_base"
            " FROM tracked t LEFT JOIN tracked_stats s ON s.video_id = t.video_id GROUP BY t.video_id").fetchall()
    done = 0
    for r in rows:
        if r["published_at"] and now - r["published_at"] > FOLLOW_DAYS * 86400:
            continue
        if r["last"] and now - r["last"] < REREAD_HOURS * 3600:
            continue
        try:
            facts = media.youtube_facts(media.parse_link(r["url"]), fetch)
        except Exception:  # noqa: BLE001 — a deleted or private video just stops updating
            continue
        _read(r["video_id"], facts, fetch, now,
              baseline=not r["last_base"] or now - r["last_base"] >= BASELINE_HOURS * 3600)
        done += 1
    return done


def followed(now: float | None = None) -> list[dict]:
    """Every followed video with its latest numbers and pace."""
    now = time.time() if now is None else now
    out = []
    with closing(_db()) as conn:
        for t in conn.execute("SELECT * FROM tracked ORDER BY published_at DESC"):
            s = conn.execute("SELECT * FROM tracked_stats WHERE video_id=? ORDER BY ts DESC LIMIT 1",
                             (t["video_id"],)).fetchone()
            first = conn.execute("SELECT views, ts FROM tracked_stats WHERE video_id=? ORDER BY ts LIMIT 1",
                                 (t["video_id"],)).fetchone()
            row = dict(t)
            age_h = max(((s["ts"] if s else now) - (t["published_at"] or now)) / 3600, 1.0)
            if s:
                vph = s["views"] / age_h if s["views"] is not None else None
                # same window as the channel's usual (media.PACE_HOURS), so old and new compare fairly
                pace = s["views"] / media.pace_hours(t["kind"], age_h) if s["views"] is not None else None
                row.update(views=s["views"], likes=s["likes"], comments=s["comments"], read_at=s["ts"],
                           views_per_hour=round(vph) if vph is not None else None,
                           channel_usual=s["channel_usual"],
                           vs_usual=round(pace / s["channel_usual"], 1) if pace and s["channel_usual"] else None,
                           like_rate=round(s["likes"] / s["views"], 4) if s["likes"] is not None and s["views"] else None,
                           reads=conn.execute("SELECT COUNT(*) FROM tracked_stats WHERE video_id=?",
                                              (t["video_id"],)).fetchone()[0],
                           gained=(s["views"] - first["views"]) if first and s["views"] is not None and first["views"] is not None else None)
            row["age_days"] = round(age_h / 24, 1)
            if t["published_at"]:
                row["posted_hour_ist"] = datetime.fromtimestamp(t["published_at"], _IST).hour
            pkg = _package(t["package_id"])
            if pkg:
                p = json.loads(pkg["package"])
                row["writer"] = pkg.get("writer")
                row["language"] = pkg.get("language")
                row["package"] = {"platform": pkg["platform"], "topic": pkg["topic"],
                                  "suggested_title": ((p.get("titles") or [{}])[0] or {}).get("text")}
            if t["media_id"]:
                from . import audience
                seen = (media.load(t["media_id"]) or {}).get("seen") or {}
                row["text_language"] = audience.text_language(seen.get("on_screen_text"))
            out.append(row)
    return out


def _slot(hour: int) -> str:
    return ("Morning (6-11)" if 6 <= hour < 11 else "Midday (11-16)" if 11 <= hour < 16
            else "Evening (16-21)" if 16 <= hour < 21 else "Night (21-6)")


def lessons(now: float | None = None) -> dict:
    """What works for this channel, by kind of content and posting time.

    Ranked on pace against the channel's own usual for the same kind of video,
    so a Short is not compared with a long video. Groups under MIN_FOR_LESSONS
    are shown with their n and marked as too few."""
    rows = [r for r in followed(now) if r.get("vs_usual") is not None and (r.get("age_days") or 0) >= 2]
    out = {"measured": len(rows), "enough": len(rows) >= MIN_FOR_LESSONS, "groups": {}}

    def group(name, key):
        g: dict[str, list] = {}
        for r in rows:
            k = key(r)
            if k:
                g.setdefault(k, []).append(r["vs_usual"])
        out["groups"][name] = sorted(
            ({"key": k, "n": len(v), "median_vs_usual": sorted(v)[len(v) // 2], "few": len(v) < 3}
             for k, v in g.items()), key=lambda x: -x["median_vs_usual"])

    group("Kind of content", lambda r: (r.get("content_type") or "").replace("_", " ") or None)
    group("Posting time (IST)", lambda r: _slot(r["posted_hour_ist"]) if r.get("posted_hour_ist") is not None else None)
    group("Format", lambda r: {"short": "Shorts", "long": "Long videos", "live": "Live"}.get(r.get("kind")))
    # Which model's packages do better on THIS channel — the answer to "which one should I pick".
    group("Written by", lambda r: r.get("writer"))
    # The finding that started this (Nov 2026): English Shorts got 2-21 views, Hinglish 172-1,368.
    group("Copy language", lambda r: (r.get("language") or "").capitalize() or None)
    group("Text on the video", lambda r: (r.get("text_language") or "").capitalize() or None)
    return out
