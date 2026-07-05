"""Publishers — request shape + the safe-by-default publish paths."""

import pytest

from engine.publishers import BY_KEY
from engine.publishers.base import _post_text

MANIFEST = {"drafts": {
    "twitter": {"caption": "Hello world", "hashtags": ["news", "India"]},
    "instagram": {"caption": "IG post", "hashtags": ["a"], "alt_text": "card"},
    "linkedin": {"caption": "story", "hashtags": ["x"]},
}, "image_path": "output/x.png"}


def test_post_text_joins_caption_and_hashtags():
    t = _post_text({"caption": "hi", "hashtags": ["a", "b"]})
    assert t == "hi\n\n#a #b"


def test_twitter_request_shape():
    req = BY_KEY["twitter"].build_request(MANIFEST)
    assert req["url"] == "https://api.twitter.com/2/tweets"
    assert "Hello world" in req["body"]["text"]


def test_instagram_two_step_and_public_url_note():
    req = BY_KEY["instagram"].build_request(MANIFEST)
    assert len(req["steps"]) == 2
    assert any("PUBLIC" in n for n in req["notes"])


def test_publish_dry_run_returns_request():
    r = BY_KEY["twitter"].publish(MANIFEST, live=False)
    assert r["status"] == "dry-run" and "request" in r


def test_publish_live_without_creds_skips(monkeypatch):
    for var in BY_KEY["twitter"].required_env:
        monkeypatch.delenv(var, raising=False)
    r = BY_KEY["twitter"].publish(MANIFEST, live=True)
    assert r["status"] == "skipped"
    assert set(r["missing"]) == set(BY_KEY["twitter"].required_env)


def test_send_is_unwired(monkeypatch):
    # even with creds present, live send must not silently "succeed"
    for var in BY_KEY["twitter"].required_env:
        monkeypatch.setenv(var, "x")
    r = BY_KEY["twitter"].publish(MANIFEST, live=True)
    assert r["status"] == "error"  # _send raises NotImplementedError -> caught


def test_missing_draft_skips():
    r = BY_KEY["twitter"].publish({"drafts": {}}, live=False)
    assert r["status"] == "skipped"
