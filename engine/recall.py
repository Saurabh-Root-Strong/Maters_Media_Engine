"""The memory engine — what the system has learned from its own history.

Storing things is not memory. This module is the part that reads the stores
back and turns them into something the next decision can use:

  grade_calls()      Every "rising / peaking / fading" call the trend board made
                     is checked, one window later, against what the topic
                     actually did. scorecard() reports how often each kind of
                     call was right — next to the base rate, because a call
                     that is right 60% of the time is worthless if any topic
                     picked at random does the same.
  snapshot_videos()  Keeps each tracked video's final pace permanently (raw
                     view readings are pruned after 30 days).
  title_patterns()   From those videos: which title traits go with a creator
                     beating their own usual pace. Reported with sample sizes
                     and only called reliable when both sides have enough.
  overview()         Everything above, plus what was researched and made, for
                     the Memory page.

Nothing here calls a model. Every number can be traced to rows in the stores.
"""

from __future__ import annotations

import re
import time
from collections import defaultdict
from contextlib import closing

from .trends import service, store

GRADED_STATES = ("EMERGING", "RISING", "PEAKING", "FADING")
GREW, HELD, DROPPED, GONE = "grew", "held", "dropped", "gone"
# What each call PROMISES. A call is "right" when the next window delivered it.
PROMISE = {
    "EMERGING": {GREW, HELD},      # still worth posting on a window later
    "RISING": {GREW, HELD},
    "PEAKING": {HELD},             # neither took off nor collapsed
    "FADING": {DROPPED, GONE},
}
MIN_VIDEO_AGE_H = 24               # a video's pace means little before a day has passed
RELIABLE_N = 25                    # videos per side, before a title pattern can be called reliable
RELIABLE_P = 0.01                  # and the difference must clear this — strict, because a dozen
                                   # traits are tested at once and one in twenty would pass 0.05 by luck

_SCHEMA = """
CREATE TABLE IF NOT EXISTS call_grades (
    call_id INTEGER PRIMARY KEY,   -- topic_states.id
    topic_id INTEGER NOT NULL,
    state TEXT NOT NULL,
    called_at REAL NOT NULL,
    window_hours REAL NOT NULL,
    graded_at REAL NOT NULL,
    change_pct REAL,               -- the topic's share, next window vs the call's window
    outcome TEXT NOT NULL          -- grew | held | dropped | gone
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS video_facts (
    item_id INTEGER PRIMARY KEY,
    channel TEXT, grp TEXT, kind TEXT, title TEXT,
    published_at REAL, views REAL, age_hours REAL,
    outlier REAL,                  -- views/hour vs the channel's median for the same kind
    updated REAL
) WITHOUT ROWID;
"""


def _connect():
    conn = store.connect()
    conn.executescript(_SCHEMA)
    return conn


# --- 1. grading the trend calls -------------------------------------------------

def grade_calls(now: float | None = None, cfg: dict | None = None, limit: int = 500) -> int:
    """Grade every call old enough to have an outcome. Returns how many were graded.

    The outcome is read from a fresh scoring of the board AS OF one window
    after the call, so it uses exactly the measure the call itself was made on
    (share of voice, this window against the previous one). The call is never
    re-derived — it is the row written at the time, so there is no hindsight
    in what is being graded.
    """
    now = time.time() if now is None else now
    cfg = cfg or service.load_config()
    with closing(_connect()) as conn:
        due = conn.execute(
            "SELECT s.id, s.topic_id, s.ts, s.state, s.window_hours FROM topic_states s"
            " LEFT JOIN call_grades g ON g.call_id = s.id"
            " WHERE g.call_id IS NULL AND s.window_hours IS NOT NULL"
            f" AND s.state IN ({','.join('?' * len(GRADED_STATES))})"
            " AND s.ts + s.window_hours * 3600 <= ? ORDER BY s.ts LIMIT ?",
            (*GRADED_STATES, now, limit)).fetchall()
        moved = {r["old_id"]: r["new_id"] for r in conn.execute("SELECT old_id, new_id FROM topic_merges")}
    if not due:
        return 0

    # One board per (hour, window): calls made in the same cycle share a scoring.
    groups: dict[tuple, list] = defaultdict(list)
    for r in due:
        groups[(int(r["ts"] // 3600), r["window_hours"])].append(r)
    graded = []
    for (_, hours), calls in groups.items():
        when = max(c["ts"] for c in calls) + hours * 3600
        board = service.compute(now=when, cfg=cfg, window=(hours, 2), per_state=100_000)
        later = {e["id"]: e for rows in board["states"].values() for e in rows}
        for c in calls:
            e = later.get(moved.get(c["topic_id"], c["topic_id"]))
            if e is None:
                outcome, change = GONE, None            # no longer active enough to be on the board
            elif e["velocity_pct"] is None:
                # nothing to compare against in the call's own window: it can
                # only have appeared since, i.e. grown from nothing
                outcome, change = (GREW if e["score"] > 0 else GONE), None
            else:
                change = e["velocity_pct"]
                outcome = GREW if change >= 30 else DROPPED if change <= -30 else HELD
            graded.append((c["id"], c["topic_id"], c["state"], c["ts"], hours, now, change, outcome))
    with closing(_connect()) as conn:
        conn.executemany("INSERT OR REPLACE INTO call_grades (call_id, topic_id, state, called_at,"
                         " window_hours, graded_at, change_pct, outcome) VALUES (?,?,?,?,?,?,?,?)", graded)
        conn.commit()
    return len(graded)


def scorecard() -> dict:
    """How the calls have done, per state, against the base rate."""
    with closing(_connect()) as conn:
        rows = conn.execute("SELECT state, outcome, COUNT(*) AS n FROM call_grades GROUP BY 1, 2").fetchall()
        pending = conn.execute(
            "SELECT COUNT(*) FROM topic_states s LEFT JOIN call_grades g ON g.call_id = s.id"
            " WHERE g.call_id IS NULL AND s.window_hours IS NOT NULL").fetchone()[0]
        ungradable = conn.execute("SELECT COUNT(*) FROM topic_states WHERE window_hours IS NULL").fetchone()[0]
        span = conn.execute("SELECT MIN(called_at), MAX(called_at) FROM call_grades").fetchone()
    by_state: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    pooled: dict[str, int] = defaultdict(int)
    for r in rows:
        by_state[r["state"]][r["outcome"]] += r["n"]
        pooled[r["outcome"]] += r["n"]
    total = sum(pooled.values())
    states = []
    for state in GRADED_STATES:
        got = by_state.get(state, {})
        n = sum(got.values())
        if not n:
            states.append({"state": state, "n": 0})
            continue
        right = sum(got.get(o, 0) for o in PROMISE[state])
        # Base rate: how often the SAME outcome happens to any called topic.
        # The call only carries information if it beats this.
        base = sum(pooled.get(o, 0) for o in PROMISE[state]) / total
        states.append({
            "state": state, "n": n, "right_pct": round(100 * right / n),
            "base_pct": round(100 * base), "lift_pts": round(100 * (right / n - base)),
            "promise": sorted(PROMISE[state]),
            "outcomes": {o: round(100 * got.get(o, 0) / n) for o in (GREW, HELD, DROPPED, GONE)},
            # under ~30 graded calls a percentage moves several points per call
            "reliable": n >= 30,
        })
    return {"graded": total, "pending": pending, "ungradable_old": ungradable, "states": states,
            "from": span[0], "to": span[1]}


# --- 2. video facts + title patterns --------------------------------------------

def snapshot_videos(now: float | None = None, cfg: dict | None = None) -> int:
    """Keep each tracked video's latest pace and outlier permanently."""
    now = time.time() if now is None else now
    cfg = cfg or service.load_config()
    groups = {ch.get("name", ""): ch.get("group", "news") for ch in cfg.get("youtube") or []}
    with closing(_connect()) as conn:
        items = conn.execute("SELECT id, source, title, publisher, published_at, kind FROM items"
                             " WHERE source='youtube' AND published_at IS NOT NULL AND first_seen >= ?",
                             (now - 8 * 86400,)).fetchall()
        obs: dict[int, list] = defaultdict(list)
        for r in conn.execute("SELECT o.item_id, o.ts, o.value FROM observations o JOIN items i ON i.id = o.item_id"
                              " WHERE i.source='youtube' AND o.ts >= ? ORDER BY o.item_id, o.ts",
                              (now - 8 * 86400,)):
            obs[r["item_id"]].append((r["ts"], r["value"]))
        pace = service.channel_pace(items, obs)
        rows = []
        for it in items:
            series = obs.get(it["id"])
            if not series:
                continue
            ts, views = series[-1]
            ratio = service.outlier(it, obs, pace)
            rows.append((it["id"], it["publisher"], groups.get(it["publisher"], "news"), it["kind"] or "long",
                         it["title"], it["published_at"], views, (ts - it["published_at"]) / 3600,
                         ratio, now))
        conn.executemany("INSERT OR REPLACE INTO video_facts (item_id, channel, grp, kind, title, published_at,"
                         " views, age_hours, outlier, updated) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        conn.commit()
    return len(rows)


_EMOJI = re.compile(r"[\U0001F000-\U0001FAFF☀-➿]")
_FEATURES = {
    "asks a question": lambda t: "?" in t,
    "has a number": lambda t: bool(re.search(r"\d", t)),
    "names a rupee amount": lambda t: bool(re.search(r"₹|\bRs\.?\s*\d", t, re.I)),
    "has a percentage": lambda t: "%" in t,
    "over 60 characters": lambda t: len(t) > 60,
    "under 40 characters": lambda t: len(t) < 40,
    "Hindi script": lambda t: bool(re.search(r"[ऀ-ॿ]", t)),
    "has an emoji": lambda t: bool(_EMOJI.search(t)),
    "has a SHOUTED word": lambda t: bool(re.search(r"\b[A-Z]{4,}\b", t)),
    "has a hashtag": lambda t: "#" in t,
    "starts with Why / How / What": lambda t: bool(re.match(r"\s*(why|how|what|kya|kaise|kyun)\b", t, re.I)),
    "uses a warning word": lambda t: bool(re.search(r"\b(crash|danger|trap|beware|warning|scam|risk|alert)\b", t, re.I)),
}


def _rank_test(a: list[float], b: list[float]) -> float:
    """Two-sided Mann-Whitney U p-value (normal approximation, tie-corrected).
    Rank-based on purpose: a single video at 14x its channel's pace would
    carry a comparison of means on its own."""
    import math
    n1, n2 = len(a), len(b)
    pooled = sorted([(v, 0) for v in a] + [(v, 1) for v in b])
    ranks, ties, i = [0.0] * len(pooled), 0.0, 0
    while i < len(pooled):
        j = i
        while j + 1 < len(pooled) and pooled[j + 1][0] == pooled[i][0]:
            j += 1
        for k in range(i, j + 1):
            ranks[k] = (i + j) / 2 + 1
        t = j - i + 1
        ties += t ** 3 - t
        i = j + 1
    r1 = sum(r for r, (_, g) in zip(ranks, pooled) if g == 0)
    u = r1 - n1 * (n1 + 1) / 2
    n = n1 + n2
    var = n1 * n2 / 12 * ((n + 1) - ties / (n * (n - 1)))
    if var <= 0:
        return 1.0
    z = (abs(u - n1 * n2 / 2) - 0.5) / math.sqrt(var)
    return max(0.0, min(1.0, math.erfc(z / math.sqrt(2))))


def _median(vals: list[float]) -> float:
    vals = sorted(vals)
    return vals[len(vals) // 2]


def title_patterns(group: str = "creator") -> dict:
    """Which title traits go with a video beating its own channel's usual pace.

    Compared on `outlier` — pace relative to that channel's median for the same
    kind of video — so a big channel does not make its habits look like the
    cause. This is association, not proof: a creator may simply put questions
    on their best material.
    """
    with closing(_connect()) as conn:
        rows = conn.execute("SELECT title, outlier FROM video_facts WHERE grp=? AND outlier IS NOT NULL"
                            " AND age_hours >= ?", (group, MIN_VIDEO_AGE_H)).fetchall()
    out = []
    for name, has in _FEATURES.items():
        yes = [r["outlier"] for r in rows if has(r["title"])]
        no = [r["outlier"] for r in rows if not has(r["title"])]
        if len(yes) < 3 or len(no) < 3:
            continue
        with_, without = _median(yes), _median(no)
        if without <= 0:
            continue
        p_value = _rank_test(yes, no)
        out.append({"trait": name, "n_with": len(yes), "n_without": len(no),
                    "with": round(with_, 2), "without": round(without, 2),
                    "effect": round(with_ / without, 2), "p": round(p_value, 4),
                    # enough videos on both sides AND unlikely to be chance
                    "reliable": len(yes) >= RELIABLE_N and len(no) >= RELIABLE_N and p_value < RELIABLE_P})
    out.sort(key=lambda p: -abs(p["effect"] - 1))
    return {"videos": len(rows), "group": group, "patterns": out,
            "needs": {"per_side": RELIABLE_N, "p_below": RELIABLE_P}}


def lessons(group: str = "creator") -> list[str]:
    """The reliable patterns as sentences, for the writer. Empty until the
    evidence is there — a guess dressed up as a lesson is worse than none."""
    out = []
    for p in title_patterns(group)["patterns"]:
        if not p["reliable"] or 0.8 < p["effect"] < 1.25:
            continue
        direction = "outrun" if p["effect"] > 1 else "trail"
        out.append(f"Among the watched creators, titles that {_verb(p['trait'])} {direction} the rest: "
                   f"{p['with']}x their channel's usual pace against {p['without']}x "
                   f"({p['n_with']} vs {p['n_without']} videos).")
    return out[:4]


def _verb(trait: str) -> str:
    return {"asks a question": "ask a question", "has a number": "carry a number",
            "names a rupee amount": "name a rupee amount", "has a percentage": "carry a percentage",
            "over 60 characters": "run over 60 characters", "under 40 characters": "stay under 40 characters",
            "Hindi script": "are in Hindi script", "has an emoji": "carry an emoji",
            "has a SHOUTED word": "shout a word in capitals", "has a hashtag": "carry a hashtag",
            "starts with Why / How / What": "open with Why / How / What",
            "uses a warning word": "use a warning word (crash, trap, risk…)"}.get(trait, trait)


# --- overview --------------------------------------------------------------------

def overview() -> dict:
    with closing(_connect()) as conn:
        first = conn.execute("SELECT MIN(started) FROM runs").fetchone()[0]
        size = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: S608 — fixed names
                for t in ("items", "topics", "topic_states", "topic_daily", "updates", "video_facts")}
        days = conn.execute("SELECT COUNT(DISTINCT day) FROM topic_daily").fetchone()[0]
    return {"scorecard": scorecard(), "patterns": title_patterns("creator"),
            "lessons": lessons("creator"), "size": size, "days": days,
            "collecting_since": first}
