"""Trend tracker — parsers, topic grouping, lifecycle maths, collection, routes.

Everything runs offline: collectors are fed synthetic payloads through the
injectable `fetch`, at a fixed clock.
"""

import json
from contextlib import closing
from email.utils import formatdate

import pytest

import app as app_module
from engine import analytics
from engine.trends import lifecycle, service, sources, store, topics
from engine.trends.text import is_niche, tokens

NOW = 1_790_000_000.0
H = 3600
CFG = {"interval_minutes": 30, "window_hours": 6, "report_hours": 6,
       "geo": "IN", "news": [{"topic": "BUSINESS"}], "youtube": [],
       "niche_keywords": [], "weights": {"news": 1.0, "gtrends": 1.0, "youtube": 0.8},
       "lifecycle": {}}


# --- payload builders ----------------------------------------------------------

def news_xml(rows, base=NOW):
    """rows = [(title, publisher, hours_ago)] — hours before `base`"""
    items = "".join(
        f"<item><title>{t} - {p}</title><link>https://news.example/{i}</link>"
        f"<pubDate>{formatdate(base - h * H, usegmt=True)}</pubDate><source url='x'>{p}</source></item>"
        for i, (t, p, h) in enumerate(rows))
    return f"<rss><channel>{items}</channel></rss>"


def gtrends_xml(rows):
    """rows = [(query, traffic, [headline, ...])]"""
    ns = "https://trends.google.com/trending/rss"
    items = "".join(
        f"<item><title>{q}</title><ht:approx_traffic>{tr}</ht:approx_traffic>"
        f"<pubDate>{formatdate(NOW - H, usegmt=True)}</pubDate>"
        + "".join(f"<ht:news_item><ht:news_item_title>{h}</ht:news_item_title>"
                  f"<ht:news_item_url>https://n.example/a</ht:news_item_url></ht:news_item>" for h in heads)
        + "</item>" for q, tr, heads in rows)
    return f'<rss xmlns:ht="{ns}"><channel>{items}</channel></rss>'


def fetcher(news=None, gtrends=None, fail=(), base=NOW):
    def fetch(url):
        for key in fail:
            if key in url:
                raise OSError(f"{key} down")
        if "trends.google" in url:
            return gtrends_xml(gtrends or [])
        if "news.google" in url:
            return news_xml(news or [], base)
        raise OSError("not stubbed")   # wikipedia: no file -> logged as a failed run
    return fetch


def board_states(board):
    return {e["label"]: e for rows in board["states"].values() for e in rows}


# --- parsers -------------------------------------------------------------------

@pytest.mark.parametrize("raw,want", [("200+", 200), ("2K+", 2000), ("1M+", 1e6),
                                      ("20,000+", 20000), ("", 0), (None, 0), ("lots", 0)])
def test_parse_traffic(raw, want):
    assert sources.parse_traffic(raw) == want


def test_parse_gtrends_keeps_query_volume_and_context():
    recs = sources.parse_gtrends(gtrends_xml([("tata motors share", "5K+", ["Tata Motors demerger date set"])]))
    assert recs[0]["title"] == "tata motors share" and recs[0]["value"] == 5000
    assert "demerger" in recs[0]["extra_text"] and recs[0]["source"] == "gtrends"


def test_parse_news_strips_publisher_and_dedupes_across_feeds():
    a = sources.parse_news(news_xml([("RBI hikes repo rate", "Mint", 1)]))[0]
    b = sources.parse_news(news_xml([("RBI hikes repo rate", "Mint", 2)]))[0]   # another feed
    c = sources.parse_news(news_xml([("RBI hikes repo rate", "Reuters", 1)]))[0]
    assert a["title"] == "RBI hikes repo rate" and a["publisher"] == "Mint"
    assert a["ext_id"] == b["ext_id"] != c["ext_id"]


def test_parse_youtube_reads_views():
    xml = ('<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015"'
           ' xmlns:media="http://search.yahoo.com/mrss/"><entry><yt:videoId>abc</yt:videoId>'
           '<title>Nifty crash explained</title><published>2026-10-06T10:00:00+00:00</published>'
           '<media:group><media:community><media:statistics views="12345"/></media:community>'
           '</media:group></entry></feed>')
    rec = sources.parse_youtube(xml, "Chan")[0]
    assert rec["ext_id"] == "abc" and rec["value"] == 12345 and rec["publisher"] == "Chan"


def test_parse_wiki_skips_non_articles():
    payload = json.dumps({"items": [{"year": "2026", "month": "10", "day": "05", "articles": [
        {"article": "Main_Page", "project": "en.wikipedia", "views_ceil": 9},
        {"article": "Special:Search", "project": "en.wikipedia", "views_ceil": 9},
        {"article": "Tata_Motors", "project": "hi.wikipedia", "views_ceil": 9},
        {"article": "Tata_Motors", "project": "en.wikipedia", "views_ceil": 4200}]}]})
    recs = sources.parse_wiki(payload)
    assert [(r["title"], r["value"]) for r in recs] == [("Tata Motors", 4200)]


# --- text ----------------------------------------------------------------------

def test_tokens_stem_stop_and_unicode():
    assert tokens("The shares of Companies are rising today") == {"share", "company", "rising"}
    assert tokens("शेयर बाजार") == {"शेयर", "बाजार"}          # Hindi must not be dropped
    assert tokens("22,700 2026") == set()


def test_niche_lexicon_matches_stemmed_forms():
    assert is_niche(tokens("Q2 results today"))                 # "results" is stored stemmed
    assert not is_niche(tokens("ind vs wi t20"))


@pytest.mark.parametrize("title", [
    "Honda Elevate Facelift Launched in India at Rs. 11.80 Lakh",       # a price is not a markets story
    "Honda XR300 Rally Launched at Rs. 2.08 Lakh",
    "Rahul Gandhi Offers Priyanka An Energy Drink; She Takes A Sip",
    "Opposition rally in Delhi draws lakhs",
])
def test_lookalike_finance_words_do_not_mark_a_story_as_markets(title):
    assert not is_niche(tokens(title))


# --- topic grouping ------------------------------------------------------------

def _assign(rows, source="news"):
    """rows = [(title, hours_ago)] -> {title: topic_id}"""
    out = {}
    with closing(store.connect()) as conn:
        recs = []
        for i, (title, h) in enumerate(rows):
            rec = {"source": source, "ext_id": f"{source}{i}{title}", "title": title, "url": "",
                   "publisher": f"P{i}", "published_at": NOW - h * H, "value": None, "extra_text": ""}
            recs.append((store.upsert_item(conn, rec, NOW)[0], rec))
        topics.assign(conn, recs, NOW)
        conn.commit()
        for item_id, rec in recs:
            out[rec["title"]] = conn.execute("SELECT topic_id FROM items WHERE id=?",
                                             (item_id,)).fetchone()[0]
    return out


def test_same_story_different_wording_is_one_topic():
    t = _assign([("Jio Platforms plans to launch IPO on October 21", 3),
                 ("Jio IPO to open on October 21: expected price band", 2),
                 ("Ambani's Jio Platforms seeks $114 billion valuation in IPO", 1)])
    assert len(set(t.values())) == 1


def test_boilerplate_does_not_merge_different_companies():
    t = _assign([("Vishal Nirmiti IPO allotment status: check online", 3),
                 ("Dove Soft IPO allotment status: check online", 2),
                 ("Omara Ventures IPO allotment status online", 1)])
    assert len(set(t.values())) == 3


def test_topic_age_is_its_earliest_member_not_processing_order():
    t = _assign([("Trent Zudio store expansion plan", 1), ("Trent Zudio expansion: 200 stores", 30)])
    with closing(store.connect()) as conn:
        first = conn.execute("SELECT first_seen FROM topics WHERE id=?",
                             (list(t.values())[0],)).fetchone()[0]
    assert first == NOW - 30 * H


def test_wikipedia_page_attaches_only_when_fully_covered_and_never_starts_a_topic():
    t = _assign([("Tata Motors demerger record date announced", 2),
                 ("Tata Motors demerger: what shareholders get", 1)])
    w = _assign([("Tata Motors", 0), ("India at the Asian Games", 0), ("Demerger Law Reform", 0)],
                source="wiki")
    assert w["Tata Motors"] == list(t.values())[0]
    assert w["India at the Asian Games"] is None and w["Demerger Law Reform"] is None
    with closing(store.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0] == 1


def test_topic_is_in_niche_only_if_a_fair_share_of_its_headlines_are():
    """One stray finance word in a politics topic must not put it on the niche board."""
    politics = [("Gandhi protest march Delhi police detained", 3), ("Gandhi protest march Delhi traffic curbs", 2),
                ("Gandhi protest march Delhi opposition leaders", 2), ("Gandhi protest march Delhi rattles market", 1)]
    markets = [("Trent Zudio expansion lifts shares", 3), ("Trent Zudio expansion store count", 2)]
    t = _assign(politics + markets)
    with closing(store.connect()) as conn:
        niche = {r["id"]: r["niche"] for r in conn.execute("SELECT id, niche FROM topics")}
    assert niche[t[politics[0][0]]] == 0          # 1 of 4 headlines on-niche = 25%, below the 40% bar
    assert niche[t[markets[0][0]]] == 1           # 1 of 2


def test_blocked_publisher_and_junk_titles_stop_counting_even_if_already_stored():
    news = (_story(JIO, 6, 3, "a") + _filler(9, 9, "p") + _filler(4, 3, "r")
            + [("Market research in your language", "Univest", 3)] * 1
            + [(f"Promo offer research app number{i}", "Univest", 3) for i in range(5)]
            + [("News by CNBC TV18 on TradingView, 2026-10-06 — cnbctv:e7dda5237094b:0", "TradingView", 3)])
    cfg_open = {**CFG, "ignore_publishers": []}
    service.collect(now=NOW, cfg=cfg_open, fetch=fetcher(news=news))          # stored before the rule existed
    with closing(store.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM items WHERE publisher='Univest'").fetchone()[0] == 6
        # the junk-id title is refused at the door regardless of config
        assert conn.execute("SELECT COUNT(*) FROM items WHERE publisher='TradingView'").fetchone()[0] == 0
    before = board_states(service.compute(now=NOW, cfg=cfg_open))[JIO]["score"]
    after = board_states(service.compute(now=NOW, cfg={**CFG, "ignore_publishers": ["univest"]}))[JIO]["score"]
    assert after > before                          # the promo items no longer dilute everyone's share


def test_merged_topic_reads_as_moved_not_dropped_in_the_next_update():
    first = _first_update()
    jio_id = _cls(first)[JIO]["id"]
    with closing(store.connect()) as conn:         # simulate the Jio topic being folded into a new id
        new_id = conn.execute("INSERT INTO topics (tokens, n_items, first_seen, last_seen, niche)"
                              " SELECT tokens, n_items, first_seen, last_seen, niche FROM topics WHERE id=?",
                              (jio_id,)).lastrowid
        conn.execute("UPDATE items SET topic_id=? WHERE topic_id=?", (new_id, jio_id))
        conn.execute("DELETE FROM topics WHERE id=?", (jio_id,))
        conn.execute("INSERT INTO topic_merges (old_id, new_id) VALUES (?,?)", (jio_id, new_id))
        conn.commit()
    u = service.make_update(now=NOW + 60, cfg=CFG)
    assert JIO not in {d["label"] for d in u["dropped"]}
    assert _cls(u)[JIO]["id"] == new_id and _cls(u)[JIO]["prev_cls"] == "SAME"


def test_fragments_of_one_story_are_merged():
    with closing(store.connect()) as conn:
        idx = topics.TopicIndex(conn, NOW)
        big = idx.create({"rbi", "repo", "rate", "hike", "mpc"}, NOW, True)
        for _ in range(4):
            idx.absorb(big, {"rbi", "repo", "rate", "hike", "mpc"}, NOW, True)
        small = idx.create({"rbi", "mpc", "hike", "emi"}, NOW - H, True)
        other = idx.create({"gold", "silver", "bullion"}, NOW, True)
        conn.execute("INSERT INTO items (source, ext_id, title, first_seen, topic_id)"
                     " VALUES ('news','x','t',?,?)", (NOW, small))
        assert topics.merge_fragments(conn, idx) == 1
        assert conn.execute("SELECT topic_id FROM items WHERE ext_id='x'").fetchone()[0] == big
        ids = {r[0] for r in conn.execute("SELECT id FROM topics")}
        assert ids == {big, other}
        assert conn.execute("SELECT first_seen FROM topics WHERE id=?", (big,)).fetchone()[0] == NOW - H


# --- lifecycle maths -----------------------------------------------------------

def test_classify_states():
    c = lifecycle.classify
    assert c(0.20, 0.10, 0.20, age_hours=30, idle_hours=0)[0] == lifecycle.RISING
    assert c(0.05, 0.10, 0.10, age_hours=30, idle_hours=0)[0] == lifecycle.FADING
    assert c(0.10, 0.10, 0.10, age_hours=30, idle_hours=0)[0] == lifecycle.PEAKING
    assert c(0.10, 0.11, 0.30, age_hours=30, idle_hours=0)[0] == lifecycle.PEAKING  # -9% is inside the band
    assert c(0.0, 0.10, 0.10, age_hours=30, idle_hours=6)[0] == lifecycle.FADING
    assert c(0.10, 0.10, 0.10, age_hours=30, idle_hours=48)[0] == lifecycle.DEAD
    assert c(None, None, 0, age_hours=30, idle_hours=0)[0] == lifecycle.WATCH
    assert c(0.10, None, 0.10, age_hours=30, idle_hours=0)[0] == lifecycle.WATCH    # direction unknown


def test_the_band_alone_decides_same_versus_fading():
    c = lifecycle.classify
    for recent in (0.071, 0.099, 0.10, 0.114, 0.129):          # -29% .. +29%, far below a 0.50 peak
        assert c(recent, 0.10, 0.50, age_hours=40, idle_hours=0)[0] == lifecycle.PEAKING, recent
    assert c(0.07, 0.10, 0.50, age_hours=40, idle_hours=0)[0] == lifecycle.FADING
    assert c(0.13, 0.10, 0.50, age_hours=40, idle_hours=0)[0] == lifecycle.RISING


def test_emerging_needs_young_and_not_yet_everywhere():
    c = lifecycle.classify
    assert c(0.2, 0.0, 0.2, age_hours=3, idle_hours=0, breadth=3)[0] == lifecycle.EMERGING
    assert c(0.2, 0.0, 0.2, age_hours=3, idle_hours=0, breadth=14)[0] == lifecycle.RISING  # already mainstream
    assert c(0.2, 0.0, 0.2, age_hours=20, idle_hours=0, breadth=3)[0] == lifecycle.RISING  # not young


def test_window_score_only_uses_the_given_sources():
    vals, totals = {"news": 5, "youtube": 900}, {"news": 10, "youtube": 1000}
    w = {"news": 1.0, "youtube": 1.0}
    assert lifecycle.window_score(vals, totals, w, {"news"}) == 0.5
    assert lifecycle.window_score(vals, totals, w, {"news", "youtube"}) == 0.7
    assert lifecycle.window_score(vals, totals, w, set()) is None
    assert lifecycle.usable({"news": 7, "youtube": 5000}) == {"youtube"}   # 7 articles is too thin


# --- collect + compute, end to end ---------------------------------------------

def _story(title, n, hours_ago, tag):
    return [(title, f"{tag}{i}", hours_ago) for i in range(n)]


def _filler(n, hours_ago, tag):
    return [(f"alpha{tag}{i} beta{tag}{i} gamma{tag}{i}", f"F{tag}{i}", hours_ago) for i in range(n)]


JIO, TRENT, HYFUN = ("Jio Platforms IPO valuation soars", "Trent Zudio expansion stores plan",
                     "Hyfun Foods frozen potato export")


def test_share_of_voice_not_raw_volume_drives_the_state():
    """News volume HALVES between the windows (as it does every night). Jio's
    count halves with it — same share — so it must not be called fading."""
    news = (_story(JIO, 10, 9, "a") + _story(TRENT, 2, 9, "b") + _story(HYFUN, 4, 9, "c") + _filler(8, 9, "p")
            + _story(JIO, 5, 3, "d") + _story(TRENT, 4, 3, "e") + _story(HYFUN, 1, 3, "f") + _filler(2, 3, "r"))
    service.collect(now=NOW, cfg=CFG, fetch=fetcher(news=news))
    board = service.compute(now=NOW, cfg=CFG, write=True)
    assert board["window_hours"] == 6 and board["comparable"] is True
    s = board_states(board)
    assert s[JIO]["state"] == lifecycle.PEAKING and s[JIO]["velocity_pct"] == 0
    assert s[TRENT]["state"] == lifecycle.RISING and s[TRENT]["velocity_pct"] == 300
    assert s[HYFUN]["state"] == lifecycle.FADING and s[HYFUN]["velocity_pct"] == -50
    # single-publisher filler stories are stored but never shown as trends
    assert not any(label.startswith("alpha") for label in s)


def test_one_outlet_flooding_a_story_does_not_outweigh_broad_coverage():
    """20 near-duplicate updates from ONE desk vs 6 outlets with one article each."""
    flood = [(f"Sensex live blog update number{i} banks rally", "LiveDesk", 3) for i in range(20)]
    broad = _story(JIO, 6, 3, "a")
    prev = _story(JIO, 6, 9, "b") + [(f"Sensex live blog update old{i} banks rally", "LiveDesk", 9) for i in range(2)] \
        + _filler(8, 9, "p")
    service.collect(now=NOW, cfg=CFG, fetch=fetcher(news=flood + broad + prev + _filler(8, 3, "r")))
    board = service.compute(now=NOW, cfg=CFG)
    s = board_states(board)
    assert s[JIO]["score"] > 0
    live = [e for label, e in s.items() if "live blog" in label]
    # the flood is a one-publisher topic: it never reaches the board at all...
    assert live == []
    # ...and it does not dilute everyone else's share: Jio is 6 of (6 + 2 capped + 8 filler)
    assert s[JIO]["score"] == pytest.approx(6 / 16, abs=1e-4)


def test_one_dead_source_does_not_stop_the_others_and_is_reported():
    news = _story(JIO, 9, 3, "a") + _story(JIO, 9, 9, "b")
    out = service.collect(now=NOW, cfg=CFG, fetch=fetcher(news=news, fail=("trends.google",)))
    assert out["gtrends"]["ok"] is False and "down" in out["gtrends"]["error"]
    assert out["news"]["ok"] is True and out["news"]["items"] == 18
    board = service.compute(now=NOW, cfg=CFG)
    src = {x["source"]: x for x in board["sources"]}
    assert src["gtrends"]["ok"] is False and src["gtrends"]["stale"] is True
    assert src["news"]["ok"] is True and src["news"]["stale"] is False
    assert JIO in board_states(board)


def test_stale_source_is_flagged_when_collection_stops():
    service.collect(now=NOW, cfg=CFG, fetch=fetcher(news=_story(JIO, 9, 3, "a")))
    later = service.compute(now=NOW + 5 * H, cfg=CFG)      # app was off for 5 hours
    assert {x["source"]: x["stale"] for x in later["sources"]}["news"] is True


def test_search_query_joins_the_news_story_and_adds_a_second_source():
    news = _story("Tata Motors demerger record date announced", 9, 3, "a") + _filler(9, 9, "p")
    gt = [("tata motors", "20K+", ["Tata Motors demerger record date"])]
    service.collect(now=NOW, cfg=CFG, fetch=fetcher(news=news, gtrends=gt))
    e = board_states(service.compute(now=NOW, cfg=CFG))["Tata Motors demerger record date announced"]
    assert e["sources"] == ["news", "gtrends"]
    assert any("20,000+ searches" in sig for sig in e["signals"])


def test_recollecting_the_same_feed_adds_nothing():
    f = fetcher(news=_story(JIO, 9, 3, "a"))
    service.collect(now=NOW, cfg=CFG, fetch=f)
    service.collect(now=NOW + 60, cfg=CFG, fetch=f)
    with closing(store.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 9
        assert conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0] == 1


def test_history_records_state_changes_once_and_daily_rollup():
    news = _story(JIO, 9, 3, "a") + _story(JIO, 9, 9, "b")
    service.collect(now=NOW, cfg=CFG, fetch=fetcher(news=news))
    service.compute(now=NOW, cfg=CFG, write=True)
    service.compute(now=NOW + 60, cfg=CFG, write=True)      # same state -> no new transition row
    with closing(store.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM topic_states").fetchone()[0] == 1
    days = service.history_days()
    assert len(days) == 1
    row = service.history(days[0])[0]
    assert row["label"] == JIO and row["path"] == [lifecycle.PEAKING] and row["mentions"] == 18


# --- the periodic update -------------------------------------------------------

SWARA = "Swara Baby Products SEBI nod FirstCry"


def _first_update():
    news = (_story(JIO, 10, 9, "a") + _story(TRENT, 2, 9, "b") + _story(HYFUN, 4, 9, "c") + _filler(8, 9, "p")
            + _story(JIO, 5, 3, "d") + _story(TRENT, 4, 3, "e") + _story(HYFUN, 1, 3, "f") + _filler(2, 3, "r"))
    service.collect(now=NOW, cfg=CFG, fetch=fetcher(news=news))
    return service.maybe_update(now=NOW, cfg=CFG)


def _cls(update):
    return {t["label"]: t for t in update["topics"]}


def test_window_choices_follow_the_configured_window():
    # the chosen window, then double it when the flow is too thin; always >= 4 windows for a sparkline
    assert service._choices({"window_hours": 6}) == ((6.0, 16), (12.0, 8))
    assert service._choices({}) == service._choices({"window_hours": 24})      # 1 day is the default
    assert service._choices({"window_hours": 24}) == ((24.0, 4), (48.0, 4))
    assert service._choices({"window_hours": 24}, base=48) == ((48.0, 4), (96.0, 4))
    assert service.window_options({"window_hours": 24, "window_options": [24, 48]}) == [24.0, 48.0]
    assert service.window_options({"window_hours": 48, "window_options": [24, 48]}) == [48.0, 24.0]


def test_first_update_classes_topics_trending_same_fading():
    assert service.update_due(now=NOW, cfg=CFG) == (True, NOW)       # nothing taken yet
    u = _first_update()
    c = _cls(u)
    assert (c[TRENT]["cls"], c[JIO]["cls"], c[HYFUN]["cls"]) == ("TRENDING", "SAME", "FADING")
    assert u["prev_ts"] is None and u["dropped"] == [] and u["late_hours"] == 0
    assert all(t["prev_cls"] is None for t in u["topics"])
    assert u["window_hours"] == 6 and u["collections"] == 1 and u["expected_collections"] == 12


def test_update_is_taken_only_when_due():
    _first_update()
    assert service.maybe_update(now=NOW + 5.9 * H, cfg=CFG) is None
    assert service.update_due(now=NOW + H, cfg=CFG) == (False, NOW + 6 * H)
    assert service.maybe_update(now=NOW + 6 * H, cfg=CFG) is not None
    assert [u["ts"] for u in service.list_updates()] == [NOW + 6 * H, NOW]


def test_second_update_reports_movement_since_the_first():
    first = _first_update()
    later = NOW + 6 * H
    news = _story(JIO, 6, 3, "g") + _story(SWARA, 3, 2, "h") + _filler(5, 3, "s")    # Trent, HyFun go silent
    service.collect(now=later, cfg=CFG, fetch=fetcher(news=news, base=later))
    u = service.maybe_update(now=later, cfg=CFG)
    c = _cls(u)
    assert u["prev_ts"] == first["ts"] and u["comparable"] is True
    assert (c[JIO]["cls"], c[JIO]["prev_cls"]) == ("SAME", "SAME")                   # no change
    assert (c[TRENT]["cls"], c[TRENT]["prev_cls"]) == ("FADING", "TRENDING")         # turned down
    assert (c[HYFUN]["cls"], c[HYFUN]["prev_cls"]) == ("FADING", "FADING")
    assert c[SWARA]["cls"] == "TRENDING" and c[SWARA]["new"] is True and c[SWARA]["prev_cls"] is None
    assert c[JIO]["new"] is False


def test_update_after_downtime_is_one_late_update_with_drop_offs():
    _first_update()
    much_later = NOW + 60 * H                       # nothing collected for two and a half days
    u = service.maybe_update(now=much_later, cfg=CFG)
    assert u["late_hours"] == 54.0                  # one update, flagged late — no back-filled ones
    assert len(service.list_updates()) == 2
    assert u["topics"] == [] and u["comparable"] is False
    assert {d["label"]: d["prev_cls"] for d in u["dropped"]} == {
        JIO: "SAME", TRENT: "TRENDING", HYFUN: "FADING"}
    assert u["collections"] == 0


def test_get_update_latest_and_by_id():
    assert service.get_update() is None
    first = _first_update()
    second = service.make_update(now=NOW + 6 * H, cfg=CFG)
    assert service.get_update()["id"] == second["id"]
    assert service.get_update(first["id"])["ts"] == NOW
    assert service.get_update(9999) is None


# --- routes --------------------------------------------------------------------

@pytest.fixture
def client():
    return app_module.app.test_client()


def test_trends_route_on_an_empty_store(client):
    app_module.trends.service._board = None
    d = client.get("/api/trends").get_json()
    assert d["sources"] == [] and d["collecting"] is False
    assert all(rows == [] for rows in d["states"].values())


def test_history_route_rejects_unknown_day(client):
    assert client.get("/api/trends/history").get_json() == {"days": [], "topics": []}
    assert client.get("/api/trends/history?day=2026-01-01").status_code == 404
    assert client.get("/api/trends/history?day=../../etc").status_code == 404


def test_updates_route(client):
    d = client.get("/api/trends/updates").get_json()
    assert d["update"] is None and d["updates"] == [] and d["report_hours"] == 24
    assert d["window_options"] == [24.0, 48.0]
    made = service.make_update(now=NOW, cfg=CFG)
    d = client.get("/api/trends/updates").get_json()
    assert d["update"]["id"] == made["id"] and len(d["updates"]) == 1
    assert client.get(f"/api/trends/updates?id={made['id']}").get_json()["update"]["ts"] == NOW
    assert client.get("/api/trends/updates?id=999").status_code == 404
    assert client.get("/api/trends/updates?id=1;drop").status_code == 404


def test_manual_collect_has_a_cooldown(monkeypatch):
    svc = service.TrendService()
    ran = []
    monkeypatch.setattr(svc, "_cycle", lambda: ran.append(1))
    assert svc.trigger()[0] is True
    svc._last_finish = service.time.time()
    started, reason = svc.trigger()
    assert started is False and "try again" in reason
    svc.running = True
    assert svc.trigger() == (False, "a collection is already running")


# --- analytics store -----------------------------------------------------------

FROZEN = {"topic": "Jio IPO", "drafts": {"twitter": {"caption": "hi", "hashtags": ["Jio"]}}}


def test_only_real_outcomes_are_logged_as_posts():
    assert analytics.record_post("twitter", FROZEN, {"status": "skipped"}) is None
    assert analytics.record_post("twitter", FROZEN, {"status": "error"}) is None
    pid = analytics.record_post("twitter", FROZEN, {"status": "dry-run"},
                                {"trend_state": "RISING", "formats": {"twitter": "hot_take"}})
    p = analytics.list_posts()[0]
    assert p["id"] == pid and p["mode"] == "dry-run" and p["trend_state"] == "RISING"
    assert p["format_id"] == "hot_take" and p["metrics"] is None


def test_metrics_validation_and_snapshots_are_kept_not_overwritten():
    pid = analytics.record_post("twitter", FROZEN, {"status": "posted", "url": "https://x.com/1"})
    assert analytics.add_metrics("nope", {"likes": 1}) is False
    for bad in ({}, {"likes": -1}, {"likes": "abc"}, {"likes": ""}):
        with pytest.raises(ValueError):
            analytics.add_metrics(pid, bad)
    assert analytics.add_metrics(pid, {"impressions": 1000, "likes": 30}) is True
    assert analytics.add_metrics(pid, {"impressions": 2000, "likes": 50, "comments": 10, "shares": 20}) is True
    p = analytics.list_posts()[0]
    assert p["snapshots"] == 2
    assert p["metrics"]["engagement"] == 80 and p["metrics"]["engagement_rate"] == 0.04


def test_summary_excludes_dry_runs_and_reports_n():
    dry = analytics.record_post("twitter", FROZEN, {"status": "dry-run"}, {"trend_state": "RISING"})
    analytics.add_metrics(dry, {"impressions": 100, "likes": 90})      # must be ignored
    for state, likes in (("RISING", 60), ("RISING", 40), ("FADING", 10)):
        pid = analytics.record_post("twitter", FROZEN, {"status": "posted"}, {"trend_state": state})
        analytics.add_metrics(pid, {"impressions": 1000, "likes": likes})
    s = analytics.summary()
    assert s["posts"] == 4 and s["live_posts"] == 3 and s["measured"] == 3
    assert s["by_trend_state"] == [{"key": "RISING", "n": 2, "avg_engagement_rate": 0.05},
                                   {"key": "FADING", "n": 1, "avg_engagement_rate": 0.01}]


def test_publish_logs_the_post_with_its_trend_state(client):
    app_module._RUNS["tr"] = {
        "frozen": FROZEN, "meta": {"trend_topic_id": 7, "trend_state": "EMERGING", "formats": {}},
        "review": {"sensitive": False, "gate": {"twitter": {"verdict": "PASS"}}}}
    assert client.post("/api/publish", json={"run_id": "tr", "platform": "twitter"}).get_json()["status"] == "dry-run"
    d = client.get("/api/analytics").get_json()
    assert d["posts"][0]["trend_state"] == "EMERGING" and d["summary"]["posts"] == 1
    pid = d["posts"][0]["id"]
    assert client.post("/api/analytics/metrics", json={"post_id": pid, "likes": -5}).status_code == 400
    assert client.post("/api/analytics/metrics", json={"post_id": "zz", "likes": 5}).status_code == 404
    assert client.post("/api/analytics/metrics", json={"post_id": pid, "likes": 5}).get_json() == {"saved": True}


def test_trend_state_is_looked_up_server_side_not_trusted_from_client(client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    import importlib

    from engine import llm
    importlib.reload(llm)
    fake = {"topic": "t", "brief": {"headline_summary": "h", "sentiment": "s", "trending_keywords": [],
                                    "sensitivity_flags": [], "sources": []},
            "angle": {"angle_type": "explainer", "angle": "a", "key_message": "m", "rationale": "r"},
            "drafts": {"twitter": {"caption": "hi", "hashtags": []}},
            "review": {"recommendation": "PASS", "critique_summary": "", "sensitive": False,
                       "gate": {"twitter": {"verdict": "PASS", "limit_issues": [], "editorial_issues": []}}}}
    monkeypatch.setattr(app_module.orchestrator, "run", lambda *a, **k: fake)
    app_module.trends.service._board = {"states": {"RISING": [{"id": 42, "state": "RISING"}]}}
    rid = client.post("/api/generate", json={"topic": "topic", "platforms": ["twitter"],
                                             "trend_topic_id": 42, "trend_state": "EMERGING"}).get_json()["run_id"]
    assert app_module._RUNS[rid]["meta"]["trend_state"] == "RISING"
    rid = client.post("/api/generate", json={"topic": "topic", "platforms": ["twitter"],
                                             "trend_topic_id": 999}).get_json()["run_id"]
    assert app_module._RUNS[rid]["meta"]["trend_state"] is None


# --- creators, the 1-day / 2-day switch, feed retries --------------------------

def _video(conn, vid, title, channel, views_then, views_now, published_h, then_h=6):
    cur = conn.execute("INSERT INTO items (source, ext_id, title, url, publisher, published_at, first_seen, topic_id)"
                       " VALUES ('youtube',?,?,?,?,?,?,NULL)",
                       (vid, title, f"https://youtu.be/{vid}", channel, NOW - published_h * H, NOW - then_h * H))
    conn.execute("INSERT INTO observations VALUES (?,?,?)", (cur.lastrowid, NOW - then_h * H, views_then))
    conn.execute("INSERT INTO observations VALUES (?,?,?)", (cur.lastrowid, NOW, views_now))
    return cur.lastrowid, {"source": "youtube", "title": title, "published_at": NOW - published_h * H,
                           "extra_text": ""}


def test_creator_views_count_more_but_real_views_are_shown():
    cfg = {**CFG, "creator_weight": 2.0, "youtube": [{"id": "UCa", "name": "Abhishek Kar", "group": "creator"},
                                                    {"id": "UCb", "name": "CNBC-TV18", "group": "news"}]}
    service.collect(now=NOW, cfg=cfg, fetch=fetcher(news=_story(JIO, 9, 3, "a") + _filler(9, 9, "p")))
    with closing(store.connect()) as conn:
        recs = [_video(conn, "v1", "Jio IPO valuation explained simply", "Abhishek Kar", 0, 10_000, 30),
                _video(conn, "v2", "Trent Zudio expansion stores analysis", "CNBC-TV18", 0, 10_000, 30),
                _video(conn, "v3", "Trent Zudio expansion stores debate", "CNBC-TV18", 0, 10_000, 30)]
        topics.assign(conn, recs, NOW)
        conn.commit()
    board = service.compute(now=NOW, cfg=cfg)
    s = board_states(board)
    assert any("Creators covering it: Abhishek Kar" in sig for sig in s[JIO]["signals"])
    assert any("10,000 views" in sig for sig in s[JIO]["signals"])        # the real count, not the weighted one
    # same 10k views each: the creator's video is worth 2x a news channel's in the score
    rows = [{"id": 1, "source": "youtube", "topic_id": 1, "publisher": "Abhishek Kar", "published_at": NOW - 30 * H},
            {"id": 2, "source": "youtube", "topic_id": 2, "publisher": "CNBC-TV18", "published_at": NOW - 30 * H}]
    obs = {1: [(NOW - 6 * H, 0), (NOW, 10_000)], 2: [(NOW - 6 * H, 0), (NOW, 10_000)]}
    totals, per_topic = service._windows(rows, obs, NOW, 24, 4, service._yt_weights(cfg))
    assert per_topic[1][0]["youtube"] == 2 * per_topic[2][0]["youtube"] == 20_000
    assert per_topic[1][0]["youtube_raw"] == per_topic[2][0]["youtube_raw"] == 10_000
    assert totals[0]["youtube"] == 30_000 and "youtube_raw" not in totals[0]


def test_two_day_window_does_not_drop_a_topic_busy_in_the_previous_window():
    # Jio coverage only ~35h ago: with 2-day windows that is the CURRENT window -> shown.
    # (Other news keeps flowing in the last day, as it always does, so 1-day windows are
    # comparable and the board does not have to widen.)
    news = _story(JIO, 9, 35, "a") + _filler(9, 35, "p") + _filler(9, 70, "q") + _filler(9, 5, "r")
    service.collect(now=NOW, cfg=CFG, fetch=fetcher(news=news))
    two_day = service.compute(now=NOW, cfg=CFG, base_hours=48)
    assert two_day["window_hours"] == 48 and JIO in board_states(two_day)
    one_day = service.compute(now=NOW, cfg=CFG, base_hours=24)
    # with 1-day windows the same story went quiet a day ago: fading, not gone
    assert board_states(one_day)[JIO]["state"] == lifecycle.FADING


def test_update_holds_every_window_option_and_reads_back_either():
    cfg = {**CFG, "window_hours": 24, "window_options": [24, 48], "report_hours": 24}
    news = _story(JIO, 9, 3, "a") + _story(JIO, 9, 30, "b") + _filler(9, 3, "p") + _filler(9, 30, "q")
    service.collect(now=NOW, cfg=cfg, fetch=fetcher(news=news))
    u = service.make_update(now=NOW, cfg=cfg)
    assert set(u["views"]) == {"24", "48"} and u["window_hours"] == 24
    day, two = service.get_update(hours=24), service.get_update(hours=48)
    assert day["window_hours"] == 24 and two["window_hours"] == 48
    assert JIO in {t["label"] for t in day["topics"]} and JIO in {t["label"] for t in two["topics"]}
    assert day["windows"] == [24.0, 48.0]
    assert service.update_due(now=NOW + 23 * H, cfg=cfg)[0] is False       # daily now
    assert service.update_due(now=NOW + 24 * H, cfg=cfg)[0] is True


def test_old_single_window_updates_still_read():
    with closing(store.connect()) as conn:
        conn.execute("INSERT INTO updates (ts, window_hours, payload) VALUES (?,?,?)", (NOW, 9, json.dumps(
            {"ts": NOW, "window_hours": 9, "comparable": True, "dropped": [],
             "topics": [{"id": 1, "label": "Old", "state": "RISING", "velocity_pct": 50, "score": 0.1}]})))
        conn.commit()
    u = service.get_update(hours=24)
    assert u["topics"][0]["label"] == "Old" and u["topics"][0]["cls"] == "TRENDING" and u["window_hours"] == 9


def test_youtube_feed_is_retried_before_a_channel_counts_as_failed(monkeypatch):
    monkeypatch.setattr(service.time, "sleep", lambda s: None)
    calls = []
    xml = ('<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015"'
           ' xmlns:media="http://search.yahoo.com/mrss/"><entry><yt:videoId>abc</yt:videoId>'
           f'<title>Nifty view</title><published>{time_iso(NOW - H)}</published>'
           '<media:group><media:community><media:statistics views="500"/></media:community>'
           '</media:group></entry></feed>')

    def flaky(url):
        if "youtube" in url:
            calls.append(url)
            if len(calls) < 3:
                raise OSError("HTTP Error 404")              # what YouTube's feed really does
            return xml
        return fetcher()(url)
    cfg = {**CFG, "youtube": [{"id": "UCx", "name": "Groww", "group": "creator"}]}
    out = service.collect(now=NOW, cfg=cfg, fetch=flaky)
    assert out["youtube"]["ok"] is True and out["youtube"]["items"] == 1 and len(calls) == 3


def test_youtube_api_path_used_when_a_key_is_set(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "k")

    def api(url):
        if "playlistItems" in url:
            assert "playlistId=UUx" in url                    # uploads playlist of channel UCx
            return json.dumps({"items": [{"contentDetails": {"videoId": "v9"}}]})
        if "/videos?" in url:
            return json.dumps({"items": [{"id": "v9", "snippet": {"title": "Jio IPO review",
                                          "publishedAt": time_iso(NOW - H)},
                                          "statistics": {"viewCount": "12345"}}]})
        return fetcher()(url)
    cfg = {**CFG, "youtube": [{"id": "UCx", "name": "IPO Review by Groww", "group": "creator"}]}
    out = service.collect(now=NOW, cfg=cfg, fetch=api)
    assert out["youtube"]["items"] == 1
    with closing(store.connect()) as conn:
        r = conn.execute("SELECT publisher, title FROM items WHERE source='youtube'").fetchone()
        v = conn.execute("SELECT value FROM observations").fetchone()[0]
    assert (r["publisher"], r["title"], v) == ("IPO Review by Groww", "Jio IPO review", 12345)


def time_iso(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def test_trends_route_accepts_only_configured_windows(client):
    app_module.trends.service._board = None
    assert client.get("/api/trends?window=48").get_json()["window_hours"] in (48, 96)
    assert client.get("/api/trends?window=7").get_json()["window_hours"] in (24, 48)   # unknown -> default
    assert client.get("/api/trends?window=abc").status_code == 200


def test_news_channels_count_less_than_creators_in_score_and_evidence():
    cfg = {**CFG, "creator_weight": 2.0, "news_channel_weight": 0.5,
           "youtube": [{"id": "a", "name": "Abhishek Kar", "group": "creator"},
                       {"id": "b", "name": "CNBC Awaaz", "group": "news"},
                       {"id": "c", "name": "Zee Business", "group": "news"}]}
    w = service._yt_weights(cfg)
    assert (w["Abhishek Kar"], w["CNBC Awaaz"]) == (2.0, 0.5)               # creator = 4x a news channel
    rows = [{"id": 1, "source": "youtube", "topic_id": 1, "publisher": "Abhishek Kar", "published_at": NOW - 30 * H},
            {"id": 2, "source": "youtube", "topic_id": 2, "publisher": "CNBC Awaaz", "published_at": NOW - 30 * H}]
    obs = {1: [(NOW - 6 * H, 0), (NOW, 1000)], 2: [(NOW - 6 * H, 0), (NOW, 1000)]}
    _, per = service._windows(rows, obs, NOW, 24, 4, w)
    assert per[1][0]["youtube"] == 4 * per[2][0]["youtube"]
    # evidence: a topic carried only by two news-channel clips is not shown
    service.collect(now=NOW, cfg=cfg, fetch=fetcher(news=_filler(9, 3, "p") + _filler(9, 30, "q")))
    with closing(store.connect()) as conn:
        recs = [_video(conn, "n1", "Bank Nifty live trading session today", "CNBC Awaaz", 0, 5000, 10),
                _video(conn, "n2", "Bank Nifty live trading session analysis", "Zee Business", 0, 5000, 10)]
        topics.assign(conn, recs, NOW)
        conn.commit()
    labels = set(board_states(service.compute(now=NOW, cfg=cfg)))
    assert not any("Bank Nifty live trading" in label for label in labels)


# --- audit 2026-10-07: Hinglish, video kinds, hot creators, pulse, coverage -------

def test_hindi_and_hinglish_filler_never_count_as_shared_names():
    a = tokens("Market Crash Me Paisa Kaise Banaye? Kya Karna Hai")
    b = tokens("Rent Pe Kyun Rehna? Kaise Bachaye Paisa Hai")
    assert not ({"me", "kaise", "kya", "hai", "pe", "kyun"} & (a | b))
    assert not ({"में", "का", "के", "क्या", "है"} & tokens("Nifty में क्या है का के"))
    assert "nifty" in tokens("Nifty में क्या है")


def test_channel_tags_in_titles_are_noise():
    from engine.trends import text
    text.set_channel_noise(["Sanjay Kathuria", "Market Gabru", "The Bald Trader", "Groww"])
    toks = tokens("Why Do 90% F&O Traders Lose Money? #sanjaykathuria #marketgabru | N18G")
    assert not ({"sanjaykathuria", "marketgabru", "n18g"} & toks)
    # the separate words of a channel name are real subjects and must survive
    assert {"trader", "money"} <= toks and "groww" in tokens("Groww shares list at a premium")
    assert "market" in tokens("Market crash today")


@pytest.mark.parametrize("video,kind", [
    ({"contentDetails": {"duration": "PT45S"}}, "short"),
    ({"contentDetails": {"duration": "PT2M59S"}}, "short"),
    ({"contentDetails": {"duration": "PT12M3S"}}, "long"),
    ({"contentDetails": {"duration": "PT1H2M"}}, "long"),
    ({"contentDetails": {"duration": "P0D"}, "liveStreamingDetails": {"actualStartTime": "x"}}, "live"),
    ({"snippet": {"liveBroadcastContent": "upcoming"}}, "live"),
    ({}, "long"),
])
def test_video_kind(video, kind):
    assert sources.video_kind(video) == kind


def test_pace_compares_like_with_like():
    """A channel's live stream at 50,000/h must not make its normal clips look dead (or vice versa)."""
    def row(i, kind):
        return {"id": i, "source": "youtube", "publisher": "Zee Business", "published_at": NOW - 10 * H, "kind": kind}
    items = [row(i, "long") for i in range(4)] + [row(10 + i, "live") for i in range(3)]
    obs = {i: [(NOW, 1000)] for i in range(4)}                       # long videos: 100/h
    obs.update({10 + i: [(NOW, 500_000)] for i in range(3)})          # live streams: 50,000/h
    pace = service.channel_pace(items, obs)
    assert pace[("Zee Business", "long")] == 100 and pace[("Zee Business", "live")] == 50_000
    obs[99] = [(NOW, 3000)]
    assert service.outlier(row(99, "long"), obs, pace) == 3.0       # 300/h vs its kind's 100/h


def test_a_hot_creator_video_plus_news_puts_a_topic_on_the_board():
    cfg = {**CFG, "creator_weight": 2.0, "news_channel_weight": 0.5,
           "youtube": [{"id": "a", "name": "Groww", "group": "creator"}]}
    # one news article only -> normally not enough evidence
    news = [("Colgate shares slump on weak volume growth", "Mint", 3)] + _filler(9, 3, "p") + _filler(9, 30, "q")
    service.collect(now=NOW, cfg=cfg, fetch=fetcher(news=news))
    with closing(store.connect()) as conn:
        recs = [_video(conn, f"g{i}", f"Groww explainer number{i} on savings", "Groww", 0, 1000, 10) for i in range(3)]
        recs.append(_video(conn, "hot", "Colgate shares slump explained", "Groww", 0, 5000, 10))  # 5x its usual
        topics.assign(conn, recs, NOW)
        conn.commit()
    board = service.compute(now=NOW, cfg=cfg)
    s = board_states(board)
    assert any("Colgate" in label for label in s)
    pulse = board["creator_pulse"]
    assert pulse[0]["title"] == "Colgate shares slump explained" and pulse[0]["outlier"] == 5.0
    assert pulse[0]["topic"] and pulse[0]["topic"]["label"].startswith("Colgate")


def test_board_reports_how_much_history_backs_the_comparison():
    service.collect(now=NOW, cfg=CFG, fetch=fetcher(news=_filler(9, 3, "p")))
    b = service.compute(now=NOW + 12 * H, cfg=CFG, window=(24, 4))
    assert b["history_hours"] == 12.0 and b["coverage"] == 0.25                # 12h of the 48h compared


def test_items_table_gains_kind_column_on_an_old_database(tmp_path, monkeypatch):
    import sqlite3
    old = tmp_path / "old.db"
    con = sqlite3.connect(old)
    con.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, source TEXT NOT NULL, ext_id TEXT NOT NULL, title TEXT NOT NULL,"
                " url TEXT, publisher TEXT, published_at REAL, first_seen REAL NOT NULL, topic_id INTEGER, UNIQUE (source, ext_id))")
    con.commit()
    con.close()
    monkeypatch.setattr(store, "DB_PATH", str(old))
    with closing(store.connect()) as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(items)")}
        item_id, _ = store.upsert_item(conn, {"source": "youtube", "ext_id": "v", "title": "t", "kind": "live"}, NOW)
        store.upsert_item(conn, {"source": "youtube", "ext_id": "v", "title": "t", "kind": "long"}, NOW)   # stream ended
        assert "kind" in cols
        assert conn.execute("SELECT kind FROM items WHERE id=?", (item_id,)).fetchone()[0] == "long"
