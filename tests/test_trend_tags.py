"""Tags and hashtags other channels use on a subject right now — offline."""
import types

import pytest

from engine import content, trend_tags as tt


def src(channel, hashtags=(), tags=(), vph=100, age=3, title="Stock market loss reality"):
    return {"title": title, "channel": channel, "views_per_hour": vph, "age_days": age,
            "hashtags": list(hashtags), "tags": list(tags)}


# --- harvest -------------------------------------------------------------------------

def test_harvest_reads_title_description_and_tag_box():
    h, t = tt.harvest({"title": "Loss ho gaya 😭 #StockMarket #shorts",
                       "description": "Dard.\n#StockMarket #ShareMarketLoss #शेयरबाजार #2026",
                       "tags": ["stock market loss", " ", "trading psychology"]})
    assert h == ["StockMarket", "shorts", "ShareMarketLoss", "शेयरबाजार", "2026"]
    assert t == ["stock market loss", "trading psychology"]


def test_harvest_survives_missing_fields():
    assert tt.harvest({}) == ([], [])


# --- aggregate -----------------------------------------------------------------------------

def test_one_channels_template_is_not_a_trend():
    out = tt.aggregate([src("A", ["StockMarketLoss"]), src("A", ["StockMarketLoss"])])
    assert out == []


def test_the_same_label_written_three_ways_counts_together():
    out = tt.aggregate([src("A", ["StockMarket"]), src("B", tags=["stock market"]), src("C", tags=["stockmarket"])])
    assert len(out) == 1 and out[0]["channels"] == 3 and out[0]["text"] == "StockMarket"


def test_junk_channel_names_and_brand_never_count():
    sources = [src("Trader Raj", ["shorts", "viral", "TraderRaj", "TheMarketTales", "shorts2026", "LossStatus"]),
               src("Paisa Talks", ["Shorts", "fyp", "TraderRaj", "TheMarketTales", "trending2026", "LossStatus"])]
    out = tt.aggregate(sources, exclude={"TheMarketTales"})
    assert [t["text"] for t in out] == ["LossStatus"]


def test_off_subject_words_and_overlong_labels_are_dropped():
    sources = [src("A", ["IranWarMarketCrash", "ThisIsAVeryLongHashtagNobodyFollows", "RedPnl"]),
               src("B", ["IranWarMarketCrash", "ThisIsAVeryLongHashtagNobodyFollows", "RedPnl"])]
    assert [t["text"] for t in tt.aggregate(sources, noise={"iran"})] == ["RedPnl"]


def test_ranked_by_channels_then_by_speed_and_marked_fresh():
    sources = [src("A", ["Slow", "Fast"], vph=10, age=20), src("B", ["Slow", "Fast"], vph=10, age=20),
               src("C", ["Slow"], vph=1, age=20), src("D", ["Fast"], vph=5000, age=2)]
    out = tt.aggregate(sources)
    assert [t["text"] for t in out] == ["Slow", "Fast"] or out[0]["channels"] >= out[1]["channels"]
    fast = next(t for t in out if t["text"] == "Fast")
    slow = next(t for t in out if t["text"] == "Slow")
    assert fast["fresh"] and not slow["fresh"]


def test_aggregate_handles_nothing():
    assert tt.aggregate([]) == [] and tt.aggregate([{"channel": "", "hashtags": None}]) == []


# --- relevant ------------------------------------------------------------------------------

VOCAB = {"stock", "market", "loss", "reality", "red", "pnl", "share", "india"}


@pytest.mark.parametrize("label,ok", [
    ("#StockMarketReality", True), ("stock market loss", True), ("stockmarketloss", True),
    ("RedPNL", True), ("BankNifty", False), ("stock market today", False), ("nifty50", False),
    ("", False), ("lossstatusxyz", False),
])
def test_relevant_only_when_the_video_says_every_word(label, ok):
    assert tt.relevant(label, VOCAB) is ok


def test_as_hashtag():
    assert tt.as_hashtag("stock market reality") == "StockMarketReality"
    assert tt.as_hashtag("#RedPnl") == "RedPnl" and tt.as_hashtag("stockmarket") == "stockmarket"


# --- add -----------------------------------------------------------------------------------

FIELDS = {"hashtags": {"min": 3, "max": 10}, "tags": {"max_total": 500}}
TRENDS = [{"text": "ShareMarketLoss", "key": "sharemarketloss", "kind": "hashtag", "channels": 4},
          {"text": "BankNifty", "key": "banknifty", "kind": "hashtag", "channels": 9},
          {"text": "शेयरबाजार", "key": "शेयरबाजार", "kind": "hashtag", "channels": 3},
          {"text": "stock market reality", "key": "stockmarketreality", "kind": "tag", "channels": 3}]


def test_add_puts_relevant_trends_in_when_the_writer_used_none():
    pkg = {"hashtags": ["StockMarketLoss", "TradingPsychology", "TheMarketTales"], "tags": ["a", "b", "c", "d"]}
    added = tt.add(pkg, TRENDS, VOCAB, FIELDS, "hinglish")
    assert pkg["hashtags"][:3] == ["StockMarketLoss", "ShareMarketLoss", "StockMarketReality"]
    assert "BankNifty" not in pkg["hashtags"] and "शेयरबाजार" not in pkg["hashtags"]     # off-video / wrong script
    assert pkg["tags"][3:5] == ["share market loss", "stock market reality"]
    assert "#ShareMarketLoss" in added


def test_hindi_packages_may_take_devanagari_trends():
    pkg = {"hashtags": ["X"], "tags": []}
    tt.add(pkg, TRENDS, VOCAB | {"शेयरबाजार"}, FIELDS, "hindi")
    assert "शेयरबाजार" in pkg["hashtags"]


def test_add_leaves_a_package_alone_when_the_writer_already_used_a_trend():
    pkg = {"hashtags": ["ShareMarketLoss", "Y"], "tags": ["stock market reality"]}
    assert tt.add(pkg, TRENDS, VOCAB, FIELDS, "hinglish") == []
    assert pkg == {"hashtags": ["ShareMarketLoss", "Y"], "tags": ["stock market reality"]}


def test_add_on_platforms_without_tags_or_hashtags():
    pkg = {"description": "x"}
    assert tt.add(pkg, TRENDS, VOCAB, {"description": {}}, "english") == [] and pkg == {"description": "x"}


def test_used_in_reads_what_will_be_pasted():
    ready = [{"label": "Description", "text": "Dard. #ShareMarketLoss"},
             {"label": "Tags", "text": "stock market reality, red pnl"}]
    assert [t["text"] for t in tt.used_in(ready, TRENDS)] == ["ShareMarketLoss", "stock market reality"]


# --- through the content generator --------------------------------------------------------

def test_analyse_turns_recent_videos_into_trend_tags(monkeypatch):
    yt = {"found": 3, "videos": [], "tag_sources": [
        src("A", ["ShareMarketLoss", "shorts"], ["stock market reality"]),
        src("B", ["ShareMarketLoss"], ["stock market reality"]),
        src("C", ["IranWar"], title="Iran war market crash"), src("D", ["IranWar"], title="Iran war stock crash")]}
    monkeypatch.setattr(content, "insight", types.SimpleNamespace(
        analyse=lambda topic, **k: {"searches": ["stock market loss", "stock market iran war"]},
        suggest=lambda *a, **k: []))
    monkeypatch.setattr(content, "lookup", types.SimpleNamespace(
        research=lambda topic, **k: {"topic": topic, "errors": [], "verdict": {}, "youtube": yt}))
    seen = {"seen": {"content_type": "mood", "subject": "stock market loss", "search_terms": ["stock market loss"],
                     "core_line": "Loss bhi sikhata hai", "on_screen_text": ["Loss bhi sikhata hai"]}}
    out = content.analyse("youtube_shorts", "stock market loss", media=seen)
    texts = [t["text"] for t in out["evidence"]["trend_tags"]]
    assert "ShareMarketLoss" in texts and "IranWar" not in texts and "shorts" not in texts
    block = content._evidence_block(out["evidence"], out["strategy"])
    assert "#ShareMarketLoss (2 channels, this week)" in block and "truly describe THIS video" in block


def test_broad_niche_labels_rank_last_and_do_not_block_specific_ones():
    sources = [src(c, ["ShareMarket", "ShareMarketLoss"]) for c in "ABCDEFG"][:7]
    sources += [src("H", ["ShareMarket"]), src("I", ["ShareMarket"])]
    out = tt.aggregate(sources)
    assert [t["text"] for t in out] == ["ShareMarketLoss", "ShareMarket"] and out[1]["broad"]
    pkg = {"hashtags": ["ShareMarket", "X"], "tags": []}       # writer used only the broad one
    tt.add(pkg, out, VOCAB, FIELDS, "hinglish")
    assert pkg["hashtags"] == ["ShareMarket", "ShareMarketLoss", "X"]


def test_an_old_cached_search_without_labels_is_looked_up_again(monkeypatch):
    import json
    import time
    from contextlib import closing
    from engine.trends import lookup, store
    monkeypatch.setenv("YOUTUBE_API_KEY", "k")
    now = time.time()
    with closing(store.connect()) as conn:
        conn.executescript(lookup._TABLE)
        lookup._save(conn, lookup._key("old topic"), "youtube", {"data": {"found": 1, "videos": []}}, now - 60)
    calls = []

    def fetch(url):
        calls.append(url)
        return json.dumps({"items": []})
    out = lookup.research("old topic", now=now, fetch=fetch)
    assert any("youtube/v3/search" in u for u in calls) and out["youtube"]["tag_sources"] == []


def test_labels_from_unrelated_news_in_the_search_are_left_out():
    sources = [src(c, ["Nvidia", "Politics", "StockMarketCrash", "ShareMarketLoss", "ShareMarket", "LossesTeach"])
               for c in "AB"]
    out = tt.aggregate(sources, subject=tt.subject_words("share market loss"))
    assert [t["text"] for t in out] == ["ShareMarketLoss", "LossesTeach", "ShareMarket"]


def test_a_purely_generic_subject_keeps_everything():
    assert tt.subject_words("stock market today") == set()
    out = tt.aggregate([src(c, ["Nvidia"]) for c in "AB"], subject=tt.subject_words("stock market today"))
    assert [t["text"] for t in out] == ["Nvidia"]
