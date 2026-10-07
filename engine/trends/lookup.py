"""On-demand research for ANY topic — including ones the tracker never collected.

The tracker (service.py) watches a fixed beat. A creator can make a video on
anything — an old scam, a single company, one word. This module looks that one
subject up, live, in three free places and reads its own trend from them:

  news      Google News search, last 30 days: articles per day -> is coverage
            growing or drying up?
  interest  Wikipedia: the subject's article, daily readers over 60 days — a
            free, per-topic "interest over time" line that exists for almost
            anything with a name.
  youtube   YouTube search (official API, needs YOUTUBE_API_KEY): what was
            uploaded on it in the last 30 days across ALL of YouTube, and which
            of those videos are getting the views.

verdict() folds the first two into TRENDING / STEADY / FADING / DORMANT. The
result is cached, because a YouTube search costs 100 of the 10,000 free daily
quota units.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone

from . import sources, store
from .text import GENERIC, _STOP, _words, strip_publisher, tokens

_IST = timezone(timedelta(hours=5, minutes=30))
CACHE_HOURS = 6                  # news + interest
YT_CACHE_HOURS = 12              # a search costs quota
YT_SEARCHES_PER_DAY = 60         # 60 x 101 units, leaving room for the channel collector
TRENDING, STEADY, FADING, DORMANT = "TRENDING", "STEADY", "FADING", "DORMANT"

_TABLE = """
CREATE TABLE IF NOT EXISTS lookups (
    key TEXT NOT NULL,
    part TEXT NOT NULL,            -- 'news' | 'interest' | 'youtube'
    ts REAL NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (key, part)
) WITHOUT ROWID;
"""


CACHE_VERSION = "v2"             # bump when what a cached part contains changes


def _key(topic: str) -> str:
    return CACHE_VERSION + "|" + (" ".join(sorted(tokens(topic))) or topic.strip().lower())


def _cached(conn, key: str, part: str, max_age_h: float, now: float):
    row = conn.execute("SELECT ts, payload FROM lookups WHERE key=? AND part=?", (key, part)).fetchone()
    if row and now - row["ts"] <= max_age_h * 3600:
        return json.loads(row["payload"])
    return None


def _save(conn, key: str, part: str, payload: dict, now: float) -> None:
    conn.execute("INSERT OR REPLACE INTO lookups (key, part, ts, payload) VALUES (?,?,?,?)",
                 (key, part, now, json.dumps(payload, ensure_ascii=False)))
    conn.commit()


def _ratio(recent: float, prior: float) -> float | None:
    return None if prior <= 0 else recent / prior


# --- news ----------------------------------------------------------------------

def news_pulse(topic: str, now: float, fetch) -> dict:
    """Articles per day on the topic over the last 30 days."""
    q = urllib.parse.quote_plus(f"{topic} when:30d")
    recs = sources.parse_news(fetch(f"https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"))
    recs = [r for r in recs if r["published_at"] and 0 <= now - r["published_at"] <= 31 * 86400]
    per_day = Counter(int((now - r["published_at"]) // 86400) for r in recs)       # 0 = last 24h
    span = max(per_day) + 1 if per_day else 0
    # Google returns at most 100 articles. If it hit the cap, the list only
    # reaches back `span` days — compare halves of what is actually covered.
    capped = len(recs) >= 100
    half = max(1, min(7, span // 2)) if capped else 7
    recent = sum(per_day[d] for d in range(half)) / half
    prior = sum(per_day[d] for d in range(half, 2 * half)) / half
    # A handful of articles is not a trend line: 1 article last week and 6
    # this week is "+500%" and means nothing. Need a real base to compare
    # against, and enough articles overall.
    enough = prior * half >= 3 and (recent + prior) * half >= 10
    recs.sort(key=lambda r: -r["published_at"])
    return {
        "articles": len(recs), "capped": capped, "days_covered": span,
        "publishers": len({r["publisher"] for r in recs if r["publisher"]}),
        "per_day_recent": round(recent, 2), "per_day_prior": round(prior, 2), "compare_days": half,
        "ratio": _ratio(recent, prior) if enough else None,
        "daily": [per_day.get(d, 0) for d in range(min(span, 30) - 1, -1, -1)] if span else [],
        "headlines": [{"title": r["title"], "publisher": r["publisher"], "url": r["url"], "at": r["published_at"]}
                      for r in recs[:8]],
    }


# --- interest (Wikipedia readers) ----------------------------------------------

_WIKI = "https://en.wikipedia.org/w/api.php?"
_VIEWS = "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia/all-access/user/"


def _article_views(title: str, now: float, fetch, days: int = 60) -> list[int]:
    end = datetime.fromtimestamp(now, timezone.utc).date() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    slug = urllib.parse.quote(title.replace(" ", "_"), safe="")
    data = json.loads(fetch(f"{_VIEWS}{slug}/daily/{start:%Y%m%d}/{end:%Y%m%d}"))
    return [int(i.get("views", 0)) for i in data.get("items", [])]


def interest_pulse(topic: str, now: float, fetch) -> dict | None:
    """Daily readers of the Wikipedia article that best matches the topic."""
    hits = json.loads(fetch(_WIKI + urllib.parse.urlencode(
        {"action": "query", "list": "search", "srsearch": topic, "srlimit": 4, "format": "json"})))
    q = tokens(topic)
    best = None
    for h in hits.get("query", {}).get("search", []):
        title = h.get("title", "")
        if not (tokens(title) & q):
            continue                      # an article sharing no word with the topic is a different subject
        try:
            views = _article_views(title, now, fetch)
        except Exception:  # noqa: BLE001
            continue
        if len(views) < 14:
            continue
        avg = sum(views) / len(views)
        # Among articles that match, the most-read one is the one people mean
        # ("ITC" -> ITC Limited, not ITC Entertainment).
        if best is None or avg > best[1]:
            best = (title, avg, views)
    if not best:
        return None
    title, avg, views = best
    last7, prev7 = sum(views[-7:]) / 7, sum(views[-14:-7]) / 7
    median = sorted(views)[len(views) // 2] or 1
    peak = max(views)
    peak_day = (datetime.fromtimestamp(now, timezone.utc).date()
                - timedelta(days=len(views) - views.index(peak)))
    return {
        "article": title, "url": "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_")),
        "avg_per_day": round(avg), "last7": round(last7), "prev7": round(prev7),
        "ratio": _ratio(last7, prev7) if avg >= 30 else None,     # under ~30 readers/day the line is noise
        "peak": peak, "peak_day": peak_day.isoformat(), "peak_vs_normal": round(peak / median, 1),
        "daily": views,
    }


# --- YouTube: what is winning on this topic ------------------------------------

_YT = "https://www.googleapis.com/youtube/v3"


def youtube_pulse(topic: str, now: float, key: str, fetch) -> dict:
    """Videos uploaded on the topic in the last 30 days, across all of YouTube."""
    after = datetime.fromtimestamp(now - 30 * 86400, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    found = json.loads(fetch(f"{_YT}/search?" + urllib.parse.urlencode(
        {"part": "snippet", "type": "video", "q": topic, "regionCode": "IN", "maxResults": 25,
         "order": "relevance", "publishedAfter": after, "key": key}))).get("items", [])
    ids = [i["id"]["videoId"] for i in found if i.get("id", {}).get("videoId")]
    if not ids:
        return {"videos": [], "found": 0}
    items = json.loads(fetch(f"{_YT}/videos?" + urllib.parse.urlencode(
        {"part": "snippet,statistics,contentDetails,liveStreamingDetails", "id": ",".join(ids),
         "key": key}))).get("items", [])
    q = tokens(topic) - GENERIC or tokens(topic)
    vids = []
    for v in items:
        sn = v.get("snippet", {})
        title = (sn.get("title") or "").strip()
        published = sources._iso(sn.get("publishedAt"))
        try:
            views = int(v.get("statistics", {}).get("viewCount", 0))
        except (TypeError, ValueError):
            views = 0
        if not title or not published:
            continue
        # search also returns loosely related videos; keep the ones whose own
        # title or description actually names the subject
        text = tokens(title) | tokens((sn.get("description") or "")[:300])
        if not (text & q):
            continue
        age_h = max((now - published) / 3600, 1.0)
        vids.append({"title": title, "channel": sn.get("channelTitle", ""), "views": views,
                     "url": f"https://www.youtube.com/watch?v={v['id']}", "kind": sources.video_kind(v),
                     "age_days": round(age_h / 24, 1), "views_per_hour": round(views / age_h),
                     "title_chars": len(title),
                     "devanagari": bool(re.search(r"[ऀ-ॿ]", title))})
    vids.sort(key=lambda v: -v["views"])
    top = vids[:10]
    pairs: Counter = Counter()
    for v in vids:
        words = [w.strip("'-&") for w in _words(v["title"].lower())]
        words = [w for w in words if len(w) > 1 and not w.isdigit()]
        for a, b in zip(words, words[1:]):
            if a not in _STOP and b not in _STOP:
                pairs[f"{a} {b}"] += 1
    kinds = Counter(v["kind"] for v in top)
    return {
        "found": len(vids), "videos": vids[:8],
        "top_kind_mix": dict(kinds),
        "shorts_share_top": round(kinds.get("short", 0) / len(top), 2) if top else 0,
        "median_title_chars": sorted(v["title_chars"] for v in top)[len(top) // 2] if top else None,
        "hindi_title_share": round(sum(v["devanagari"] for v in top) / len(top), 2) if top else 0,
        "phrases": [{"phrase": p, "count": c} for p, c in pairs.most_common(10) if c >= 2],
        "uploads_last_7d": sum(1 for v in vids if v["age_days"] <= 7),
    }


# --- verdict -------------------------------------------------------------------

def verdict(news: dict | None, interest: dict | None) -> dict:
    """Fold news coverage and reader interest into one call, with its reasons.

    Each line is compared with ITSELF (this week vs last week), so a small
    subject and a huge one are judged on the same scale. Thresholds match the
    trend board: +30% = trending, -30% = fading.
    """
    ratios, reasons = [], []
    if interest:
        chg = "" if interest["ratio"] is None else f" ({(interest['ratio'] - 1) * 100:+.0f}% week on week)"
        reasons.append(f"Wikipedia “{interest['article']}”: about {interest['last7']:,} readers a day{chg}")
        if interest["ratio"] is not None:
            ratios.append(interest["ratio"])
        if interest["peak_vs_normal"] >= 3:
            reasons.append(f"Interest spiked to {interest['peak']:,} on {interest['peak_day']} "
                           f"({interest['peak_vs_normal']}× a normal day) — it flares up around events")
    if news:
        if news["articles"]:
            cap = "100+ (list is capped)" if news["capped"] else str(news["articles"])
            chg = (" (too few to call a direction)" if news["ratio"] is None else
                   f", {(news['ratio'] - 1) * 100:+.0f}% on the {news['compare_days']} days before")
            reasons.append(f"News: {cap} articles in {news['days_covered']} days from {news['publishers']} "
                           f"publishers — {news['per_day_recent']:g} a day lately{chg}")
            if news["ratio"] is not None:
                ratios.append(news["ratio"])
        else:
            reasons.append("News: no articles in the last 30 days")
    level_low = (not interest or interest["avg_per_day"] < 100) and (not news or news["articles"] < 3)
    if not ratios:
        state = DORMANT if level_low else STEADY
        if not level_low:
            reasons.append("Not enough movement to call a direction — treating it as steady")
    else:
        score = 1.0
        for r in ratios:
            score *= min(max(r, 1 / 3), 3.0)     # no single line may swing the call beyond 3x
        score **= 1 / len(ratios)                # geometric mean: both lines count equally
        state = TRENDING if score >= 1.3 else FADING if score <= 0.7 else STEADY
        if level_low and state != TRENDING:
            state = DORMANT
    return {"state": state, "reasons": reasons,
            "basis": [n for n, x in (("news", news), ("interest", interest)) if x]}


# --- public --------------------------------------------------------------------

def research(topic: str, now: float | None = None, fetch=None, youtube: bool = True) -> dict:
    """Everything above for one topic, cached. Never raises: a source that
    fails is reported in `errors` and left out of the verdict."""
    now = time.time() if now is None else now
    fetch = fetch or (lambda url: sources.http_get(url, timeout=15))
    key = _key(topic)
    out: dict = {"topic": topic, "errors": [], "cached": []}
    with closing(store.connect()) as conn:
        conn.executescript(_TABLE)
        for part, fn, ttl in (("news", news_pulse, CACHE_HOURS), ("interest", interest_pulse, CACHE_HOURS)):
            hit = _cached(conn, key, part, ttl, now)
            if hit is not None:
                out[part] = hit.get("data")
                out["cached"].append(part)
                continue
            try:
                out[part] = fn(topic, now, fetch)
                _save(conn, key, part, {"data": out[part]}, now)
            except Exception as exc:  # noqa: BLE001
                out[part] = None
                out["errors"].append(f"{part}: {type(exc).__name__}")

        api_key = os.environ.get("YOUTUBE_API_KEY", "").strip()
        out["youtube"] = None
        if youtube:
            hit = _cached(conn, key, "youtube", YT_CACHE_HOURS, now)
            used = conn.execute("SELECT COUNT(*) FROM lookups WHERE part='youtube' AND ts > ?",
                                (now - 86400,)).fetchone()[0]
            if hit is not None:
                out["youtube"] = hit.get("data")
                out["cached"].append("youtube")
            elif not api_key:
                out["youtube_note"] = "Add YOUTUBE_API_KEY to search all of YouTube for this topic."
            elif used >= YT_SEARCHES_PER_DAY:
                out["youtube_note"] = (f"Daily limit of {YT_SEARCHES_PER_DAY} new YouTube searches reached "
                                       "(protects the free quota). Cached topics still work.")
            else:
                try:
                    out["youtube"] = youtube_pulse(topic, now, api_key, fetch)
                    _save(conn, key, "youtube", {"data": out["youtube"]}, now)
                except Exception as exc:  # noqa: BLE001
                    out["errors"].append(f"youtube: {type(exc).__name__}")
    out["verdict"] = verdict(out.get("news"), out.get("interest"))
    return out
