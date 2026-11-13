"""Following the creator's own posted videos — offline, with a fake YouTube API."""
import json

import pytest

from engine import content, media, tracking

NOW = 1_800_000_000.0


def yt(views, published=NOW - 2 * 86400, kind_secs="PT20S", private=False):
    item = {"id": "abcdefghijk", "snippet": {"title": "Stock market ke rang", "channelTitle": "The Market Tales",
                                            "channelId": "UCx", "publishedAt": "2027-01-10T10:00:00Z"},
            "statistics": {"viewCount": str(views), "likeCount": "40", "commentCount": "5"},
            "contentDetails": {"duration": kind_secs}, "status": {"privacyStatus": "public"}}
    return {"items": [] if private else [item]}


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "k")


def fake(views, usual=None):
    def fetch(url):
        return json.dumps(yt(views))
    return fetch


def test_only_youtube_can_be_followed(key):
    with pytest.raises(media.MediaError, match="Only YouTube"):
        tracking.track("https://www.instagram.com/reel/abc/")


def test_a_private_or_missing_video_is_refused_with_a_reason(key):
    with pytest.raises(media.MediaError):
        tracking.track("https://youtube.com/shorts/abcdefghijk", fetch=lambda u: json.dumps({"items": []}))


def test_follow_links_the_package_and_reads_numbers(key, monkeypatch):
    monkeypatch.setattr(media, "performance", lambda *a, **k: {"channel_usual": 10.0})
    pid = content._save("youtube_shorts", "stock market emotions", None, {"titles": [{"text": "Rang 📊"}]}, media_id=None)
    v = tracking.track("https://youtube.com/shorts/abcdefghijk", pid, fetch=fake(960), now=NOW)
    assert v["views"] == 960 and v["package"]["topic"] == "stock market emotions"
    assert v["channel_usual"] == 10.0 and v["vs_usual"] is not None


def test_refresh_rereads_after_hours_and_keeps_the_baseline(key, monkeypatch):
    monkeypatch.setattr(media, "performance", lambda *a, **k: {"channel_usual": 10.0})
    tracking.track("https://youtube.com/shorts/abcdefghijk", fetch=fake(500), now=NOW)
    assert tracking.refresh(fetch=fake(700), now=NOW + 3600) == 0                 # too soon
    monkeypatch.setattr(media, "performance", lambda *a, **k: None)             # baseline not refreshed today
    assert tracking.refresh(fetch=fake(900), now=NOW + 7 * 3600) == 1
    v = tracking.followed(NOW + 7 * 3600)[0]
    assert v["views"] == 900 and v["gained"] == 400 and v["channel_usual"] == 10.0 and v["reads"] == 2


def test_lessons_need_enough_videos(key, monkeypatch):
    monkeypatch.setattr(media, "performance", lambda *a, **k: {"channel_usual": 10.0})
    tracking.track("https://youtube.com/shorts/abcdefghijk", fetch=fake(2000), now=NOW)
    L = tracking.lessons(NOW)
    assert L["measured"] == 1 and L["enough"] is False
    assert L["groups"]["Format"][0]["key"] == "Shorts" and L["groups"]["Format"][0]["few"] is True


def test_routes(key, monkeypatch):
    import app as app_module
    monkeypatch.setattr(media, "performance", lambda *a, **k: None)
    monkeypatch.setattr(media, "youtube_facts", lambda link, fetch=None: {
        "numbers": True, "views": 10, "likes": 1, "comments": 0, "title": "T", "published_at": NOW, "kind": "short"})
    c = app_module.app.test_client()
    assert c.post("/api/track", json={}).status_code == 400
    assert c.post("/api/track", json={"url": "https://youtube.com/shorts/abcdefghijk"}).get_json()["title"] == "T"
    assert c.get("/api/track").get_json()["videos"][0]["video_id"] == "abcdefghijk"
