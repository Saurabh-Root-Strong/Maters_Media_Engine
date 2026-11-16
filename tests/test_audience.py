"""Audience language, the text inside the video, posting gaps, and following
your own uploads without pasting links — offline, fake YouTube API."""
import json
from contextlib import closing

import pytest

from engine import audience, content, media, memory, tracking

NOW = 1_800_000_000.0
CH = "UCabcdefghijklmnopqrstuv"
LANGS = {"default": "hinglish", "linkedin": "english"}


@pytest.fixture
def brand(monkeypatch):
    monkeypatch.setattr(memory, "brand_profile", lambda: {"name": "T", "languages": dict(LANGS)})


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "k")
    monkeypatch.setenv("MEDIA_ENGINE_YT_CHANNEL", CH)
    audience._CACHE.clear()


def iso(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- what the text says ----------------------------------------------------------

@pytest.mark.parametrize("lines,want", [
    (["Your anger will disappear once you get money.", "Bro, get money."], "english"),
    (["Be patient in the stock market. Years of losses can be recovered with 3-4 great trades."], "english"),
    (["Trader ko aur kya chahiye? 📱 Mobile, internet aur sukoon."], "hinglish"),
    (["Stock market bhi bilkul in lights jaisa hai – har din alag colour"], "hinglish"),
    (["ब्रो, पैसा कमाओ"], "hindi"),
    # English words that look Hindi in Roman letters must not flip it
    (["The main log file is open in the next tab, so jab the mat"], "english"),
    # one Hindi word in an English line is still English to the reader
    (["Bro, paisa is everything for me today"], "english"),
    (["Bro"], None), (["📈💰🔥"], None), ([], None), (None, None), ([None, 5], None),
])
def test_text_language(lines, want):
    assert audience.text_language(lines) == want


# --- what the audience reads ---------------------------------------------------------

def test_not_configured_means_old_behaviour(monkeypatch):
    monkeypatch.setattr(memory, "brand_profile", lambda: {})
    assert audience.default_language("youtube_shorts") == "english"
    assert audience.notes("youtube_shorts", "english", {"on_screen_text": ["Bhai kya hai yeh sab"]}) == {"video": [], "copy": []}


def test_per_platform_default(brand):
    assert audience.default_language("youtube_shorts") == "hinglish"
    assert audience.default_language("linkedin") == "english"
    assert audience.default_language(None) == "hinglish"


def test_junk_config_falls_back(monkeypatch):
    monkeypatch.setattr(memory, "brand_profile", lambda: {"languages": {"default": "klingon"}})
    assert not audience.configured() and audience.default_language("youtube_shorts") == "english"
    monkeypatch.setattr(memory, "brand_profile", lambda: {"languages": "hinglish"})
    assert not audience.configured()


def test_english_copy_for_a_hinglish_audience_is_flagged(brand):
    n = audience.notes("youtube_shorts", "english", {})
    assert n["copy"] and "Hinglish" in n["copy"][0]
    assert audience.notes("youtube_shorts", "hinglish", {})["copy"] == []
    assert audience.notes("linkedin", "english", {})["copy"] == []


def test_english_text_in_the_video_is_a_fix_before_posting(brand):
    seen = {"on_screen_text": ["Be patient in the stock market. Years of losses can be recovered."]}
    n = audience.notes("youtube_shorts", "hinglish", seen)
    assert n["video"] and "Re-edit" in n["video"][0]
    assert audience.notes("youtube_shorts", "hinglish", seen, owner="theirs")["video"] == []   # not your video
    assert audience.notes("linkedin", "english", seen)["video"] == []                           # matches


def test_hindi_text_suits_a_hinglish_audience(brand):
    assert audience.notes("youtube_shorts", "hinglish", {"on_screen_text": ["ब्रो, पैसा कमाओ"]})["video"] == []


# --- posting gap -------------------------------------------------------------------------

def uploads(*videos):
    """videos: (id, published_ts, seconds, views)."""
    def fetch(url):
        if "playlistItems" in url:
            return json.dumps({"items": [{"contentDetails": {"videoId": v[0]}} for v in videos]})
        return json.dumps({"items": [{"id": v[0], "snippet": {"title": f"Video {v[0]}", "publishedAt": iso(v[1])},
                                      "statistics": {"viewCount": str(v[3])},
                                      "contentDetails": {"duration": f"PT{v[2]}S"}} for v in videos]})
    return fetch


def test_a_short_under_a_day_old_gets_a_wait_note(key):
    out = audience.posting_gap("youtube_shorts", uploads(("aaaaaaaaaaa", NOW - 5 * 3600, 11, 1158)), NOW)
    assert len(out) == 1 and "5 h ago" in out[0] and "1,158" in out[0] and "after" in out[0]


def test_a_day_old_short_is_no_reason_to_wait(key):
    assert audience.posting_gap("youtube_shorts", uploads(("aaaaaaaaaaa", NOW - 25 * 3600, 11, 900)), NOW) == []


def test_only_shorts_compete_with_shorts(key):
    fetch = uploads(("longlonglon", NOW - 3600, 600, 50), ("aaaaaaaaaaa", NOW - 30 * 3600, 11, 900))
    assert audience.posting_gap("youtube_shorts", fetch, NOW) == []


@pytest.mark.parametrize("platform", ["instagram_reel", "youtube", "linkedin"])
def test_other_platforms_get_no_gap_note(key, platform):
    assert audience.posting_gap(platform, uploads(("aaaaaaaaaaa", NOW - 3600, 11, 5)), NOW) == []


def test_no_key_no_channel_or_a_failing_api_is_silent(key, monkeypatch):
    def boom(url):
        raise OSError("network down")
    assert audience.posting_gap("youtube_shorts", boom, NOW) == []
    monkeypatch.delenv("YOUTUBE_API_KEY")
    assert audience.posting_gap("youtube_shorts", uploads(("aaaaaaaaaaa", NOW - 3600, 11, 5)), NOW) == []
    monkeypatch.setenv("YOUTUBE_API_KEY", "k")
    monkeypatch.setattr(tracking, "own_channel", lambda: None)
    assert audience.posting_gap("youtube_shorts", uploads(("aaaaaaaaaaa", NOW - 3600, 11, 5)), NOW) == []


def test_a_short_that_stopped_growing_is_fine_to_follow(key):
    vid = "plateau0001"
    with closing(tracking._db()) as conn:
        conn.executemany("INSERT INTO tracked_stats (video_id, ts, views) VALUES (?,?,?)",
                         [(vid, NOW - 8 * 3600, 1190), (vid, NOW - 2 * 3600, 1193)])
        conn.commit()
    out = audience.posting_gap("youtube_shorts", uploads((vid, NOW - 20 * 3600, 11, 1193)), NOW)
    assert out and "stopped" in out[0] and "after" not in out[0]


def test_a_short_still_gaining_says_so(key):
    vid = "growing0001"
    with closing(tracking._db()) as conn:
        conn.executemany("INSERT INTO tracked_stats (video_id, ts, views) VALUES (?,?,?)",
                         [(vid, NOW - 8 * 3600, 200), (vid, NOW - 2 * 3600, 1158)])
        conn.commit()
    out = audience.posting_gap("youtube_shorts", uploads((vid, NOW - 9 * 3600, 11, 1158)), NOW)
    assert out and "still gaining (+958" in out[0]


# --- following your own uploads ----------------------------------------------------------

def test_package_match_survives_hashtags_emoji_and_case():
    pid = content._save("youtube_shorts", "money motivation", None,
                        {"titles": [{"text": "Bro, Get Money 💰"}], "description": "Anger. Depression. Bro, get money."})
    created = content._db().execute("SELECT created_at FROM content_packages WHERE id=?", (pid,)).fetchone()[0]
    assert tracking.match_package("#StockMarket Bro, get money 💸#shorts", "", created + 3600) == pid


def test_package_match_picks_the_one_whose_description_was_pasted():
    a = content._save("youtube_shorts", "m", None, {"titles": [{"text": "Bro, Get Money"}],
                                                   "description": "Anger, depression, tiredness — the reel says it all."})
    b = content._save("youtube_shorts", "m", None, {"titles": [{"text": "Bro, Get Money"}],
                                                   "description": "Pure hustle line for late nights."})
    created = content._db().execute("SELECT MAX(created_at) FROM content_packages").fetchone()[0]
    assert tracking.match_package("Bro, get money", "Anger, depression, tiredness — the reel says it all.\n\n#Paisa",
                                  created + 60) == a
    assert b != a


def test_package_match_refuses_strangers_and_later_packages():
    pid = content._save("youtube_shorts", "x", None, {"titles": [{"text": "Patience In Trading Pays Off"}]})
    created = content._db().execute("SELECT created_at FROM content_packages WHERE id=?", (pid,)).fetchone()[0]
    assert tracking.match_package("Nifty crash explained", "", created + 60) is None
    assert tracking.match_package("Patience In Trading Pays Off", "", created - 86400) is None   # written after
    assert tracking.match_package("Patience In Trading Pays Off", "", created + 8 * 86400) is None  # too old
    assert tracking.match_package("Patience In Trading Pays Off", "", None) is None


def channel_api(videos, calls):
    """videos: (id, title, published_ts)."""
    def fetch(url):
        calls.append(url)
        if "playlistItems" in url:
            return json.dumps({"items": [{"snippet": {"resourceId": {"videoId": v[0]}, "title": v[1],
                                                      "description": "", "publishedAt": iso(v[2])}} for v in videos]})
        vid = url.split("id=")[1].split("&")[0]
        v = next(x for x in videos if x[0] == vid)
        return json.dumps({"items": [{"id": v[0], "snippet": {"title": v[1], "channelTitle": "T", "channelId": CH,
                                                              "publishedAt": iso(v[2])},
                                      "statistics": {"viewCount": "6", "likeCount": "1", "commentCount": "0"},
                                      "contentDetails": {"duration": "PT6S"}, "status": {"privacyStatus": "public"}}]})
    return fetch


def test_discover_follows_new_uploads_once_and_respects_hand_picks(key, monkeypatch):
    monkeypatch.setattr(media, "performance", lambda *a, **k: None)
    pid = content._save("youtube_shorts", "money", None, {"titles": [{"text": "Bro, Get Money"}]})
    created = content._db().execute("SELECT created_at FROM content_packages WHERE id=?", (pid,)).fetchone()[0]
    vids = [("newvideo001", "#StockMarket Bro, get money 💸#shorts", created + 3600),
            ("oldvideo001", "Ancient", created - 90 * 86400)]
    calls = []
    now = created + 2 * 3600
    assert tracking.discover(channel_api(vids, calls), now) == 1           # the 90-day-old one is skipped
    row = next(v for v in tracking.followed(now) if v["video_id"] == "newvideo001")
    assert row["package_id"] == pid
    calls.clear()
    assert tracking.discover(channel_api(vids, calls), now + 3600) == 0 and calls == []   # throttled: no quota
    assert tracking.discover(channel_api(vids, calls), now + 7 * 3600) == 0               # already followed
    # a hand-picked package is never replaced by the guess
    tracking.track("https://youtube.com/shorts/newvideo001", None, channel_api(vids, []), now)
    tracking.discover(channel_api(vids, []), now + 20 * 3600, force=True)
    assert next(v for v in tracking.followed(now) if v["video_id"] == "newvideo001")["package_id"] is None


def test_discover_without_a_channel_or_key_does_nothing(monkeypatch):
    monkeypatch.delenv("MEDIA_ENGINE_YT_CHANNEL", raising=False)
    monkeypatch.setenv("YOUTUBE_API_KEY", "k")
    monkeypatch.setattr(tracking, "own_channel", lambda: None)
    assert tracking.discover(lambda u: (_ for _ in ()).throw(AssertionError("no call")), NOW) == 0


def test_discover_survives_a_broken_api(key):
    def boom(url):
        raise OSError("down")
    assert tracking.discover(boom, NOW, force=True) == 0


def test_bad_channel_env_is_ignored(monkeypatch):
    monkeypatch.setenv("MEDIA_ENGINE_YT_CHANNEL", "not-a-channel")
    assert tracking.own_channel() != "not-a-channel"


# --- the route -------------------------------------------------------------------------------

@pytest.fixture
def client(brand, monkeypatch):
    import app as webapp
    from engine import llm
    monkeypatch.setattr(llm, "has_api_key", lambda: True)
    monkeypatch.setattr(content, "generate", lambda platform, topic, notes, language, board, **k:
                        {"language": language, "platform": platform})
    return webapp.app.test_client()


@pytest.mark.parametrize("sent,platform,want,auto", [
    ("auto", "youtube_shorts", "hinglish", True),
    (None, "youtube_shorts", "hinglish", True),
    ("auto", "linkedin", "english", True),
    ("klingon", "youtube_shorts", "hinglish", True),
    ("english", "youtube_shorts", "english", False),
])
def test_route_resolves_the_audience_language(client, sent, platform, want, auto):
    body = {"platform": platform, "topic": "stock market patience", **({"language": sent} if sent else {})}
    d = client.post("/api/content", json=body).get_json()
    assert d["language"] == want and d["language_auto"] is auto
    assert bool(d["copy_notes"]) is (platform == "youtube_shorts" and want == "english")


def test_formats_tell_the_page_each_platforms_language(client):
    d = client.get("/api/content/formats").get_json()
    assert d["audience_set"] and d["language_defaults"]["youtube_shorts"] == "hinglish"
    assert d["language_defaults"]["linkedin"] == "english"


def test_a_short_the_feed_never_picked_up_is_no_reason_to_wait(key):
    out = audience.posting_gap("youtube_shorts", uploads(("deadshort01", NOW - 21 * 3600, 16, 2)), NOW)
    assert out and "not picked up" in out[0] and "after" not in out[0]
    # ...but a brand-new one with few views is still being tested
    out = audience.posting_gap("youtube_shorts", uploads(("newshort001", NOW - 2 * 3600, 16, 2)), NOW)
    assert out and "after" in out[0]


def test_a_small_channels_usual_pace_is_not_rounded_to_zero(monkeypatch):
    from engine.trends import sources
    monkeypatch.setenv("YOUTUBE_API_KEY", "k")
    old = [{"ext_id": f"v{i}", "value": 13.0, "published_at": NOW - 200 * 86400, "kind": "short"} for i in range(5)]
    monkeypatch.setattr(sources, "fetch_youtube_api", lambda *a, **k: old)
    p = media.performance({"channel_id": CH, "views": 1158, "published_at": NOW - 6 * 3600, "kind": "short"},
                          "x", lambda u: "{}", NOW)
    assert p["channel_usual"] > 0 and p["vs_usual"] and p["vs_usual"] > 1


def test_old_shorts_are_compared_over_the_same_window(monkeypatch):
    """A new Short with as many views as the old ones is 'usual', not 1,000x better."""
    from engine.trends import sources
    monkeypatch.setenv("YOUTUBE_API_KEY", "k")
    old = [{"ext_id": f"v{i}", "value": 1200.0, "published_at": NOW - 200 * 86400, "kind": "short"} for i in range(5)]
    monkeypatch.setattr(sources, "fetch_youtube_api", lambda *a, **k: old)
    p = media.performance({"channel_id": CH, "views": 1200, "published_at": NOW - 4 * 86400, "kind": "short"},
                          "x", lambda u: "{}", NOW)
    assert p["vs_usual"] == 1.0
