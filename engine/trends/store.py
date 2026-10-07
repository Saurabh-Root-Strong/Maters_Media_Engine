"""trends.db — the trend tracker's own SQLite store.

Kept apart from the post analytics store (engine/analytics.py) on purpose:
this one is market-wide signal history, that one is your own posts' results.

  items         one row per thing seen (article, search query, video, wiki page)
  observations  a measured number for an item at a moment (search volume, views)
  topics        a group of items that are about the same story
  topic_states  every lifecycle change — the record that lets the calls be
                checked against what happened next
  topic_daily   one row per topic per day — the permanent history
  updates       the periodic digest (every `report_hours`): what was trending,
                the same, or fading, and how that moved since the last one
  runs          every collector run, so stale or failing sources are visible
"""

from __future__ import annotations

import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "trends.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    ext_id TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT,
    publisher TEXT,
    published_at REAL,
    first_seen REAL NOT NULL,
    topic_id INTEGER,
    UNIQUE (source, ext_id)
);
CREATE INDEX IF NOT EXISTS items_topic ON items (topic_id);
CREATE INDEX IF NOT EXISTS items_seen ON items (first_seen);

CREATE TABLE IF NOT EXISTS observations (
    item_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    value REAL NOT NULL,
    PRIMARY KEY (item_id, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS topics (
    id INTEGER PRIMARY KEY,
    tokens TEXT NOT NULL,          -- json {token: member count}
    n_items INTEGER NOT NULL,      -- members that contributed tokens
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    niche INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS topics_last ON topics (last_seen);

CREATE TABLE IF NOT EXISTS topic_states (
    id INTEGER PRIMARY KEY,
    topic_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    state TEXT NOT NULL,
    score REAL,
    velocity REAL
);
CREATE INDEX IF NOT EXISTS topic_states_topic ON topic_states (topic_id, ts);

CREATE TABLE IF NOT EXISTS topic_daily (
    topic_id INTEGER NOT NULL,
    day TEXT NOT NULL,             -- IST calendar day
    label TEXT,
    peak_score REAL,
    last_state TEXT,
    mentions INTEGER,
    sources TEXT,
    niche INTEGER,
    PRIMARY KEY (topic_id, day)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS topic_merges (
    old_id INTEGER PRIMARY KEY,    -- a topic id that was folded into another
    new_id INTEGER NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS updates (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    window_hours REAL NOT NULL,
    payload TEXT NOT NULL          -- json: every topic's class at this update
);
CREATE INDEX IF NOT EXISTS updates_ts ON updates (ts);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    started REAL NOT NULL,
    finished REAL,
    ok INTEGER NOT NULL,
    n_items INTEGER,
    error TEXT
);
CREATE INDEX IF NOT EXISTS runs_source ON runs (source, started);
"""


def connect() -> sqlite3.Connection:
    """A fresh connection per call site — sqlite connections must not be shared
    between the collector thread and request threads."""
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(_SCHEMA)
    # Added after the first release: what kind of video an item is
    # ("live" / "short" / "long"; NULL = unknown or not a video).
    if "kind" not in {r["name"] for r in conn.execute("PRAGMA table_info(items)")}:
        conn.execute("ALTER TABLE items ADD COLUMN kind TEXT")
    return conn


def upsert_item(conn, rec: dict, now: float) -> tuple[int, bool]:
    """Returns (item id, is_new). An existing item keeps its first_seen/topic."""
    row = conn.execute("SELECT id, kind FROM items WHERE source=? AND ext_id=?",
                       (rec["source"], rec["ext_id"])).fetchone()
    if row:
        # a live stream ends and becomes a recording: keep the kind current
        if rec.get("kind") and rec["kind"] != row["kind"]:
            conn.execute("UPDATE items SET kind=? WHERE id=?", (rec["kind"], row["id"]))
        return row["id"], False
    cur = conn.execute(
        "INSERT INTO items (source, ext_id, title, url, publisher, published_at, first_seen, kind)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (rec["source"], rec["ext_id"], rec["title"][:500], rec.get("url"),
         rec.get("publisher"), rec.get("published_at"), now, rec.get("kind")))
    return cur.lastrowid, True


def add_observation(conn, item_id: int, ts: float, value: float) -> None:
    conn.execute("INSERT OR REPLACE INTO observations (item_id, ts, value) VALUES (?,?,?)",
                 (item_id, ts, value))


def log_run(conn, source: str, started: float, finished: float, ok: bool,
            n_items: int, error: str = "") -> None:
    conn.execute("INSERT INTO runs (source, started, finished, ok, n_items, error)"
                 " VALUES (?,?,?,?,?,?)", (source, started, finished, int(ok), n_items, error[:500]))


def source_health(conn) -> dict[str, dict]:
    """Per source: when it last succeeded, and how the latest attempt went."""
    out: dict[str, dict] = {}
    for r in conn.execute(
            "SELECT source, MAX(CASE WHEN ok=1 THEN finished END) AS last_ok,"
            " MAX(started) AS last_try FROM runs GROUP BY source"):
        last = conn.execute("SELECT ok, error, n_items FROM runs WHERE source=? AND started=?",
                            (r["source"], r["last_try"])).fetchone()
        out[r["source"]] = {"last_ok": r["last_ok"], "last_try": r["last_try"],
                            "ok": bool(last["ok"]), "error": last["error"] or "",
                            "n_items": last["n_items"]}
    return out


def prune(conn, now: float, keep_days: int = 30) -> None:
    """Raw per-collection observations are bulky; topic_daily is the long history."""
    conn.execute("DELETE FROM observations WHERE ts < ?", (now - keep_days * 86400,))
    conn.execute("DELETE FROM runs WHERE started < ?", (now - keep_days * 86400,))
