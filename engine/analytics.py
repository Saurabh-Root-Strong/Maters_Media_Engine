"""analytics.db — your own posts and how they performed.

Separate from the trend store (engine/trends/store.py): that one records what
the market was talking about, this one records what YOU published and what it
earned. Joining the two is the point — every post is stamped with the trend
state its topic was in when it went out, so over time the numbers show whether
posting on "rising" really beats posting on "peaking" for this audience.

  posts         one row per platform post (dry-runs included, flagged as such)
  post_metrics  a snapshot of a post's numbers at a moment; several per post,
                so growth over the first hours/days is kept, not overwritten

Metrics are entered by hand for now — pulling them automatically needs each
platform's API credentials, which arrive with live posting.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import closing

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "analytics.db")

METRIC_FIELDS = ("impressions", "likes", "comments", "shares", "saves", "clicks")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    platform TEXT NOT NULL,
    topic TEXT NOT NULL,
    trend_topic_id INTEGER,
    trend_state TEXT,
    format_id TEXT,
    caption TEXT,
    hashtags TEXT,
    mode TEXT NOT NULL,            -- 'live' or 'dry-run'
    status TEXT NOT NULL,
    external_url TEXT,
    scheduled INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS posts_created ON posts (created_at);

CREATE TABLE IF NOT EXISTS post_metrics (
    id INTEGER PRIMARY KEY,
    post_id TEXT NOT NULL,
    captured_at REAL NOT NULL,
    impressions INTEGER, likes INTEGER, comments INTEGER,
    shares INTEGER, saves INTEGER, clicks INTEGER,
    source TEXT NOT NULL DEFAULT 'manual'
);
CREATE INDEX IF NOT EXISTS post_metrics_post ON post_metrics (post_id, captured_at);
"""


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    return conn


def record_post(platform: str, frozen: dict, result: dict, meta: dict | None = None,
                scheduled: bool = False) -> str | None:
    """Log a publish outcome. Only 'posted' and 'dry-run' are posts; skipped /
    blocked / errored attempts never reached an audience and are not logged."""
    status = result.get("status")
    if status not in ("posted", "dry-run"):
        return None
    meta = meta or {}
    draft = (frozen.get("drafts") or {}).get(platform) or {}
    post_id = uuid.uuid4().hex[:16]
    with closing(connect()) as conn:
        conn.execute(
            "INSERT INTO posts (id, created_at, platform, topic, trend_topic_id, trend_state,"
            " format_id, caption, hashtags, mode, status, external_url, scheduled)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (post_id, time.time(), platform, frozen.get("topic", ""),
             meta.get("trend_topic_id"), meta.get("trend_state"),
             (meta.get("formats") or {}).get(platform),
             draft.get("caption", ""), json.dumps(draft.get("hashtags", []), ensure_ascii=False),
             "live" if status == "posted" else "dry-run", status, result.get("url"),
             int(scheduled)))
        conn.commit()
    return post_id


def _num(v) -> int | None:
    """A metric value: non-negative whole number, or None when left blank."""
    if v is None or v == "" or isinstance(v, bool):
        return None
    try:
        n = int(float(v))
    except (TypeError, ValueError, OverflowError):
        raise ValueError("metrics must be numbers") from None
    if n < 0:
        raise ValueError("metrics cannot be negative")
    return n


def add_metrics(post_id: str, metrics: dict, source: str = "manual") -> bool:
    """Add one snapshot. Returns False if the post does not exist.
    Raises ValueError for bad numbers or a snapshot with nothing in it."""
    vals = {f: _num(metrics.get(f)) for f in METRIC_FIELDS}
    if all(v is None for v in vals.values()):
        raise ValueError("enter at least one number")
    with closing(connect()) as conn:
        if not conn.execute("SELECT 1 FROM posts WHERE id=?", (post_id,)).fetchone():
            return False
        conn.execute(
            f"INSERT INTO post_metrics (post_id, captured_at, {', '.join(METRIC_FIELDS)}, source)"  # noqa: S608
            f" VALUES (?,?,{','.join('?' * len(METRIC_FIELDS))},?)",
            (post_id, time.time(), *[vals[f] for f in METRIC_FIELDS], source))
        conn.commit()
    return True


def _engagement(m) -> int:
    return sum((m[f] or 0) for f in ("likes", "comments", "shares", "saves"))


def list_posts(limit: int = 30) -> list[dict]:
    with closing(connect()) as conn:
        out = []
        for p in conn.execute("SELECT * FROM posts ORDER BY created_at DESC LIMIT ?", (limit,)):
            m = conn.execute("SELECT * FROM post_metrics WHERE post_id=? ORDER BY captured_at DESC"
                             " LIMIT 1", (p["id"],)).fetchone()
            n = conn.execute("SELECT COUNT(*) FROM post_metrics WHERE post_id=?", (p["id"],)).fetchone()[0]
            out.append({
                "id": p["id"], "created_at": p["created_at"], "platform": p["platform"],
                "topic": p["topic"], "trend_state": p["trend_state"], "format_id": p["format_id"],
                "mode": p["mode"], "scheduled": bool(p["scheduled"]), "url": p["external_url"],
                "snapshots": n,
                "metrics": None if m is None else {
                    **{f: m[f] for f in METRIC_FIELDS}, "captured_at": m["captured_at"],
                    "engagement": _engagement(m),
                    "engagement_rate": (round(_engagement(m) / m["impressions"], 4)
                                        if m["impressions"] else None)},
            })
        return out


def summary() -> dict:
    """Average engagement rate of LIVE posts, by trend state / platform / format.

    Dry-runs never reached an audience and are excluded. Each post counts once,
    at its latest snapshot. `n` is shown with every average — a rate built on
    two posts is an anecdote, not a finding.
    """
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT p.platform, p.trend_state, p.format_id, m.impressions, m.likes, m.comments,"
            " m.shares, m.saves FROM posts p JOIN post_metrics m ON m.id = ("
            "   SELECT id FROM post_metrics WHERE post_id = p.id ORDER BY captured_at DESC LIMIT 1)"
            " WHERE p.mode = 'live' AND m.impressions > 0").fetchall()
        totals = conn.execute(
            "SELECT COUNT(*) AS n, SUM(mode='live') AS live FROM posts").fetchone()

    def group(key: str) -> list[dict]:
        buckets: dict[str, list[float]] = {}
        for r in rows:
            buckets.setdefault(r[key] or "—", []).append(_engagement(r) / r["impressions"])
        return sorted(({"key": k, "n": len(v), "avg_engagement_rate": round(sum(v) / len(v), 4)}
                       for k, v in buckets.items()), key=lambda d: -d["avg_engagement_rate"])

    return {"posts": totals["n"] or 0, "live_posts": totals["live"] or 0,
            "measured": len(rows), "by_trend_state": group("trend_state"),
            "by_platform": group("platform"), "by_format": group("format_id")}
