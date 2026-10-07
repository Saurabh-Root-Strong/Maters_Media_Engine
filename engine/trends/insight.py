"""Topic insight — what the collected data says about ONE subject.

Given a few words describing a piece of content, look through the trend store
for everything about that subject and summarise it as evidence:

  now        the matching topic on the live board (rising / peaking / fading)
  videos     the watched channels' videos on it, ranked by views per hour —
             i.e. which titles are actually winning right now
  headlines  the most recent coverage and how many outlets carried it
  searches   what people type: live search suggestions + matching Google trends
  phrases    the word pairs used most across those titles and headlines
  daily      articles and videos per day, as far back as the store goes
  past       earlier days on which the subject was on the board

No LLM here. The only network call is the search-suggestion feed, which is
optional and fail-soft.
"""

from __future__ import annotations

import json
import time
import urllib.parse
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone

from . import sources, store
from .text import GENERIC, _STOP, _words, strip_publisher, tokens

_IST = timezone(timedelta(hours=5, minutes=30))
LOOKBACK_DAYS = 30
# Words that describe the video's FORMAT, not its subject.
_FORMAT_WORDS = {"explained", "explain", "explainer", "review", "analysis", "guide", "tutorial",
                 "complete", "detail", "hindi", "english", "video", "short", "reel", "episode",
                 "part", "breakdown", "simple", "simply", "beginner", "everything", "understand",
                 "understanding", "latest"}


def _channel_groups() -> dict[str, str]:
    """channel name -> 'creator' / 'news', from config/trends.yaml."""
    try:
        from .service import load_config
        return {ch.get("name", ""): ch.get("group", "news") for ch in load_config().get("youtube") or []}
    except Exception:  # noqa: BLE001
        return {}


def suggest(query: str, feed: str = "yt", geo: str = "IN", fetch=None) -> list[str]:
    """Live search suggestions for a query — what people actually type.
    feed='yt' = YouTube search, anything else = Google. [] on any failure."""
    # Up to four lookups in a row: a short timeout each, or a slow feed would
    # hold the whole analysis for well over a minute.
    fetch = fetch or (lambda url: sources.http_get(url, timeout=6))
    words = " ".join(query.split())[:80].split()
    if not words:
        return []
    # A full descriptive line ("jio ipo shareholder quota explained") is rarely
    # typed by anyone, so the feed returns nothing for it. Back off to shorter
    # prefixes until there is something — the subject is in the first words.
    tries = [words] + [words[:n] for n in (4, 3, 2) if n < len(words)]
    out: list[str] = []
    for attempt in tries:
        url = ("https://suggestqueries.google.com/complete/search?client=firefox&hl=en"
               f"&gl={urllib.parse.quote(geo)}{'&ds=yt' if feed == 'yt' else ''}"
               f"&q={urllib.parse.quote(' '.join(attempt))}")
        try:
            data = json.loads(fetch(url))
            found = [s for s in data[1] if isinstance(s, str)]
        except Exception:  # noqa: BLE001 — a nice-to-have; never fail the analysis over it
            found = []
        out += [s for s in found if s not in out]
        if len(out) >= 8:
            break
    return out[:12]


def _matcher(query: str):
    """-> (query tokens, predicate over an item's token set).

    The query is a creator's one-line description ("Jio IPO shareholder quota
    explained"): a subject plus an angle. Demanding most of its words would
    find only near-identical titles and miss the subject's wider coverage. So a
    title matches when it shares a NAMING word plus one more query word
    (weight 1.5: "jio" + "ipo"), which keeps "Jio recharge plans" and
    "Reliance shareholder meeting" out. Generic market words alone never match.
    """
    q = tokens(query) - _FORMAT_WORDS
    strong = q - GENERIC
    if strong:
        need = min(1.5, sum(0.5 if t in GENERIC else 1.0 for t in q))
        return q, lambda toks: (bool(toks & strong)
                                and sum(0.5 if t in GENERIC else 1.0 for t in toks & q) >= need)
    need = max(2, len(q))            # an all-generic query ("nifty today") must match in full
    return q, lambda toks: len(toks & q) >= need


def analyse(query: str, now: float | None = None, board: dict | None = None,
            suggest_feed: str | None = None, geo: str = "IN", fetch=None) -> dict:
    """Evidence about `query` from the trend store. `board` = the current trend
    board (to read the live state from); `suggest_feed` = 'yt'/'web' to also
    pull live search suggestions, None to skip the network entirely."""
    now = time.time() if now is None else now
    q_tokens, is_match = _matcher(query)
    since = now - LOOKBACK_DAYS * 86400
    out = {"query": query, "matched": 0, "now": None, "videos": [], "headlines": [],
           "searches": [], "trending_searches": [], "phrases": [], "daily": [], "past": [],
           "days_of_history": 0, "searchable": bool(q_tokens)}

    with closing(store.connect()) as conn:
        first = conn.execute("SELECT MIN(first_seen) FROM items").fetchone()[0]
        out["days_of_history"] = round((now - first) / 86400, 1) if first else 0
    if not q_tokens:
        # Only emoji, punctuation or filler words: there is nothing to look up.
        return out

    with closing(store.connect()) as conn:
        rows = conn.execute(
            "SELECT id, source, title, url, publisher, published_at, first_seen, topic_id FROM items"
            " WHERE first_seen >= ? AND source IN ('news','youtube','gtrends')", (since,)).fetchall()
        hits = [r for r in rows
                if is_match(tokens(strip_publisher(r["title"]) if r["source"] == "news" else r["title"]))]
        out["matched"] = len(hits)

        # --- videos: which titles are winning ---
        # Raw views/hour favours big channels: a TV channel's routine clip
        # beats a creator's breakout. So each video is also measured against
        # its OWN channel's usual pace (median views/hour of its tracked
        # uploads) — the "outlier" figure creators use to spot what is working.
        groups = _channel_groups()
        from .service import channel_pace, outlier
        vid_rows = conn.execute(
            "SELECT id, source, publisher, published_at, kind FROM items"
            " WHERE source='youtube' AND published_at IS NOT NULL AND first_seen >= ?", (since,)).fetchall()
        vid_obs: dict[int, list] = {}
        for r in conn.execute("SELECT o.item_id, o.ts, o.value FROM observations o JOIN items i ON i.id=o.item_id"
                              " WHERE i.source='youtube' AND i.first_seen >= ? ORDER BY o.ts", (since,)):
            vid_obs.setdefault(r["item_id"], []).append((r["ts"], r["value"]))
        pace = channel_pace(vid_rows, vid_obs)      # per channel AND kind (live / short / long)
        kinds = {r["id"]: r["kind"] for r in vid_rows}
        for r in (h for h in hits if h["source"] == "youtube"):
            series = vid_obs.get(r["id"])
            if not series or not r["published_at"]:
                continue
            age_h = max((series[-1][0] - r["published_at"]) / 3600, 0.5)
            row = {"id": r["id"], "source": "youtube", "publisher": r["publisher"],
                   "published_at": r["published_at"], "kind": kinds.get(r["id"])}
            ratio = outlier(row, vid_obs, pace)
            out["videos"].append({
                "title": r["title"], "channel": r["publisher"], "url": r["url"],
                "views": int(series[-1][1]), "age_hours": round(age_h, 1),
                "views_per_hour": round(series[-1][1] / age_h), "kind": kinds.get(r["id"]) or "long",
                "outlier": None if ratio is None else round(ratio, 1),
                "creator": groups.get(r["publisher"]) == "creator",
                "title_chars": len(r["title"])})
        # creators first (they pick topics by demand), then by how far the
        # video beats its channel's normal, then raw pace
        out["videos"].sort(key=lambda v: (not v["creator"], -(v["outlier"] or 0), -v["views_per_hour"]))
        out["videos"] = out["videos"][:8]

        # --- headlines ---
        news = sorted((h for h in hits if h["source"] == "news"),
                      key=lambda h: -(h["published_at"] or h["first_seen"]))
        out["publishers"] = len({h["publisher"] for h in news if h["publisher"]})
        out["headlines"] = [{"title": strip_publisher(h["title"]), "publisher": h["publisher"],
                             "url": h["url"], "at": h["published_at"] or h["first_seen"]}
                            for h in news[:8]]

        # --- trending searches that match (Google "trending now") ---
        for r in (h for h in hits if h["source"] == "gtrends"):
            last = conn.execute("SELECT value FROM observations WHERE item_id=? ORDER BY ts DESC LIMIT 1",
                                (r["id"],)).fetchone()
            out["trending_searches"].append({"query": r["title"],
                                             "searches": int(last["value"]) if last else None})

        # --- word pairs in use ---
        pairs: Counter = Counter()
        for h in hits:
            words = [w.strip("'-&") for w in
                     _words((strip_publisher(h["title"]) if h["source"] == "news" else h["title"]).lower())]
            words = [w for w in words if len(w) > 1 and not w.isdigit()]
            for a, b in zip(words, words[1:]):
                if a not in _STOP and b not in _STOP:
                    pairs[f"{a} {b}"] += 1
        out["phrases"] = [{"phrase": p, "count": c} for p, c in pairs.most_common(12) if c >= 2]

        # --- per-day volume: the subject's own history ---
        per_day: dict[str, dict] = {}
        for h in hits:
            if h["source"] == "gtrends":
                continue
            ts = h["published_at"] if h["published_at"] and h["published_at"] <= now else h["first_seen"]
            day = datetime.fromtimestamp(ts, _IST).strftime("%Y-%m-%d")
            d = per_day.setdefault(day, {"day": day, "articles": 0, "videos": 0})
            d["articles" if h["source"] == "news" else "videos"] += 1
        out["daily"] = [per_day[d] for d in sorted(per_day)][-LOOKBACK_DAYS:]

        # --- the topic it belongs to: live state + earlier days on the board ---
        topic_ids = Counter(h["topic_id"] for h in hits if h["topic_id"] is not None)
        top_ids = [tid for tid, _ in topic_ids.most_common(3)]
        if top_ids:
            marks = ",".join("?" * len(top_ids))
            out["past"] = [dict(r) for r in conn.execute(
                f"SELECT day, label, last_state AS state, mentions FROM topic_daily"  # noqa: S608
                f" WHERE topic_id IN ({marks}) ORDER BY day DESC LIMIT 30", top_ids)]

    if board and top_ids:
        entries = {e["id"]: e for rows_ in board.get("states", {}).values() for e in rows_}
        live = next((entries[t] for t in top_ids if t in entries), None)
        if live:
            out["now"] = {"id": live["id"], "label": live["label"], "state": live["state"],
                          "velocity_pct": live["velocity_pct"], "signals": live["signals"],
                          "sources": live["sources"], "window_hours": board.get("window_hours")}

    if suggest_feed:
        # Suggestions for the query itself — the wording as the creator typed it.
        out["searches"] = suggest(query, suggest_feed, geo, fetch)
        out["suggest_feed"] = "YouTube search" if suggest_feed == "yt" else "Google search"
    return out
