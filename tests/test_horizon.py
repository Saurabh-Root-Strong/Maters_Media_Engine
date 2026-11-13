"""Timing: occasions, short and long term — offline, with fake Wikipedia readers."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from engine.trends import horizon

NOW = datetime(2026, 11, 13, 12, tzinfo=timezone.utc).timestamp()


def fake(daily=None, monthly=None):
    def fetch(url):
        if "/daily/" in url:
            return json.dumps({"items": [{"views": v} for v in daily]})
        if "/monthly/" in url:
            return json.dumps({"items": [{"timestamp": f"{m}0100", "views": v} for m, v in monthly]})
        raise AssertionError(url)
    return fetch


def months(peak_months=(10, 11), base=1000, high=6000, start=(2023, 10)):
    y, m, out = start[0], start[1], []
    for _ in range(36):
        out.append((f"{y}{m:02d}", high if m in peak_months else base))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def diwali_daily(days_since_peak, left_pct):
    """60 days: flat 1,000, a peak of 20,000, then down to left_pct of it."""
    d = [1000] * 60
    at = 59 - days_since_peak
    d[at] = 20000
    for i in range(at + 1, 60):
        d[i] = max(1000, 20000 * left_pct // 100)
    return d


@pytest.mark.parametrize("text,labels", [
    ("Stock market bhi Diwali lights jaisa hai", ["Diwali"]),
    ("Muhurat trading on Diwali", ["Diwali", "Muhurat trading"]),
    ("budget for a holiday trip", []),                         # a budget is not the Union Budget
    ("Union Budget 2027 explained", ["Union Budget"]),
    ("IPL auction and stocks", ["IPL"]),
    ("nothing seasonal here", []),
    ("Rakhi Sawant ke stock tips", []),
    ("Nifty at a new year high", []),
    ("Happy New Year 2027 portfolio", ["New Year"]),
    ("Rakhi gift: gold coin", ["Raksha Bandhan"]),
])
def test_occasions_are_found_from_what_the_post_says(text, labels):
    assert [o["label"] for o in horizon.occasions_in(text)] == labels


def test_an_occasion_that_peaked_days_ago_and_dropped_is_fading():
    r = horizon.read_occasion("Diwali", "Diwali", NOW, fake(diwali_daily(5, 10), months()))
    assert r["phase"] == "FADING" and r["days_since_peak"] == 5 and r["pct_of_peak"] == 10
    assert r["peak_day"] == "2026-11-07" and r["next_peak"] == "October 2027"


def test_yesterdays_peak_is_post_today():
    r = horizon.read_occasion("Diwali", "Diwali", NOW, fake(diwali_daily(0, 100), months()))
    assert r["phase"] == "PEAK"


def test_a_quiet_occasion_is_out_of_season_and_says_when_it_returns():
    r = horizon.read_occasion("Holi", "Holi", NOW, fake([1000] * 60, months((3,))))
    assert r["phase"] == "OFF_SEASON" and r["next_peak"] == "March 2027"


def test_a_rising_line_is_building():
    r = horizon.read_occasion("Christmas", "Christmas", NOW, fake([1000] * 53 + [1500] * 7, months((12,))))
    assert r["phase"] == "BUILDING"


@pytest.mark.parametrize("prev,last,label", [(1000, 1500, "GROWING"), (1000, 700, "SHRINKING"), (1000, 1100, "STABLE")])
def test_long_term_is_year_on_year(prev, last, label):
    m = [(f"2024{i:02d}", prev) for i in range(1, 13)] + [(f"2025{i:02d}", last) for i in range(1, 13)]
    r = horizon.read_long_term("X", NOW, fake(monthly=m))
    assert r["label"] == label


def test_advice_for_a_passed_occasion_says_do_not_frame_it_as_now():
    o = {"label": "Diwali", "phase": "FADING", "days_since_peak": 5, "pct_of_peak": 10, "next_peak": "October 2027"}
    a = horizon._advice([o], {"label": "QUIET"}, None, True)
    assert "has passed" in a and "October 2027" in a and "Do not frame the post as happening now" in a
    o["pct_of_peak"] = 45
    assert "within a day or two" in horizon._advice([o], {"label": "QUIET"}, None, True)


def test_advice_without_an_occasion_follows_the_short_and_long_term():
    assert "post as soon as you can" in horizon._advice([], {"label": "RISING"}, None, False)
    assert "Growing year on year" in horizon._advice([], {"label": "STEADY"}, {"label": "GROWING"}, False)
    assert "Not tied to the news" in horizon._advice([], {"label": "QUIET"}, None, True)


def test_timing_never_raises_and_caches(tmp_path, monkeypatch):
    calls = []

    def fetch(url):
        calls.append(url)
        return fake(diwali_daily(5, 10), months())(url)
    ev = {"content_type": "mood", "lookup": {"verdict": {"state": "EVERGREEN"}}}
    t = horizon.timing("stock market emotions", "Diwali lights", {}, ev, fetch=fetch, now=NOW)
    assert t["occasions"][0]["phase"] == "FADING" and "Diwali has passed" in t["advice"]
    n = len(calls)
    horizon.timing("stock market emotions", "Diwali lights", {}, ev, fetch=fetch, now=NOW)
    assert len(calls) == n                                       # second read from cache

    def broken(url):
        raise OSError("offline")
    t = horizon.timing("Holi colours", "", {}, ev, fetch=broken, now=NOW + 99999)
    assert t["occasions"] == [] and t["errors"]


def test_brief_tells_the_writer_short_long_and_occasion():
    t = {"advice": "Diwali has passed.", "short": {"label": "QUIET", "reasons": []}, "long": None, "share": True,
         "occasions": [{"label": "Diwali", "phase": "FADING", "peak_day": "2026-11-07", "days_since_peak": 5,
                        "pct_of_peak": 10, "next_peak": "October 2027"}]}
    b = horizon.brief(t)
    assert b.startswith("TIMING — follow this: Diwali has passed.") and "Short term" in b and "Long term" in b
    assert "Occasion Diwali: fading" in b


def test_occasion_as_subject_gets_recap_or_early_advice():
    o = {"label": "Muhurat trading", "phase": "FADING", "days_since_peak": 2, "pct_of_peak": 15, "next_peak": "October 2027",
         "subject": True}
    assert "look-back" in horizon._advice([o], {"label": "RISING"}, None, False)
    o["days_since_peak"] = 9
    assert "only search traffic" in horizon._advice([o], {"label": "RISING"}, None, False)
    b = {"label": "Union Budget", "phase": "OFF_SEASON", "days_since_peak": 280, "pct_of_peak": 5,
         "next_peak": "February 2027", "months_away": 3, "subject": True}
    assert horizon._advice([b], {"label": "RISING"}, None, False).startswith("Early: Union Budget peaks around February 2027")
    b["subject"] = False                                    # only a backdrop: the old advice stands
    assert "out of season" in horizon._advice([b], {"label": "RISING"}, None, False)


def test_a_quiet_unknown_phrase_is_told_to_check_its_wording():
    assert "Check the spelling" in horizon._advice([], {"label": "QUIET"}, None, False)


def test_rate_limits_are_retried(monkeypatch):
    import urllib.error

    from engine.trends import sources
    monkeypatch.setattr(sources.time, "sleep", lambda s: None)
    calls = []

    def flaky(url):
        calls.append(url)
        if len(calls) < 3:
            raise urllib.error.HTTPError(url, 429, "slow down", {}, None)
        return "ok"
    assert sources.patient(flaky)("u") == "ok" and len(calls) == 3

    def gone(url):
        raise urllib.error.HTTPError(url, 404, "no", {}, None)
    with pytest.raises(urllib.error.HTTPError):
        sources.patient(gone)("u")


def test_the_occasion_a_post_is_about_decides_before_one_it_mentions():
    diwali = {"label": "Diwali", "phase": "FADING", "days_since_peak": 4, "pct_of_peak": 17, "next_peak": "October 2027"}
    muhurat = {"label": "Muhurat trading", "phase": "FADING", "days_since_peak": 4, "pct_of_peak": 3,
               "next_peak": "November 2027", "subject": True}
    a = horizon._advice([diwali, muhurat], {"label": "RISING"}, None, False)
    assert a.startswith("Muhurat trading was 4 days ago")
