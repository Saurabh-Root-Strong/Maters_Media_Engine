"""Trend service: collect -> group into topics -> score lifecycle -> board.

`collect()` and `compute()` are plain functions (a timestamp and a fetch
function can be injected, so tests replay saved payloads at a fixed clock).
`TrendService` wraps them in a background thread for the dashboard.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone

import yaml

from . import lifecycle, sources, store, topics
from .text import niche_extra, set_channel_noise, strip_publisher, tokens, weight

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "config", "trends.yaml")
_IST = timezone(timedelta(hours=5, minutes=30))

HORIZON_H = 96                   # how far back the board looks
MAX_ITEM_AGE_H = 96              # ignore news older than this at first sight
YT_TRACK_DAYS = 7                # stop re-measuring a video's views after this
YT_BACKFILL_H = 48               # a video first seen within this of its upload: views since upload are known
PER_STATE = 12
CREATOR_HOT = 1.5                # a creator video at >= 1.5x its channel's usual pace is "running hot"
PULSE_HOURS = 72                 # creator pulse: videos uploaded in the last 3 days
OUTLET_CAP = 2                   # one outlet's articles on one story, per window, that count
# Machine-generated "headlines" that are a feed id, not a story, e.g.
# "News by CNBC TV18 on TradingView, 2026-10-06 — cnbctv:e7dda5237094b:0".
_JUNK_TITLE = re.compile(r"\b\w+:[0-9a-f]{8,}\b|^news by .+ on tradingview", re.I)
SOURCE_LABEL = {"news": "Google News", "gtrends": "Google Trends", "youtube": "YouTube",
                "wiki": "Wikipedia"}

_DEFAULTS = {"interval_minutes": 30, "window_hours": 24, "report_hours": 24, "window_options": [24, 48],
             "geo": "IN", "news": [{"topic": "BUSINESS"}], "youtube": [],
             "niche_keywords": [], "weights": {"news": 1.0, "gtrends": 1.0, "youtube": 1.0},
             "lifecycle": {}}


def load_config() -> dict:
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            user = yaml.safe_load(f) or {}
    except OSError:
        user = {}
    cfg = {**_DEFAULTS, **{k: v for k, v in user.items() if v is not None}}
    cfg["weights"] = {**_DEFAULTS["weights"], **(cfg.get("weights") or {})}
    cfg["interval_minutes"] = max(5, float(cfg.get("interval_minutes") or 30))
    set_channel_noise(ch.get("name") for ch in cfg.get("youtube") or [])
    return cfg


# =============================== collect =======================================

FEED_TRIES = 3                   # YouTube's free feed answers 404/500 at random — try again


def _with_retries(call, tries: int = FEED_TRIES, pause: float = 1.5):
    """Call `call()` until it succeeds or `tries` run out (the last error is
    raised). Measured 2026-10-07: the same channel feed returned 404, 404, then
    200 seconds apart, so one failed attempt means nothing."""
    for attempt in range(tries):
        try:
            return call()
        except Exception:  # noqa: BLE001
            if attempt == tries - 1:
                raise
            time.sleep(pause * (attempt + 1))
    return None

def collect(now: float | None = None, cfg: dict | None = None, fetch=sources.http_get) -> dict:
    """One collection pass over every source. Never raises: each source is
    isolated and its outcome logged, so one dead feed cannot stop the others."""
    # With an injected clock, run records use it too — otherwise staleness
    # would be judged against a different clock than the data.
    tick = time.time if now is None else (lambda: now)
    now = tick()
    cfg = cfg or load_config()
    geo = cfg["geo"]
    extra = niche_extra(cfg.get("niche_keywords"))
    blocked = {str(p).strip().lower() for p in cfg.get("ignore_publishers") or []}
    summary: dict[str, dict] = {}

    with closing(store.connect()) as conn:
        fresh: list[tuple[int, dict]] = []

        def run(source: str, job) -> None:
            started = tick()
            try:
                n, err = job()
                ok = True
            except Exception as exc:  # noqa: BLE001 — a source failing is data, not a crash
                n, err, ok = 0, f"{type(exc).__name__}: {exc}", False
            store.log_run(conn, source, started, tick(), ok, n, err)
            conn.commit()
            summary[source] = {"ok": ok, "items": n, "error": err}

        def take(recs: list[dict], observe: bool) -> int:
            for rec in recs:
                item_id, is_new = store.upsert_item(conn, rec, now)
                if is_new:
                    fresh.append((item_id, rec))
                if observe and rec.get("value") is not None:
                    store.add_observation(conn, item_id, now, rec["value"])
            return len(recs)

        def gtrends_job():
            return take(sources.parse_gtrends(fetch(sources.gtrends_url(geo))), True), ""

        def news_job():
            n, failed = 0, []
            feeds = cfg.get("news") or []
            for feed in feeds:
                try:
                    recs = sources.parse_news(fetch(sources.news_url(feed, geo)))
                except Exception as exc:  # noqa: BLE001
                    failed.append(f"{feed.get('q') or feed.get('topic')}: {type(exc).__name__}")
                    continue
                cutoff = now - MAX_ITEM_AGE_H * 3600
                n += take([r for r in recs if (r["published_at"] or now) >= cutoff
                           and r["publisher"].strip().lower() not in blocked
                           and not _JUNK_TITLE.search(r["title"])], False)
            if feeds and len(failed) == len(feeds):
                raise RuntimeError("every news feed failed — " + "; ".join(failed))
            return n, ("failed feeds — " + "; ".join(failed)) if failed else ""

        def youtube_job():
            n, failed = 0, []
            channels = cfg.get("youtube") or []
            api_key = os.environ.get("YOUTUBE_API_KEY", "").strip()
            for ch in channels:
                try:
                    if api_key:
                        # Official API: reliable, exact counts; ~2 quota units per channel
                        # per run against a free 10,000/day.
                        recs = sources.fetch_youtube_api(ch["id"], api_key, ch.get("name", ""), fetch)
                    else:
                        recs = _with_retries(lambda ch=ch: sources.parse_youtube(
                            fetch(sources.youtube_url(ch["id"])), ch.get("name", "")))
                except Exception as exc:  # noqa: BLE001
                    failed.append(f"{ch.get('name') or ch.get('id')}: {type(exc).__name__}")
                    continue
                cutoff = now - YT_TRACK_DAYS * 86400
                n += take([r for r in recs if (r["published_at"] or now) >= cutoff], True)
            if channels and len(failed) == len(channels):
                raise RuntimeError("every channel feed failed — " + "; ".join(failed))
            return n, ("failed channels — " + "; ".join(failed)) if failed else ""

        run("gtrends", gtrends_job)
        run("news", news_job)
        if cfg.get("youtube"):
            run("youtube", youtube_job)
        topics.assign(conn, fresh, now, extra)
        conn.commit()

        # Wikipedia publishes one total per finished day, so once a day is
        # enough. Runs last: a page only ever attaches to an existing story.
        health = store.source_health(conn).get("wiki", {})
        if not health.get("last_ok") or now - health["last_ok"] >= 12 * 3600:
            def wiki_job():
                index = topics.TopicIndex(conn, now)
                today = datetime.fromtimestamp(now, timezone.utc).date()
                n, got, new = 0, 0, []
                for back in (1, 2, 3):
                    try:
                        recs = sources.parse_wiki(fetch(sources.wiki_url(geo, today - timedelta(days=back))))
                    except Exception:  # noqa: BLE001 — yesterday is often not published yet
                        continue
                    got += 1
                    for rec in recs:
                        row = conn.execute("SELECT id FROM items WHERE source='wiki' AND ext_id=?",
                                           (rec["ext_id"],)).fetchone()
                        if row:
                            item_id = row["id"]
                        elif index.best(tokens(rec["title"]), niche_only=True, covered=True) is not None:
                            item_id, _ = store.upsert_item(conn, rec, now)
                            new.append((item_id, rec))
                        else:
                            continue
                        store.add_observation(conn, item_id, rec["published_at"], rec["value"])
                        n += 1
                if not got:
                    raise RuntimeError("no daily page-view file available")
                topics.assign(conn, new, now, extra)
                return n, ""
            run("wiki", wiki_job)

        store.prune(conn, now)
        conn.commit()
    return summary


# =============================== compute =======================================

def _spread(t0: float, t1: float, amount: float, now: float, hours: int, k_max: int, add) -> None:
    """Allocate `amount` gained evenly over [t0, t1] to the windows it overlaps."""
    span = t1 - t0
    if amount <= 0 or span <= 0:
        return
    size = hours * 3600
    for k in range(k_max):
        hi, lo = now - k * size, now - (k + 1) * size
        overlap = min(t1, hi) - max(t0, lo)
        if overlap > 0:
            add(k, amount * overlap / span)


def _windows(items, obs, now: float, hours: int, k_max: int, yt_weight: dict | None = None):
    """-> (totals[k][source], per_topic[topic][k][source]) for one window size.
    yt_weight: channel name -> multiplier for its views (creators count more)."""
    yt_weight = yt_weight or {}
    totals = [defaultdict(float) for _ in range(k_max)]
    per_topic: dict[int, list] = defaultdict(lambda: [defaultdict(float) for _ in range(k_max)])
    size = hours * 3600

    def k_of(ts: float) -> int | None:
        k = int((now - ts) // size)
        return k if 0 <= k < k_max and ts <= now else None

    # One outlet's coverage of one story counts at most this many times per
    # window. Without it a live-blog or an aggregator posting twenty
    # near-identical updates outweighs twenty outlets posting one each — and
    # share of voice is meant to measure breadth of attention, not one desk.
    per_outlet: dict[tuple, int] = defaultdict(int)

    for it in items:
        tid, src = it["topic_id"], it["source"]
        series = obs.get(it["id"], [])
        if src == "news":
            ts = it["published_at"] if it["published_at"] and it["published_at"] <= now else it["first_seen"]
            k = k_of(ts)
            if k is not None:
                key = (tid, k, it["publisher"] or it["id"])
                per_outlet[key] += 1
                if per_outlet[key] <= OUTLET_CAP:
                    totals[k]["news"] += 1
                    per_topic[tid][k]["news"] += 1
        elif src == "gtrends":
            # Search volume is a level, not a count: use the mean seen in the window.
            by_k: dict[int, list] = defaultdict(list)
            for ts, v in series:
                k = k_of(ts)
                if k is not None:
                    by_k[k].append(v)
            for k, vals in by_k.items():
                m = sum(vals) / len(vals)
                totals[k]["gtrends"] += m
                per_topic[tid][k]["gtrends"] += m
        elif src == "youtube":
            mult = yt_weight.get(it["publisher"], 1.0)

            def add(k, amt, tid=tid, mult=mult):
                totals[k]["youtube"] += amt * mult
                per_topic[tid][k]["youtube"] += amt * mult
                # the real count, for display; not a source key in `totals`,
                # so it never enters the score
                per_topic[tid][k]["youtube_raw"] += amt
            pub = it["published_at"]
            if series and pub and 0 < series[0][0] - pub <= YT_BACKFILL_H * 3600:
                _spread(pub, series[0][0], series[0][1], now, hours, k_max, add)
            for (t0, v0), (t1, v1) in zip(series, series[1:]):
                _spread(t0, t1, v1 - v0, now, hours, k_max, add)
    return totals, per_topic


def _label(members: list, sig: set[str]) -> str:
    """The member headline closest to what the topic's items have in common."""
    for src in ("news", "youtube", "gtrends"):
        pool = [m for m in members if m["source"] == src]
        if pool:
            def central(m):
                toks = tokens(strip_publisher(m["title"]))
                shared = weight(toks & sig)
                # share of the headline that is on-topic: favours the concise,
                # typical headline over the longest one
                return (shared >= 2, shared / (weight(toks) or 1), m["published_at"] or m["first_seen"])
            best = max(pool, key=central)
            return strip_publisher(best["title"]) if src == "news" else best["title"]
    return members[0]["title"] if members else ""


def _windows_kept(hours: float) -> int:
    """How many windows to score: enough for a sparkline (4), never fewer than 2."""
    return max(2, int(max(HORIZON_H, 4 * hours) // hours))


def _choices(cfg: dict, base: float | None = None) -> tuple:
    """Window sizes to try, shortest first: the chosen window, then double it —
    widening only when the flow is too thin to compare two windows."""
    w = float(base or cfg.get("window_hours") or 24)
    return tuple((h, _windows_kept(h)) for h in (w, 2 * w))


def window_options(cfg: dict) -> list[float]:
    """The 1-day / 2-day switch: configured options, default first."""
    opts = [float(x) for x in (cfg.get("window_options") or []) if float(x) > 0]
    default = float(cfg.get("window_hours") or 24)
    return [default] + [o for o in opts if o != default]


def _yt_weights(cfg: dict) -> dict[str, float]:
    """channel name -> how much its views count. Creators pick topics by what
    is trending; TV/news channels cover the whole day's agenda, so a video from
    them says far less about what the audience is hungry for."""
    weight_of = {"creator": float(cfg.get("creator_weight") or 1.0),
                 "news": float(cfg.get("news_channel_weight", 1.0))}
    return {ch.get("name", ""): weight_of.get(ch.get("group", "news"), 1.0)
            for ch in cfg.get("youtube") or []}


def channel_pace(items, obs) -> dict[tuple, float]:
    """(channel, kind) -> median views/hour of its tracked videos (>= 3 needed).
    Live streams, Shorts and long videos run at rates orders of magnitude
    apart, so a video is only ever measured against its own kind."""
    rates: dict[tuple, list] = defaultdict(list)
    for it in items:
        series = obs.get(it["id"])
        if it["source"] != "youtube" or not series or not it["published_at"]:
            continue
        ts, v = series[-1]
        rates[(it["publisher"], it["kind"] or "long")].append(v / max((ts - it["published_at"]) / 3600, 0.5))
    return {k: sorted(v)[len(v) // 2] for k, v in rates.items() if len(v) >= 3}


def outlier(it, obs, pace: dict) -> float | None:
    """How many times its channel's usual pace (same kind) a video is running."""
    series = obs.get(it["id"])
    base = pace.get((it["publisher"], it["kind"] or "long"))
    if not series or not it["published_at"] or not base:
        return None
    ts, v = series[-1]
    return (v / max((ts - it["published_at"]) / 3600, 0.5)) / base


def _creators(cfg: dict) -> set[str]:
    return {ch.get("name", "") for ch in cfg.get("youtube") or [] if ch.get("group") == "creator"}


def _h(hours: float) -> str:
    return f"{hours:g}"


def compute(now: float | None = None, cfg: dict | None = None, write: bool = False,
            window: tuple | None = None, per_state: int = PER_STATE,
            base_hours: float | None = None) -> dict:
    """Score every active topic and return the board. write=True also records
    lifecycle changes and the daily history. window=(hours, count) forces one
    window size instead of widening (the periodic update needs a fixed one);
    base_hours picks the starting window (the 1-day / 2-day switch)."""
    now = time.time() if now is None else now
    cfg = cfg or load_config()
    w = cfg["weights"]
    since = now - 8 * 86400

    with closing(store.connect()) as conn:
        blocked = {str(p).strip().lower() for p in cfg.get("ignore_publishers") or []}
        # The publisher block-list and junk-title filter are applied here too,
        # not only at collection: items stored before a rule existed must stop
        # counting the moment the rule is added.
        items = [r for r in conn.execute(
            "SELECT id, source, title, url, publisher, published_at, first_seen, topic_id, kind"
            " FROM items WHERE topic_id IS NOT NULL AND first_seen >= ?", (since,))
            if not (r["source"] == "news" and ((r["publisher"] or "").strip().lower() in blocked
                                               or _JUNK_TITLE.search(r["title"])))]
        obs: dict[int, list] = defaultdict(list)
        for r in conn.execute("SELECT item_id, ts, value FROM observations WHERE ts >= ?"
                              " ORDER BY item_id, ts", (since,)):
            obs[r["item_id"]].append((r["ts"], r["value"]))
        trows = {r["id"]: r for r in conn.execute(
            "SELECT * FROM topics WHERE last_seen >= ?", (since,))}
        health = store.source_health(conn)
        first_run = conn.execute("SELECT MIN(started) FROM runs").fetchone()[0]

        # Pick the shortest window in which the latest two windows share a
        # usable source — at night the 6h news flow is too thin to compare.
        yt_weight, creators = _yt_weights(cfg), _creators(cfg)
        news_ch = float(cfg.get("news_channel_weight", 1.0))
        pace = channel_pace(items, obs)
        for hours, k_max in ((window,) if window else _choices(cfg, base_hours)):
            totals, per_topic = _windows(items, obs, now, hours, k_max, yt_weight)
            usable = [lifecycle.usable(t) for t in totals]
            common = usable[0] & usable[1]
            if common:
                break

        members: dict[int, list] = defaultdict(list)
        for it in items:
            members[it["topic_id"]].append(it)

        board = {s: [] for s in (lifecycle.EMERGING, lifecycle.RISING, lifecycle.PEAKING,
                                 lifecycle.FADING, lifecycle.WATCH)}
        recorded = []
        # Scale the "gone quiet" cut-off with the window: with 2-day windows a
        # topic that was busy in the previous window is 48h idle by construction
        # and must be called fading, not dropped. Emerging defaults to half a window.
        lc = dict(cfg.get("lifecycle") or {})
        lc["dead_hours"] = max(float(lc.get("dead_hours", 48)), 2 * hours)
        lc.setdefault("emerging_hours", hours / 2)
        horizon_h = max(HORIZON_H, 2 * hours)
        for tid, t in trows.items():
            mem = members.get(tid)
            if not mem:
                continue
            vals = per_topic[tid]
            series = [lifecycle.window_score(vals[k], totals[k], w, usable[k]) for k in range(k_max)]
            if common:
                recent = lifecycle.window_score(vals[0], totals[0], w, common)
                prev = lifecycle.window_score(vals[1], totals[1], w, common)
            else:
                recent, prev = series[0], None
            known = [s for s in series if s is not None]
            peak = max(known, default=0.0)
            if series[0] and recent:   # express the peak in the same units as `recent`
                peak *= recent / series[0]
            active = [k for k in range(k_max) if any(v > 0 for v in vals[k].values())]
            idle_h = active[0] * hours if active else (now - t["last_seen"]) / 3600
            age_h = (now - t["first_seen"]) / 3600

            # --- evidence: one article (or one video) is not a trend ---
            horizon = now - horizon_h * 3600
            news = [m for m in mem if m["source"] == "news"
                    and (m["published_at"] or m["first_seen"]) >= horizon]
            vids = [m for m in mem if m["source"] == "youtube" and obs.get(m["id"])]
            gts = [m for m in mem if m["source"] == "gtrends"
                   and obs.get(m["id"]) and obs[m["id"]][-1][0] >= now - 24 * 3600]
            wikis = [m for m in mem if m["source"] == "wiki" and obs.get(m["id"])]
            publishers = {m["publisher"] for m in news if m["publisher"]}
            channels = {m["publisher"] for m in vids}
            # Two creators make a topic real; a news channel counts only for its
            # weight (two clips from TV desks are routine, not a trend).
            channel_evidence = sum(1.0 if c in creators else min(news_ch, 1.0) for c in channels)
            # A creator video running well above its channel's normal is a
            # demand signal on its own: one such video plus any news coverage
            # is enough. Creators title for curiosity, not news, so they rarely
            # reach two-creator agreement — this was measured to leave three in
            # four creator videos off the board.
            hot_creator = any(m["publisher"] in creators and (outlier(m, obs, pace) or 0) >= CREATOR_HOT
                              for m in vids)
            if not (len(publishers) >= 2 or gts or channel_evidence >= 2 or (hot_creator and publishers)):
                continue

            state, velocity = lifecycle.classify(recent, prev, peak, age_h, idle_h, lc,
                                                 breadth=len(publishers) + len(channels))
            if state == lifecycle.DEAD:
                continue

            sig = topics.signature(json.loads(t["tokens"]), t["n_items"])
            label = _label(mem, sig)
            signals, present = [], []
            n_recent = int(vals[0].get("news", 0))
            if news:
                present.append("news")
                signals.append(f"{n_recent} article{'s' if n_recent != 1 else ''} in last {_h(hours)}h · "
                               f"{len(publishers)} publisher{'s' if len(publishers) != 1 else ''}")
            if gts:
                present.append("gtrends")
                top = max(gts, key=lambda m: obs[m["id"]][-1][1])
                signals.append(f"Google India: “{top['title']}” {int(obs[top['id']][-1][1]):,}+ searches")
            if vids:
                present.append("youtube")
                gained = int(vals[0].get("youtube_raw", 0))
                n_v = f"{len(vids)} video{'s' if len(vids) != 1 else ''} · {len(channels)} channel" \
                      f"{'s' if len(channels) != 1 else ''}"
                signals.append(f"YouTube: {gained:,} views in last {_h(hours)}h · {n_v}" if gained
                               else f"YouTube: {n_v}")
                by_creators = sorted(channels & creators)
                if by_creators:
                    signals.append("Creators covering it: " + ", ".join(by_creators))
            if wikis:
                present.append("wiki")
                wk = max(wikis, key=lambda m: obs[m["id"]][-1][1])
                s = obs[wk["id"]]
                chg = f" ({(s[-1][1] / s[-2][1] - 1) * 100:+.0f}% vs day before)" if len(s) > 1 and s[-2][1] else ""
                signals.append(f"Wikipedia: {int(s[-1][1]):,} views/day{chg}")

            links = sorted([m for m in mem if m["source"] in ("news", "youtube")],
                           key=lambda m: m["published_at"] or m["first_seen"], reverse=True)[:3]
            entry = {
                "id": tid, "label": label, "state": state,
                "score": round(recent or 0.0, 5),
                "velocity_pct": None if velocity is None else round(velocity * 100),
                "spark": [None if s is None else round(s, 5) for s in reversed(series)],
                "sources": present, "signals": signals, "niche": bool(t["niche"]),
                "age_hours": round(age_h, 1), "first_seen": t["first_seen"],
                "links": [{"title": strip_publisher(m["title"]) if m["source"] == "news" else m["title"],
                           "url": m["url"], "publisher": m["publisher"]} for m in links],
                # confirmed on more sources -> ranks higher within its state
                "_rank": (recent or 0.0) * (1 + 0.5 * (len(present) - 1)),
                "_prev": prev or 0.0, "_mentions": len(news) + len(vids) + len(gts),
            }
            board[state].append(entry)
            recorded.append(entry)

        for state, rows in board.items():
            # A fading topic matters by how big it WAS, not how little is left.
            key = (lambda e: e["_prev"]) if state == lifecycle.FADING else (lambda e: e["_rank"])
            rows.sort(key=key, reverse=True)

        if write:
            _record(conn, recorded, now)
            conn.commit()

    interval_s = cfg["interval_minutes"] * 60
    src_out = []
    for s in ("gtrends", "news", "youtube", "wiki"):
        h = health.get(s)
        if not h:
            continue
        limit = 36 * 3600 if s == "wiki" else 2.5 * interval_s
        src_out.append({"source": s, "label": SOURCE_LABEL[s], "last_ok": h["last_ok"],
                        "ok": h["ok"], "error": h["error"], "items": h["n_items"],
                        "stale": (not h["last_ok"]) or now - h["last_ok"] > limit})
    # --- creator pulse: what the preferred creators are making right now, and
    # which of it is outrunning their usual pace — whether or not it matched a
    # news story. Creators often lead the news cycle or work evergreen angles.
    shown = {e["id"]: e for e in recorded}
    pulse = []
    for m in items:
        if (m["source"] != "youtube" or m["publisher"] not in creators or not m["published_at"]
                or m["published_at"] < now - PULSE_HOURS * 3600 or not obs.get(m["id"])):
            continue
        ts, views = obs[m["id"]][-1]
        age = max((ts - m["published_at"]) / 3600, 0.5)
        topic = shown.get(m["topic_id"])
        ratio = outlier(m, obs, pace)
        pulse.append({"title": m["title"], "channel": m["publisher"], "url": m["url"], "kind": m["kind"] or "long",
                      "views": int(views), "age_hours": round(age, 1), "views_per_hour": round(views / age),
                      "outlier": None if ratio is None else round(ratio, 1),
                      "topic": None if not topic else {"id": topic["id"], "label": topic["label"],
                                                       "state": topic["state"]}})
    pulse.sort(key=lambda v: (-(v["outlier"] or 0), -v["views_per_hour"]))

    counts = {s: len(r) for s, r in board.items()}
    niche_counts = {s: sum(1 for e in r if e["niche"]) for s, r in board.items()}
    for state, rows in board.items():
        # Keep the top of each list, but never let off-niche search trends
        # crowd the niche ones out of the cut.
        keep = [e for e in rows if e["niche"]][:per_state] + [e for e in rows if not e["niche"]][:per_state]
        board[state] = [e for e in rows if e in keep]
        for e in board[state]:
            for k in [k for k in e if k.startswith("_")]:
                del e[k]
    return {"as_of": now, "window_hours": hours, "comparable": bool(common), "states": board,
            "window_options": window_options(cfg),
            "creator_pulse": pulse[:12],
            # How much of the two windows being compared was actually observed:
            # a 2-day view needs 4 days of collection before YouTube and Google
            # numbers exist for the earlier window.
            "history_hours": round((now - first_run) / 3600, 1) if first_run else 0,
            "coverage": round(min((now - first_run) / (2 * hours * 3600), 1.0), 2) if first_run else 0,
            "sources": src_out, "counts": counts, "niche_counts": niche_counts}


def _record(conn, entries: list[dict], now: float) -> None:
    day = datetime.fromtimestamp(now, _IST).strftime("%Y-%m-%d")
    for e in entries:
        last = conn.execute("SELECT state FROM topic_states WHERE topic_id=? ORDER BY ts DESC LIMIT 1",
                            (e["id"],)).fetchone()
        if not last or last["state"] != e["state"]:
            vel = None if e["velocity_pct"] is None else e["velocity_pct"] / 100
            conn.execute("INSERT INTO topic_states (topic_id, ts, state, score, velocity)"
                         " VALUES (?,?,?,?,?)", (e["id"], now, e["state"], e["score"], vel))
        conn.execute(
            "INSERT INTO topic_daily (topic_id, day, label, peak_score, last_state, mentions, sources, niche)"
            " VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(topic_id, day) DO UPDATE SET"
            " label=excluded.label, peak_score=MAX(peak_score, excluded.peak_score),"
            " last_state=excluded.last_state, mentions=MAX(mentions, excluded.mentions),"
            " sources=excluded.sources, niche=excluded.niche",
            (e["id"], day, e["label"], e["score"], e["state"], e["_mentions"],
             ",".join(e["sources"]), int(e["niche"])))


# =============================== periodic update ===============================

TRENDING, SAME = "TRENDING", "SAME"
_CLASS = {lifecycle.EMERGING: TRENDING, lifecycle.RISING: TRENDING,
          lifecycle.PEAKING: SAME, lifecycle.WATCH: SAME, lifecycle.FADING: lifecycle.FADING}


def _cls(state: str, velocity_pct) -> str:
    """Trending / same / fading. With a measured change the band decides and
    nothing else; without one (new topic, or gone quiet) the state does."""
    if velocity_pct is None:
        return _CLASS.get(state, SAME)
    if velocity_pct >= 30:
        return TRENDING
    return lifecycle.FADING if velocity_pct <= -30 else SAME


def _normalise(topics_: list[dict]) -> list[dict]:
    """Classes re-derived so every update — however old the rule it was taken
    under — reads by the same definition."""
    for t in topics_:
        t["cls"] = _cls(t.get("state", ""), t.get("velocity_pct"))
    return topics_


def _view(payload: dict, hours: float | None) -> dict:
    """One window's part of a stored update. Updates taken before the
    1-day/2-day switch hold a single window at the top level."""
    views = payload.get("views") or {}
    key = _h(hours) if hours else None
    if key and key in views:
        return views[key]
    if views:
        return views[_h(payload["window_hours"])] if _h(payload["window_hours"]) in views else next(iter(views.values()))
    return {k: payload.get(k) for k in ("window_hours", "comparable", "topics", "dropped")}


def _topics_of(payload_json: str, hours: float | None = None) -> list[dict]:
    """A stored update's topics for one window, classes normalised."""
    return _normalise(list(_view(json.loads(payload_json), hours).get("topics") or []))


def _report_hours(cfg: dict) -> float:
    return max(1.0, float(cfg.get("report_hours") or 9))


def update_due(now: float | None = None, cfg: dict | None = None) -> tuple[bool, float]:
    """-> (is one due now, when the next one is due)."""
    now = time.time() if now is None else now
    cfg = cfg or load_config()
    with closing(store.connect()) as conn:
        last = conn.execute("SELECT MAX(ts) FROM updates").fetchone()[0]
    if last is None:
        return True, now
    due = last + _report_hours(cfg) * 3600
    return now >= due, due


def _update_view(conn, now: float, cfg: dict, hrs: float, prev: dict | None, moved: dict) -> dict:
    """Trending / same / fading over `hrs`-hour windows, and movement since the
    previous update's view of the same window."""
    board = compute(now, cfg, write=False, window=(hrs, _windows_kept(hrs)),
                    per_state=10_000)   # everything: "dropped off" must mean gone, not cut off
    prev_topics: dict[int, dict] = {}
    if prev:
        for t in _topics_of(prev["payload"], hrs):
            tid = moved.get(t["id"], t["id"])
            if tid not in prev_topics or t["score"] > prev_topics[tid]["score"]:
                prev_topics[tid] = t      # several fragments -> keep the biggest one's class
    topics_out = []
    for rows in board["states"].values():
        for e in rows:
            was = prev_topics.get(e["id"])
            topics_out.append({
                "id": e["id"], "label": e["label"],
                "cls": _cls(e["state"], e["velocity_pct"]), "state": e["state"],
                "velocity_pct": e["velocity_pct"], "score": e["score"], "sources": e["sources"],
                "signals": e["signals"], "niche": e["niche"],
                # first seen inside this window = it did not exist at the last update
                "new": e["age_hours"] <= min(hrs, _report_hours(cfg)),
                "prev_cls": was["cls"] if was else None,
            })
    now_ids = {t["id"] for t in topics_out}
    dropped = [{"id": i, "label": t["label"], "prev_cls": t["cls"], "niche": t["niche"]}
               for i, t in prev_topics.items() if i not in now_ids]
    return {"window_hours": hrs, "comparable": board["comparable"], "topics": topics_out,
            "dropped": dropped, "sources": board["sources"]}


def make_update(now: float | None = None, cfg: dict | None = None) -> dict:
    """Take the periodic update: for EACH window option (1 day, 2 days) every
    topic classed TRENDING / SAME / FADING against the window before, plus how
    each one moved since the previous update. Stored permanently."""
    now = time.time() if now is None else now
    cfg = cfg or load_config()
    every = _report_hours(cfg)
    windows = window_options(cfg)
    with closing(store.connect()) as conn:
        prev = conn.execute("SELECT id, ts, payload FROM updates ORDER BY ts DESC LIMIT 1").fetchone()
        # Topics folded into another since the last update carry their old
        # class over to the id they became, instead of showing as dropped.
        moved = {r["old_id"]: r["new_id"] for r in conn.execute("SELECT old_id, new_id FROM topic_merges")}
        views = {_h(h): _update_view(conn, now, cfg, h, prev, moved) for h in windows}
        # How well was the period actually observed? Collection only happens
        # while the app (or the scheduled task) runs.
        runs = conn.execute("SELECT COUNT(*) FROM runs WHERE source='news' AND ok=1 AND started > ?",
                            (now - every * 3600,)).fetchone()[0]
        gap_h = (now - prev["ts"]) / 3600 if prev else None
        main = views[_h(windows[0])]
        payload = {
            "ts": now, "every_hours": every, "prev_ts": prev["ts"] if prev else None,
            # the previous update should be `every` hours old; much older = the app was off
            "late_hours": round(gap_h - every, 1) if gap_h is not None and gap_h - every > 1 else 0,
            "collections": runs, "expected_collections": round(every * 60 / cfg["interval_minutes"]),
            "views": views,
            # the default window also at the top level, as older readers expect
            **{k: main[k] for k in ("window_hours", "comparable", "topics", "dropped", "sources")},
        }
        cur = conn.execute("INSERT INTO updates (ts, window_hours, payload) VALUES (?,?,?)",
                           (now, windows[0], json.dumps(payload, ensure_ascii=False)))
        conn.commit()
        payload["id"] = cur.lastrowid
    return payload


def maybe_update(now: float | None = None, cfg: dict | None = None) -> dict | None:
    """Make the digest if one is due. After downtime this fires once, late,
    rather than back-filling digests for hours nobody observed."""
    now = time.time() if now is None else now
    cfg = cfg or load_config()
    return make_update(now, cfg) if update_due(now, cfg)[0] else None


def list_updates(limit: int = 40) -> list[dict]:
    with closing(store.connect()) as conn:
        return [{"id": r["id"], "ts": r["ts"], "window_hours": r["window_hours"]}
                for r in conn.execute("SELECT id, ts, window_hours FROM updates ORDER BY ts DESC LIMIT ?",
                                      (limit,))]


def get_update(update_id: int | None = None, hours: float | None = None) -> dict | None:
    """One update (latest when no id), seen through one window (default: the
    one it was taken with first)."""
    with closing(store.connect()) as conn:
        row = (conn.execute("SELECT id, payload FROM updates WHERE id=?", (update_id,)).fetchone()
               if update_id is not None else
               conn.execute("SELECT id, payload FROM updates ORDER BY ts DESC LIMIT 1").fetchone())
    if not row:
        return None
    payload = json.loads(row["payload"])
    view = _view(payload, hours)
    out = {k: v for k, v in payload.items() if k != "views"}
    out.update({k: view.get(k, out.get(k)) for k in ("window_hours", "comparable", "dropped", "sources")})
    out["topics"] = _normalise(list(view.get("topics") or []))
    out["windows"] = sorted(float(k) for k in (payload.get("views") or {})) or [payload.get("window_hours")]
    out["id"] = row["id"]
    return out


# =============================== history =======================================

def history_days(limit: int = 30) -> list[str]:
    with closing(store.connect()) as conn:
        return [r["day"] for r in conn.execute(
            "SELECT DISTINCT day FROM topic_daily ORDER BY day DESC LIMIT ?", (limit,))]


def history(day: str, limit: int = 40) -> list[dict]:
    with closing(store.connect()) as conn:
        rows = conn.execute(
            "SELECT topic_id, label, peak_score, last_state, mentions, sources, niche"
            " FROM topic_daily WHERE day=? ORDER BY peak_score DESC LIMIT ?", (day, limit)).fetchall()
        out = []
        for r in rows:
            path = [s["state"] for s in conn.execute(
                "SELECT state FROM topic_states WHERE topic_id=? ORDER BY ts", (r["topic_id"],))]
            out.append({"id": r["topic_id"], "label": r["label"], "peak_score": r["peak_score"],
                        "state": r["last_state"], "mentions": r["mentions"],
                        "sources": (r["sources"] or "").split(",") if r["sources"] else [],
                        "niche": bool(r["niche"]), "path": path})
        return out


# =============================== service =======================================

class TrendService:
    MANUAL_COOLDOWN_S = 60   # be polite to the free feeds

    def __init__(self) -> None:
        self._lock = threading.Lock()          # one collection at a time
        self._thread: threading.Thread | None = None
        self._board: dict | None = None
        self._board_at = 0.0
        self._other: dict[float, tuple] = {}   # boards for the non-default window options
        self._last_finish = 0.0
        self.running = False

    def _cycle(self) -> None:
        with self._lock:
            self.running = True
            try:
                collect()
                self._board, self._board_at = compute(write=True), time.time()
                self._other.clear()  # fresh data: other windows recompute on next view
                maybe_update()      # the periodic digest, when one is due
            finally:
                self.running = False
                self._last_finish = time.time()

    def start(self) -> None:
        if self._thread:
            return

        def loop() -> None:
            while True:
                try:
                    self._cycle()
                except Exception:  # noqa: BLE001 — never let the loop die
                    pass
                # Wake for the next collection, or sooner if a digest falls due
                # first, so the digest lands on time rather than up to a full
                # interval late.
                pause = load_config()["interval_minutes"] * 60
                try:
                    until_due = update_due()[1] - time.time()
                    if 0 < until_due < pause:
                        pause = max(60, until_due + 1)
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(pause)

        self._thread = threading.Thread(target=loop, daemon=True, name="trend-collector")
        self._thread.start()

    def trigger(self) -> tuple[bool, str]:
        """Manual 'collect now'. Returns (started, reason-if-not)."""
        if self.running:
            return False, "a collection is already running"
        wait = self.MANUAL_COOLDOWN_S - (time.time() - self._last_finish)
        if wait > 0:
            return False, f"just collected — try again in {int(wait) + 1}s"
        threading.Thread(target=self._cycle, daemon=True, name="trend-collect-now").start()
        return True, ""

    def board(self, max_age_s: float = 60, hours: float | None = None) -> dict:
        """The board, recomputed at most once a minute (cheap, read-only).
        hours = a window option other than the default (the 1-day/2-day switch)."""
        default = window_options(load_config())[0]
        if hours and float(hours) != default:
            cached = self._other.get(float(hours))
            if not cached or time.time() - cached[1] > max_age_s:
                cached = (compute(write=False, base_hours=float(hours)), time.time())
                self._other[float(hours)] = cached
            return {**cached[0], "collecting": self.running}
        if self._board is None or time.time() - self._board_at > max_age_s:
            self._board, self._board_at = compute(write=False), time.time()
        return {**self._board, "collecting": self.running}

    def topic(self, topic_id) -> dict | None:
        """Look up a topic on the current board (used to stamp posts with the
        trend state they were written in)."""
        for rows in (self._board or {}).get("states", {}).values():
            for e in rows:
                if e["id"] == topic_id:
                    return e
        return None


service = TrendService()
