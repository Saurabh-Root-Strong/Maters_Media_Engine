"""Timing for one post: the short term (this week), the long term (this year
and the years before), and the occasions it is tied to.

A post about Diwali lights is a different post five days after Diwali than
five days before it. Nothing here hard-codes a date: Wikipedia readers of the
occasion's article show when it peaked and how fast it is fading, and three
years of monthly readers show when it comes back. Free, no key, cached.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
from contextlib import closing
from datetime import date, datetime, timedelta, timezone

import yaml

from . import lookup, sources, store

_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "config", "occasions.yaml")
CACHE_HOURS = 12
MAX_OCCASIONS = 2
SPIKE = 2.5           # an occasion's peak day vs a normal day, for it to count as a moment
FADED = 25            # % of the peak left, below which the moment has passed


def occasions_in(text: str) -> list[dict]:
    """Occasions the post mentions, in the order they appear in the config."""
    with open(_PATH, encoding="utf-8") as f:
        rows = yaml.safe_load(f) or []
    return [r for r in rows if re.search(r["match"], text, re.I)][:MAX_OCCASIONS]


def _monthly(article: str, now: float, fetch, months: int = 36) -> list[dict]:
    today = datetime.fromtimestamp(now, timezone.utc).date()
    end = today.replace(day=1) - timedelta(days=1)                 # last complete month
    start = (end.replace(day=1) - timedelta(days=31 * (months - 1))).replace(day=1)
    slug = urllib.parse.quote(article.replace(" ", "_"), safe="")
    data = json.loads(fetch(f"{lookup._VIEWS}{slug}/monthly/{start:%Y%m}0100/{end:%Y%m%d}00"))
    out = []
    for i in data.get("items", []):
        s = str(i.get("timestamp", ""))
        if len(s) >= 6:
            out.append({"month": f"{s[:4]}-{s[4:6]}", "views": int(i.get("views", 0))})
    return out


def _next_peak(season: dict | None, today: date) -> str | None:
    if not season:
        return None
    m = today.month + (season["months_away"] or 12)
    y = today.year + (m - 1) // 12
    return f"{season['peak_month_name']} {y}"


def read_occasion(label: str, article: str, now: float, fetch) -> dict | None:
    """Where this occasion is in its cycle: building, at its peak, fading, or off-season."""
    daily = lookup._article_views(article, now, fetch, days=60)
    if len(daily) < 30:
        return None
    yesterday = datetime.fromtimestamp(now, timezone.utc).date() - timedelta(days=1)
    median = sorted(daily)[len(daily) // 2] or 1
    peak = max(daily)
    at = daily.index(peak)
    days_since = len(daily) - 1 - at                              # 0 = yesterday was the peak
    peak_day = yesterday - timedelta(days=days_since)
    recent = daily[max(at, len(daily) - 3):]                      # never average in the days before the peak
    level = sum(recent) / len(recent)
    pct = round(100 * level / peak) if peak else 0
    last7, prev7 = sum(daily[-7:]) / 7, sum(daily[-14:-7]) / 7
    try:
        season = lookup._seasonality(article, now, fetch)
    except Exception:  # noqa: BLE001 — the 60-day line still stands without it
        season = None
    nxt = _next_peak(season, yesterday + timedelta(days=1))
    if peak / median >= SPIKE and days_since <= 30:
        phase = "PEAK" if days_since <= 1 or pct >= 60 else "FADING"
    elif prev7 and last7 / prev7 >= 1.3:
        phase = "BUILDING"
    else:
        phase = "OFF_SEASON"
    return {"label": label, "article": article, "phase": phase, "peak_day": peak_day.isoformat(),
            "days_since_peak": days_since, "pct_of_peak": pct, "spike": round(peak / median, 1),
            "next_peak": nxt, "months_away": (season or {}).get("months_away"), "daily": daily,
            "url": "https://en.wikipedia.org/wiki/" + urllib.parse.quote(article.replace(" ", "_"))}


def read_long_term(article: str, now: float, fetch) -> dict | None:
    """Is interest in the subject growing or shrinking, year on year?"""
    months = _monthly(article, now, fetch)
    if len(months) < 24:
        return None
    last12 = sum(m["views"] for m in months[-12:])
    prev12 = sum(m["views"] for m in months[-24:-12])
    growth = round(100 * (last12 - prev12) / prev12) if prev12 else None
    label = ("GROWING" if growth is not None and growth >= 20 else
             "SHRINKING" if growth is not None and growth <= -20 else "STABLE")
    return {"article": article, "label": label, "growth_pct": growth, "months": months}


def _cached(name: str, part: str, now: float, fn):
    with closing(store.connect()) as conn:
        conn.executescript(lookup._TABLE)
        key = lookup._key(name)
        hit = lookup._cached(conn, key, part, CACHE_HOURS, now)
        if hit is not None:
            return hit.get("data")
        data = fn()
        lookup._save(conn, key, part, {"data": data}, now)
        return data


def _short_term(evidence: dict) -> dict:
    """This week, from what analyse() already gathered."""
    look, reasons = evidence.get("lookup") or {}, []
    now_ = evidence.get("now") or {}
    state = now_.get("state")
    if state:
        reasons.append(f"On today's trend board: {state.lower()}"
                       + (f", {now_['velocity_pct']:+.0f}% in the last window" if now_.get("velocity_pct") is not None else ""))
    yt = look.get("youtube") or {}
    if yt.get("found"):
        reasons.append(f"{yt.get('uploads_last_7d', 0)} of {yt['found']} YouTube videos on it came out this week")
    news = look.get("news") or {}
    if news.get("articles"):
        reasons.append(f"News: {news.get('per_day_recent')} a day lately vs {news.get('per_day_prior')} before")
    interest = look.get("interest") or {}
    if interest.get("ratio"):
        reasons.append(f"Wikipedia readers this week vs last: {interest['last7']:,} vs {interest['prev7']:,} a day")
    verdict = (look.get("verdict") or {}).get("state")
    label = {"RISING": "RISING", "EMERGING": "RISING", "PEAKING": "PEAKING", "FADING": "FADING",
             "STEADY": "STEADY"}.get(state) or {"TRENDING": "RISING", "STEADY": "STEADY", "FADING": "FADING",
                                                 "DORMANT": "QUIET", "EVERGREEN": "EVERGREEN"}.get(verdict, "UNKNOWN")
    return {"label": label, "reasons": reasons}


def _about_it(o: dict) -> str | None:
    """Advice when the occasion is the post's subject: recaps and previews are
    wanted at times a backdrop is not."""
    n, d = o["label"], o["days_since_peak"]
    if o["phase"] == "FADING":
        if d <= 3:
            return (f"{n} was {d} day{'s' if d != 1 else ''} ago: people still look for what happened. Post today and "
                    f"title it as a look-back (what happened, what it means), not as news of something coming.")
        return (f"{n} was {d} days ago: the spike is over and only search traffic is left. Title it for search "
                f"(the year and the exact name people type), and do not present it as new.")
    away = o.get("months_away")
    if o["phase"] == "OFF_SEASON" and o.get("next_peak") and away is not None and away <= 3:
        return (f"Early: {n} peaks around {o['next_peak']}. Publish now so the post is already found when searches "
                f"climb, and refresh the title and thumbnail a week before.")
    if o["phase"] == "OFF_SEASON" and o.get("next_peak"):
        return (f"{n} is far off (around {o['next_peak']}) and few look for it now. Post a short version now, or save "
                f"the full one for nearer the date.")
    return None


def _advice(occ: list[dict], short: dict, long: dict | None, share: bool) -> str:
    # The occasion the post is ABOUT decides before one it only mentions.
    occ = sorted(occ, key=lambda o: not o.get("subject"))
    for o in occ:
        n = o["label"]
        if o.get("subject") and not share:
            about = _about_it(o)
            if about:
                return about
        if o["phase"] == "FADING":
            when = f"{o['days_since_peak']} day{'s' if o['days_since_peak'] != 1 else ''} ago"
            back = f" It comes back around {o['next_peak']}." if o.get("next_peak") else ""
            if o["pct_of_peak"] <= FADED:
                return (f"{n} has passed: it peaked {when} and interest is down to {o['pct_of_peak']}% of that. "
                        f"Do not frame the post as happening now. Either drop the {n} reference and lead with the "
                        f"feeling or idea itself, or save this post for next time.{back}")
            return (f"{n} peaked {when} and interest is at {o['pct_of_peak']}% of that and falling. Post within a "
                    f"day or two, and write about it as just gone, not coming.{back}")
        if o["phase"] == "PEAK":
            return f"{n} is at its peak right now. Post today; every day from here, interest falls."
        if o["phase"] == "BUILDING":
            return f"Interest in {n} is building. Post in the next few days, before the peak, when the crowd arrives."
        if o["phase"] == "OFF_SEASON" and o.get("next_peak"):
            return (f"{n} is out of season (next peak around {o['next_peak']}). Keep the {n} reference only if the "
                    f"post works without it, or save it for then.")
    if share:
        return "Not tied to the news or a date: post it at your audience's best time; it can be reposted later."
    if short["label"] == "RISING":
        return "Rising this week: post as soon as you can, before the topic gets crowded."
    if short["label"] in ("PEAKING", "FADING"):
        return "Past its high this week: post only with a new angle; a repeat of what is out there will not travel."
    if long and long["label"] == "GROWING":
        return "Growing year on year: a steady search topic worth a thorough evergreen post."
    if long and long["label"] == "SHRINKING":
        return "Shrinking year on year: keep it short and tie it to something current."
    if short["label"] == "QUIET" and not long:
        return ("Little sign anyone looks for this exact wording right now. Check the spelling, or use the words "
                "people do type (the search suggestions) in the title.")
    return "Steady: package it for search — the title should answer what people type."


def timing(topic: str, notes: str, seen: dict, evidence: dict, fetch=None, now: float | None = None) -> dict:
    """Short term, long term, occasions and one line of advice. Never raises."""
    now = time.time() if now is None else now
    fetch = fetch or sources.patient(lambda url: sources.http_get(url, timeout=15))
    share = evidence.get("content_type") in ("mood", "humour")
    text = " ".join([topic, notes, seen.get("summary") or "", seen.get("core_line") or "", seen.get("subject") or ""]
                    + list(seen.get("key_points") or []) + list(seen.get("on_screen_text") or []))
    occ, errors = [], []
    for o in occasions_in(text):
        try:
            r = _cached(o["article"], "occasion", now, lambda o=o: read_occasion(o["label"], o["article"], now, fetch))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{o['label']}: {type(exc).__name__}")
            continue
        if r:
            # Is the occasion what the post is ABOUT (a Budget explainer), or only
            # its backdrop (a mood reel with Diwali lights)? The advice differs.
            occ.append(dict(r, subject=bool(re.search(o["match"], topic, re.I))))
    long = None
    article = ((evidence.get("lookup") or {}).get("interest") or {}).get("article")
    if article:
        try:
            long = _cached(article, "longterm", now, lambda: read_long_term(article, now, fetch))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"long term: {type(exc).__name__}")
    season = ((evidence.get("lookup") or {}).get("interest") or {}).get("season")
    if long is not None:
        long = dict(long, season=season)
    short = _short_term(evidence)
    return {"short": short, "long": long, "occasions": occ, "share": share,
            "advice": _advice(occ, short, long, share), "errors": errors}


def brief(t: dict | None) -> str:
    """The timing read, as evidence for the writer."""
    if not t:
        return ""
    parts = [f"TIMING — follow this: {t['advice']}",
             f"Short term (this week): {t['short']['label']}"
             + (" — " + "; ".join(t["short"]["reasons"]) if t["short"]["reasons"] else "")]
    lt = t.get("long")
    if lt:
        parts.append(f"Long term (year on year): {lt['label']}"
                     + (f", {lt['growth_pct']:+d}% readers over the last 12 months vs the 12 before" if lt.get("growth_pct") is not None else "")
                     + (f"; yearly peak in {lt['season']['peak_month_name']}" if lt.get("season") else ""))
    elif t.get("share"):
        parts.append("Long term: a feeling, not a topic — it does not rise and fall with the news or the year.")
    for o in t.get("occasions", []):
        parts.append(f"Occasion {o['label']}: {o['phase'].lower().replace('_', ' ')}; peaked {o['peak_day']} "
                     f"({o['days_since_peak']} days ago), now {o['pct_of_peak']}% of peak"
                     + (f"; next peak around {o['next_peak']}" if o.get("next_peak") else ""))
    return "\n".join(parts)
