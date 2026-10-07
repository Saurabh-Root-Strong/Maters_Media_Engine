"""On-demand topic research — news, reader interest, YouTube, verdict, cache. Offline."""

import json
from datetime import datetime, timezone
from email.utils import formatdate

import pytest

from engine import content
from engine.trends import lookup

NOW = 1_790_000_000.0
DAY = 86400


def news_xml(ages_days):
    items = "".join(
        f"<item><title>Headline {i} about it - Pub{i % 7}</title><link>https://n.example/{i}</link>"
        f"<pubDate>{formatdate(NOW - a * DAY, usegmt=True)}</pubDate><source url='x'>Pub{i % 7}</source></item>"
        for i, a in enumerate(ages_days))
    return f"<rss><channel>{items}</channel></rss>"


def views_json(series):
    return json.dumps({"items": [{"views": v} for v in series]})


def make_fetch(news_ages=(), wiki=None, yt=None, fail=()):
    """wiki = {article title: [daily views]}; yt = [(id, title, views, age_days, duration, description)]"""
    calls = []

    def fetch(url):
        calls.append(url)
        for f in fail:
            if f in url:
                raise OSError("down")
        if "news.google.com" in url:
            return news_xml(news_ages)
        if "w/api.php" in url:
            return json.dumps({"query": {"search": [{"title": t} for t in (wiki or {})]}})
        if "pageviews/per-article" in url:
            for title, series in (wiki or {}).items():
                if "/" + title.replace(" ", "_") + "/" in url:
                    return views_json(series)
            raise OSError("404")
        if "/youtube/v3/search" in url:
            return json.dumps({"items": [{"id": {"videoId": v[0]}} for v in (yt or [])]})
        if "/youtube/v3/videos" in url:
            return json.dumps({"items": [{
                "id": v[0], "snippet": {"title": v[1], "channelTitle": "Chan", "description": v[5] if len(v) > 5 else "",
                                        "publishedAt": datetime.fromtimestamp(NOW - v[3] * DAY, timezone.utc).isoformat()},
                "statistics": {"viewCount": str(v[2])}, "contentDetails": {"duration": v[4]}} for v in (yt or [])]})
        raise OSError("unstubbed " + url)
    fetch.calls = calls
    return fetch


# --- verdict -------------------------------------------------------------------

def test_rising_interest_and_news_is_trending():
    f = make_fetch(news_ages=[0.5, 1, 1, 2, 2, 3, 3, 4, 5, 6] + [8, 9, 10, 12], wiki={"Harshad Mehta": [1000] * 53 + [2000] * 7})
    r = lookup.research("harshad mehta scam", now=NOW, fetch=f, youtube=False)
    assert r["verdict"]["state"] == "TRENDING"
    assert r["interest"]["article"] == "Harshad Mehta" and r["interest"]["last7"] == 2000
    assert any("+100% week on week" in x for x in r["verdict"]["reasons"])


def test_flat_lines_are_steady_evergreen():
    f = make_fetch(news_ages=[1, 2, 3, 5, 6, 8, 9, 10, 12, 13], wiki={"Harshad Mehta": [1400] * 60})
    r = lookup.research("harshad mehta scam", now=NOW, fetch=f, youtube=False)
    assert r["verdict"]["state"] == "STEADY" and r["verdict"]["basis"] == ["news", "interest"]


def test_falling_lines_are_fading():
    f = make_fetch(news_ages=[8, 8, 9, 9, 10, 11, 12, 13], wiki={"Some Topic": [3000] * 53 + [900] * 7})
    assert lookup.research("some topic", now=NOW, fetch=f, youtube=False)["verdict"]["state"] == "FADING"


def test_nothing_much_anywhere_is_dormant():
    f = make_fetch(news_ages=[20], wiki={"Obscure Topic": [12] * 60})
    r = lookup.research("obscure topic", now=NOW, fetch=f, youtube=False)
    assert r["verdict"]["state"] == "DORMANT"
    assert r["interest"]["ratio"] is None             # under ~30 readers a day the line is noise


def test_a_spike_is_called_out():
    series = [500] * 40 + [4000] + [500] * 19
    r = lookup.research("spiky topic", now=NOW, fetch=make_fetch(wiki={"Spiky Topic": series}), youtube=False)
    assert r["interest"]["peak"] == 4000 and r["interest"]["peak_vs_normal"] == 8.0
    assert any("flares up" in x for x in r["verdict"]["reasons"])


# --- news ----------------------------------------------------------------------

def test_capped_news_list_compares_halves_of_what_it_covers():
    # 100 articles, all inside 4 days: 70 in the last two days, 30 in the two before
    ages = [0.5] * 40 + [1.5] * 30 + [2.5] * 20 + [3.5] * 10
    n = lookup.news_pulse("itc", NOW, make_fetch(news_ages=ages))
    assert n["capped"] is True and n["days_covered"] == 4 and n["compare_days"] == 2
    assert n["per_day_recent"] == 35 and n["per_day_prior"] == 15 and n["ratio"] == pytest.approx(35 / 15)
    assert n["daily"] == [10, 20, 30, 40]              # oldest -> newest, for the sparkline


def test_a_handful_of_articles_gives_no_direction():
    n = lookup.news_pulse("x", NOW, make_fetch(news_ages=[1, 9]))
    assert n["articles"] == 2 and n["ratio"] is None
    # the live case that mis-fired: 6 articles this week against 1 last week is not "+500%"
    n = lookup.news_pulse("x", NOW, make_fetch(news_ages=[1, 1, 2, 4, 5, 6, 9, 20, 22, 28]))
    assert n["ratio"] is None
    r = lookup.research("harshad mehta", now=NOW, youtube=False,
                        fetch=make_fetch(news_ages=[1, 1, 2, 4, 5, 6, 9, 20, 22, 28],
                                         wiki={"Harshad Mehta": [1232] * 53 + [1401] * 7}))
    assert r["verdict"]["state"] == "STEADY"            # +14% readers, news too thin to count
    assert any("too few to call a direction" in x for x in r["verdict"]["reasons"])


def test_one_runaway_line_cannot_decide_the_verdict_alone():
    # news x10 against a base of 4/day, readers flat -> capped at 3x: sqrt(3 * 1.0) = 1.73 -> trending, but
    # readers FALLING by half with the same news burst must not read as trending: sqrt(3 * 0.5) = 1.22
    ages = [0.5] * 280 + [5.5] * 28          # 70 a day lately vs 7 a day before = 10x
    r = lookup.research("burst topic", now=NOW, youtube=False,
                        fetch=make_fetch(news_ages=ages, wiki={"Burst Topic": [2000] * 53 + [1000] * 7}))
    assert r["verdict"]["state"] == "STEADY"


# --- interest ------------------------------------------------------------------

def test_the_most_read_matching_article_is_the_one_people_mean():
    wiki = {"ITC Entertainment": [120] * 60, "ITC Limited": [9000] * 60, "Unrelated Page": [50000] * 60}
    i = lookup.interest_pulse("itc", NOW, make_fetch(wiki=wiki))
    assert i["article"] == "ITC Limited"                # not the first hit, and not a page sharing no word


def test_no_matching_article_is_none():
    assert lookup.interest_pulse("zzzz", NOW, make_fetch(wiki={"Different Thing": [100] * 60})) is None


# --- youtube -------------------------------------------------------------------

YT = [("a", "Harshad Mehta scam explained", 632_000, 10, "PT14M", ""),
      ("b", "Harshad Mehta ₹10 crore secret", 210_000, 3, "PT45S", ""),
      ("c", "हर्षद मेहता की कहानी | Harshad Mehta", 90_000, 20, "PT20M", ""),
      ("d", "Mumbai liquidation sale", 2_000_000, 5, "PT30S", "nothing related"),      # off-topic search noise
      ("e", "Scam 1992 full story", 80_000, 2, "PT1H", "The Harshad Mehta story")]     # named in description


def test_youtube_keeps_on_topic_videos_and_summarises_what_wins():
    y = lookup.youtube_pulse("harshad mehta scam", NOW, "k", make_fetch(yt=YT))
    titles = [v["title"] for v in y["videos"]]
    assert "Mumbai liquidation sale" not in titles and y["found"] == 4
    assert titles[0] == "Harshad Mehta scam explained" and y["videos"][0]["kind"] == "long"
    assert y["top_kind_mix"] == {"long": 3, "short": 1} and y["shorts_share_top"] == 0.25
    assert y["hindi_title_share"] == 0.25 and y["uploads_last_7d"] == 2
    assert {"phrase": "harshad mehta", "count": 3} in y["phrases"]


# --- research: cache, quota guard, fail-soft ------------------------------------

def test_results_are_cached_so_a_repeat_costs_nothing(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "k")
    f = make_fetch(news_ages=[1, 2, 3], wiki={"Harshad Mehta": [1400] * 60}, yt=YT)
    first = lookup.research("Harshad Mehta scam", now=NOW, fetch=f)
    n = len(f.calls)
    again = lookup.research("scam harshad mehta", now=NOW + 3600, fetch=f)       # same words, other order
    assert len(f.calls) == n and set(again["cached"]) == {"news", "interest", "youtube"}
    assert again["youtube"]["found"] == first["youtube"]["found"] == 4
    lookup.research("Harshad Mehta scam", now=NOW + 7 * 3600, fetch=f)            # news/interest expired, YouTube not
    assert not any("/youtube/v3/search" in u for u in f.calls[n:])


def test_youtube_searches_are_capped_per_day(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "k")
    monkeypatch.setattr(lookup, "YT_SEARCHES_PER_DAY", 2)
    f = make_fetch(wiki={}, yt=YT)
    for topic in ("alpha one", "beta two"):
        assert lookup.research(topic, now=NOW, fetch=f)["youtube"] is not None
    third = lookup.research("gamma three", now=NOW, fetch=f)
    assert third["youtube"] is None and "Daily limit" in third["youtube_note"]


def test_no_key_and_dead_sources_do_not_break_research(monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    r = lookup.research("harshad mehta", now=NOW, fetch=make_fetch(fail=("news.google", "w/api.php")))
    assert r["news"] is None and r["interest"] is None and r["youtube"] is None
    assert sorted(e.split(":")[0] for e in r["errors"]) == ["interest", "news"]
    assert "YOUTUBE_API_KEY" in r["youtube_note"] and r["verdict"]["state"] == "DORMANT"


# --- the generator uses it -----------------------------------------------------

def test_strategy_reads_the_topics_own_trend_when_the_tracker_has_nothing():
    look = {"verdict": {"state": "STEADY", "reasons": ["Wikipedia “Harshad Mehta”: about 1,400 readers a day"]},
            "youtube": {"found": 25, "videos": [{"title": "T", "channel": "C", "views": 632000, "age_days": 10}],
                        "shorts_share_top": 0.6, "hindi_title_share": 0.5, "uploads_last_7d": 12, "top_kind_mix": {}}}
    s = content.strategy({"matched": 0, "lookup": look, "days_of_history": 1})
    assert s["state"] == "STEADY" and s["mode"] == "evergreen" and "evergreen" in s["headline"].lower()
    text = " ".join(s["notes"])
    assert "1,400 readers" in text and "632,000 views" in text and "60% of the top videos are Shorts" in text
    assert "Hindi" in text and "crowded" in text
    assert "trend history collected" not in text       # that caveat is about the tracker, not this lookup


def test_live_board_state_wins_over_the_lookup():
    s = content.strategy({"matched": 9, "now": {"state": "RISING", "velocity_pct": 80},
                          "lookup": {"verdict": {"state": "FADING", "reasons": ["r"]}}})
    assert s["state"] == "RISING" and s["mode"] == "news" and "r" in s["notes"]


def test_generic_only_hashtags_are_flagged_and_specific_ones_pass():
    weak = content.lint({"hashtags": ["StockMarket", "IndianEconomy"]}, "harshad mehta scam", "")
    assert len(weak) == 1 and "names the subject" in weak[0] and "harshad" in weak[0]
    assert content.lint({"hashtags": ["HarshadMehta", "StockMarket"]}, "harshad mehta scam", "") == []
    assert content.lint({"hashtags": []}, "harshad mehta scam", "") == []


def test_hooks_are_measured_against_the_platform_limit():
    fields = content.formats()["twitter"]["fields"]
    pkg, notes = content.enforce({"description": "d", "hashtags": [], "hooks": [
        {"text": "  Short   hook  ", "why": "w"}, {"text": "x" * 130, "why": "w"}, {"text": "", "why": ""},
        {"text": "ok", "why": "w"}, {"text": "extra", "why": "w"}]}, fields)
    assert [h["text"] for h in pkg["hooks"]] == ["Short hook", "x" * 130, "ok"]
    assert pkg["hooks"][1]["over_limit"] is True and any("hook is longer" in n for n in notes)
    for pid in ("instagram_reel", "facebook", "twitter", "linkedin"):
        assert "hooks" in content.formats()[pid]["fields"] and "hooks" in content._schema(content.formats()[pid]["fields"])["required"]
    assert "hooks" not in content.formats()["youtube"]["fields"]       # YouTube has title options instead


def test_copy_that_never_names_the_subject_is_flagged():
    pkg = {"hooks": [{"text": "The 1992 market crash changed Indian stock regulations forever."},
                     {"text": "Can a manipulation of that scale happen today?"}],
           "description": "How did the 1992 market manipulation rewrite regulations? We break it down.",
           "hashtags": ["HarshadMehta"]}
    issues = content.lint(pkg, "harshad mehta scam", "")
    assert any("names the subject" in i and "harshad" in i for i in issues)          # hooks
    assert any("never names the subject in its opening" in i for i in issues)        # body
    ok = {"hooks": [{"text": "How did Harshad Mehta move the 1992 market with bank receipts?"},
                    {"text": "Could it happen again today?"}],
          "description": "Harshad Mehta and the 1992 securities scam: what broke, and what changed after.",
          "hashtags": ["HarshadMehta"]}
    assert content.lint(ok, "harshad mehta scam", "") == []
    # a topic made only of describing words has no name to demand
    assert content.lint({"hooks": [{"text": "Why prices fell"}], "description": "x", "hashtags": []},
                        "stock market crash today", "") == []


EV = {"lookup": {"interest": {"article": "Harshad Mehta", "last7": 1401, "prev7": 1232, "avg_per_day": 1411, "peak": 2362},
                 "youtube": {"videos": [{"views": 632908}]}}}


def test_research_figures_must_not_leak_into_the_copy():
    pkg = {"hooks": [{"text": "1,401 daily readers are searching for the truth behind Harshad Mehta."}],
           "description": "Harshad Mehta and 1992.", "hashtags": ["HarshadMehta"]}
    issues = content.lint(pkg, "harshad mehta scam", "", EV)
    assert any("1,401 comes from the research" in i for i in issues)
    pkg["hooks"][0]["text"] = "A rival video has 632908 views on Harshad Mehta."
    assert any("632,908" in i for i in content.lint(pkg, "harshad mehta scam", "", EV))
    # the same number is fine when the creator supplied it, and years are never research figures
    pkg["hooks"][0]["text"] = "Harshad Mehta moved 1,401 crore, and 1992 was never the same."
    assert content.lint(pkg, "harshad mehta scam", "he moved Rs 1,401 crore", EV) == []


def test_a_lowercase_pasted_name_is_flagged():
    pkg = {"hooks": [{"text": "Do you know how the harshad mehta scam 1992 bypassed the banks?"}],
           "description": "What happened in the harshad mehta scam 1992?", "hashtags": ["HarshadMehta"]}
    issues = content.lint(pkg, "harshad mehta scam", "", EV)
    assert any("“Harshad”, not “harshad”" in i for i in issues)
    # a correct use elsewhere does not excuse a lower-case one
    both = {"hooks": [{"text": "Harshad Mehta, properly named."}],
            "description": "Harshad Mehta. Also the harshad mehta scam 1992.", "hashtags": ["HarshadMehta"]}
    assert any("not “harshad”" in i for i in content.lint(both, "harshad mehta scam", "", EV))
    pkg = {"hooks": [{"text": "How did Harshad Mehta bypass the banks in 1992?"}],
           "description": "Harshad Mehta, 1992.", "hashtags": ["HarshadMehta"]}
    assert content.lint(pkg, "harshad mehta scam", "", EV) == []
    assert content.lint(pkg, "harshad mehta scam", "") == []           # no evidence -> nothing to compare


def test_research_talk_wrong_language_and_keyword_stuffing_are_flagged():
    ev = {**EV, "searches": ["harshad mehta scam 1992", "harshad mehta scam movie", "harshad mehta scam web series"]}
    talk = {"hooks": [{"text": "Over 380 views per hour on recent creator videos show interest in Harshad Mehta."}],
            "description": "Harshad Mehta.", "hashtags": ["HarshadMehta"]}
    assert any("talks about the research" in i for i in content.lint(talk, "harshad mehta scam", "", ev))

    hindi = {"titles": [{"text": "Harshad Mehta scam explained in Hindi: how 1992 happened"}],
             "description": "Harshad Mehta.", "hashtags": ["HarshadMehta"]}
    assert any("Hindi-language search phrase" in i for i in content.lint(hindi, "harshad mehta scam", "", ev, "english"))
    assert not any("Hindi-language" in i for i in content.lint(hindi, "harshad mehta scam", "", ev, "hinglish"))

    stuffed = {"description": "Harshad Mehta: the Harshad Mehta scam 1992, beyond the Harshad Mehta scam movie and "
                              "the Harshad Mehta scam web series.", "hashtags": ["HarshadMehta"]}
    assert any("keyword stuffing" in i for i in content.lint(stuffed, "harshad mehta scam", "", ev))
    one = {"description": "Harshad Mehta scam 1992: how bank receipts were used.", "hashtags": ["HarshadMehta"]}
    assert content.lint(one, "harshad mehta scam", "", ev) == []


def test_over_long_tags_are_dropped_not_cut_mid_word():
    fields = content.formats()["youtube"]["fields"]
    pkg, _ = content.enforce({"titles": [], "description": "", "hashtags": [], "thumbnail_text": [],
                              "pinned_comment": "", "tags": ["harshad mehta scam explained in hindi", "scam 1992"]},
                             fields)
    assert pkg["tags"] == ["scam 1992"]
