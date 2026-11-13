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
import time
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
                row.update(views=s["views"], likes=s["likes"], comments=s["comments"], read_at=s["ts"],
                           views_per_hour=round(vph) if vph is not None else None,
                           channel_usual=s["channel_usual"],
                           vs_usual=round(vph / s["channel_usual"], 1) if vph and s["channel_usual"] else None,
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
                row["package"] = {"platform": pkg["platform"], "topic": pkg["topic"],
                                  "suggested_title": ((p.get("titles") or [{}])[0] or {}).get("text")}
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
    return out
