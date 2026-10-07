"""Group items into topics by shared wording.

A topic keeps a count of how many of its members used each word; its
"signature" is the words most members share. A new item joins the topic whose
signature it overlaps best, or starts a new one. Matching against the
signature — not every word ever seen — stops a topic snowballing into
unrelated stories.

Words are weighted by how much they identify a story:
  0.5  generic market words ("share", "ipo", "nifty")
  1.0  ordinary words
  2.0  rare words — in practice names ("jio", "hyfun", "trent") — found in
       only a handful of topics
so "Jio … IPO" joins the Jio story on the name alone, while two unrelated
headlines sharing "share price valuation" do not merge.
"""

from __future__ import annotations

import json
import math
from collections import Counter

from .text import GENERIC, is_niche, strip_publisher, tokens

ACTIVE_HOURS = 72          # a topic silent this long no longer absorbs new items
MIN_OVERLAP = 2.0          # weighted shared words needed
MIN_RATIO = 0.3            # ...and that share of the smaller side
RARE_SHARE = 0.02          # a word in <= 2% of active topics counts as a name
NICHE_SHARE = 0.4          # share of a topic's headlines that must be on-niche


def signature(counts: dict[str, int], n_items: int) -> set[str]:
    if n_items < 4:
        return set(counts)
    need = max(2, math.ceil(0.25 * n_items))
    return {t for t, c in counts.items() if c >= need}


def _base(tok: str) -> float:
    return 0.5 if tok in GENERIC else 1.0


def anchors(counts: dict[str, int], n_items: int) -> set[str]:
    """The topic's core: its most-used naming words. Empty while the topic is
    still forming (fewer than 4 members) — then any shared name will do."""
    if n_items < 4:
        return set()
    strong = sorted(((c, t) for t, c in counts.items() if t not in GENERIC), reverse=True)
    if not strong:
        return set()
    cut = strong[min(2, len(strong) - 1)][0]          # count of the 3rd most-used word
    return {t for c, t in strong if c >= max(cut, 2)}


def match_score(item: set[str], sig: set[str], w=None, core: set[str] | None = None) -> float:
    """0 = no match; otherwise higher = better.

    w    = token -> weight for the overlap bar (names count double).
    core = the topic's anchor words; when given, one of them must be shared,
           so an established story only absorbs items about its actual subject.
    """
    w = w or _base
    shared = item & sig
    if not shared or not (shared - GENERIC):
        return 0.0  # must share at least one word that actually names something
    # A bare 1-2 word title ("tata motors") matches when the topic covers all
    # of it — it can never reach the overlap bar on its own.
    if len(item) <= 2 and item <= sig:
        return 1.0
    if len(shared) < 2:
        return 0.0  # one shared word, however rare, is a coincidence
    if core and not (shared & core):
        return 0.0
    if sum(w(t) for t in shared) < MIN_OVERLAP:
        return 0.0
    # The ratio uses plain weights: boosting names here would inflate both
    # sides and reject every long headline.
    ratio = sum(map(_base, shared)) / max(min(sum(map(_base, item)), sum(map(_base, sig))), 1e-9)
    return ratio if ratio >= MIN_RATIO else 0.0


class TopicIndex:
    """Active topics held in memory for one assignment pass."""

    def __init__(self, conn, now: float):
        self.conn = conn
        self.topics: dict[int, dict] = {}
        self.df: Counter = Counter()   # in how many topics each word appears
        for r in conn.execute("SELECT * FROM topics WHERE last_seen >= ?",
                              (now - ACTIVE_HOURS * 3600,)):
            counts = json.loads(r["tokens"])
            self.topics[r["id"]] = {"counts": counts, "n": r["n_items"],
                                    "sig": signature(counts, r["n_items"]),
                                    "first_seen": r["first_seen"],
                                    "last_seen": r["last_seen"], "niche": r["niche"]}
            self.df.update(counts.keys())

    def weight(self, tok: str) -> float:
        if tok in GENERIC:
            return 0.5
        return 2.0 if self.df[tok] <= max(2, RARE_SHARE * len(self.topics)) else 1.0

    def best(self, item_tokens: set[str], niche_only: bool = False,
             covered: bool = False, skip: int | None = None) -> int | None:
        """covered=True: every item word must be in the topic (reference pages)."""
        best_id, best = None, 0.0
        for tid, t in self.topics.items():
            if tid == skip or (niche_only and not t["niche"]):
                continue
            if covered:
                s = 1.0 if item_tokens <= t["sig"] and item_tokens - GENERIC else 0.0
            else:
                s = match_score(item_tokens, t["sig"], self.weight, anchors(t["counts"], t["n"]))
            # ties -> the bigger topic, so results are stable
            if s > best or (s == best and s > 0 and t["n"] > self.topics[best_id]["n"]):
                best_id, best = tid, s
        return best_id

    def create(self, item_tokens: set[str], ts: float, niche: bool) -> int:
        counts = dict.fromkeys(sorted(item_tokens), 1)
        cur = self.conn.execute(
            "INSERT INTO topics (tokens, n_items, first_seen, last_seen, niche) VALUES (?,?,?,?,?)",
            (json.dumps(counts), 1, ts, ts, int(niche)))
        self.topics[cur.lastrowid] = {"counts": counts, "n": 1, "sig": set(counts),
                                      "first_seen": ts, "last_seen": ts, "niche": int(niche)}
        self.df.update(counts.keys())
        return cur.lastrowid

    def absorb(self, tid: int, item_tokens: set[str], ts: float, niche: bool,
               learn: bool = True) -> None:
        t = self.topics[tid]
        if learn:
            for tok in item_tokens:
                if tok not in t["counts"]:
                    self.df[tok] += 1
                t["counts"][tok] = t["counts"].get(tok, 0) + 1
            t["n"] += 1
            t["sig"] = signature(t["counts"], t["n"])
            # Items arrive in feed order, not time order: the topic is as old
            # as its EARLIEST member, or a day-old story looks brand new.
            t["first_seen"] = min(t["first_seen"], ts)
        t["last_seen"] = max(t["last_seen"], ts)
        t["niche"] = int(t["niche"] or niche)
        self.conn.execute(
            "UPDATE topics SET tokens=?, n_items=?, first_seen=?, last_seen=?, niche=? WHERE id=?",
            (json.dumps(t["counts"]), t["n"], t["first_seen"], t["last_seen"], t["niche"], tid))


def assign(conn, records: list[tuple[int, dict]], now: float,
           niche_extra: set[str] | None = None) -> int:
    """Attach each new (item_id, record) to a topic. Returns how many were attached.

    Order matters and is fixed: news first (it has the richest wording and
    defines the stories), then videos, then bare search queries, then Wikipedia
    pages — which only ever attach to an existing niche story, never start one.
    """
    order = {"news": 0, "youtube": 1, "gtrends": 2, "wiki": 3}
    index = TopicIndex(conn, now)
    attached = 0
    for item_id, rec in sorted(records, key=lambda p: (order.get(p[1]["source"], 9), p[0])):
        src = rec["source"]
        title_toks = tokens(strip_publisher(rec["title"]) if src == "news" else rec["title"])
        if not title_toks:
            continue
        ts = min(rec.get("published_at") or now, now)

        if src == "wiki":
            tid = index.best(title_toks, niche_only=True, covered=True)
            if tid is None:
                continue
            index.absorb(tid, title_toks, ts, False, learn=False)
        elif src == "gtrends":
            ctx = title_toks | tokens(rec.get("extra_text", ""))
            niche = is_niche(ctx, niche_extra)
            # Try the bare query first — the attached headlines are context and
            # can pull a query towards a loosely related story.
            tid = index.best(title_toks) or index.best(ctx)
            if tid is None:
                tid = index.create(ctx, ts, niche)
            else:
                index.absorb(tid, title_toks, ts, niche)
        else:
            # Neither feed is purely on-niche: the business news section also
            # carries airline incidents and car launches, and business channels
            # carry politics and sport. An item is in-niche only if its own
            # title says so; a topic is in-niche if any member is.
            niche = is_niche(title_toks, niche_extra)
            tid = index.best(title_toks)
            if tid is None:
                tid = index.create(title_toks, ts, niche)
            else:
                index.absorb(tid, title_toks, ts, niche)

        conn.execute("UPDATE items SET topic_id=? WHERE id=?", (tid, item_id))
        attached += 1
    merge_fragments(conn, index)
    refresh_niche(conn, index, niche_extra)
    return attached


def refresh_niche(conn, index: TopicIndex, niche_extra: set[str] | None = None) -> None:
    """Re-derive every active topic's niche flag from its members' titles.

    The flag is otherwise only ever set when an item arrives, so it would go
    stale whenever the keyword list changes — and a topic created under an
    older, looser rule would stay on the niche board forever.
    """
    total: dict[int, int] = dict.fromkeys(index.topics, 0)
    hits: dict[int, int] = dict.fromkeys(index.topics, 0)
    for r in conn.execute("SELECT topic_id, source, title FROM items WHERE topic_id IS NOT NULL"
                          " AND source IN ('news','youtube')"):
        tid = r["topic_id"]
        if tid in total:
            title = strip_publisher(r["title"]) if r["source"] == "news" else r["title"]
            total[tid] += 1
            hits[tid] += is_niche(tokens(title), niche_extra)
    for tid, n in total.items():
        if not n:
            # a search trend's niche came from its attached headlines, which
            # are not stored — leave a topic with no news/video member alone
            continue
        # A real markets story says so in a fair share of its headlines. One
        # stray finance word in a 12-video politics topic does not make it one.
        flag = int(hits[tid] / n >= NICHE_SHARE)
        if flag != index.topics[tid]["niche"]:
            index.topics[tid]["niche"] = flag
            conn.execute("UPDATE topics SET niche=? WHERE id=?", (flag, tid))


def merge_fragments(conn, index: TopicIndex) -> int:
    """Fold a story's fragments back together.

    Assignment is one item at a time, so the first two headlines of a story can
    be worded differently enough to start two topics that then both grow. Once
    their signatures have formed they recognisably overlap: each smaller topic
    is tested against the bigger ones as if it were a single item.
    """
    merged = 0
    by_size = sorted(index.topics, key=lambda i: (-index.topics[i]["n"], i))
    for small in reversed(by_size):                 # smallest first
        s = index.topics.get(small)
        if s is None or not s["sig"]:
            continue
        best_id, best = None, 0.0
        for big in by_size:
            b = index.topics.get(big)
            if b is None or big == small or (b["n"], -big) <= (s["n"], -small):
                continue                            # only ever merge into a bigger topic
            score = match_score(s["sig"], b["sig"], index.weight, anchors(b["counts"], b["n"]))
            if score > best:
                best_id, best = big, score
        if best_id is None:
            continue
        b = index.topics[best_id]
        for tok, c in s["counts"].items():
            b["counts"][tok] = b["counts"].get(tok, 0) + c
        b["n"] += s["n"]
        b["sig"] = signature(b["counts"], b["n"])
        b["first_seen"] = min(b["first_seen"], s["first_seen"])
        b["last_seen"] = max(b["last_seen"], s["last_seen"])
        b["niche"] = int(b["niche"] or s["niche"])
        conn.execute("UPDATE items SET topic_id=? WHERE topic_id=?", (best_id, small))
        conn.execute(
            "UPDATE topics SET tokens=?, n_items=?, first_seen=?, last_seen=?, niche=? WHERE id=?",
            (json.dumps(b["counts"]), b["n"], b["first_seen"], b["last_seen"], b["niche"], best_id))
        # Remember where it went: a later update must read "merged", not
        # "dropped off", for a topic that only changed its id.
        conn.execute("INSERT OR REPLACE INTO topic_merges (old_id, new_id) VALUES (?,?)", (small, best_id))
        conn.execute("UPDATE topic_merges SET new_id=? WHERE new_id=?", (best_id, small))
        # The fragment's own history described half a story; drop it.
        for table in ("topics", "topic_states", "topic_daily"):
            col = "id" if table == "topics" else "topic_id"
            conn.execute(f"DELETE FROM {table} WHERE {col}=?", (small,))  # noqa: S608 — fixed names
        del index.topics[small]
        merged += 1
    return merged
