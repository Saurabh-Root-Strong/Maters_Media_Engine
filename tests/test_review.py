"""Review gate — deterministic limits + verdict fusion."""

from engine import review
from engine.review import _worst, hard_violations, validate


def test_worst_ordering():
    assert _worst("PASS", "HOLD", "REVISE") == "HOLD"
    assert _worst("PASS", "REVISE") == "REVISE"
    assert _worst("PASS", "PASS") == "PASS"


def test_validate_catches_char_and_hashtag_limits():
    drafts = {
        "twitter": {"caption": "x" * 300, "hashtags": ["a", "b", "c"]},
        "instagram": {"caption": "ok", "hashtags": ["a", "b", "c", "d"]},
    }
    v = validate(drafts)
    msgs = [i["msg"] for i in v["twitter"]]
    assert any("300" in m and "280" in m for m in msgs)      # over char limit
    assert any("outside [0,2]" in m for m in msgs)           # too many hashtags
    hv = hard_violations(v)
    assert "twitter" in hv          # 4 hashtags is valid for instagram (3-5), so no IG hard-fail
    assert "instagram" not in hv


def test_validate_empty_caption_is_hard():
    v = validate({"twitter": {"caption": "  ", "hashtags": []}})
    assert any(i["level"] == "hard" for i in v["twitter"])


def test_linkedin_short_caption_is_soft_only():
    v = validate({"linkedin": {"caption": "too short", "hashtags": ["a", "b", "c", "d", "e"]}})
    levels = {i["level"] for i in v["linkedin"]}
    assert "soft" in levels and "hard" not in levels
    assert hard_violations(v) == {}


def test_review_fuses_and_forces_hold_on_sensitive(monkeypatch):
    # clean drafts, but the brief is sensitive -> everything must HOLD
    monkeypatch.setattr(review, "critique", lambda b, a, d: {
        "overall": {"recommendation": "PASS", "summary": "fine"},
        "platforms": [{"platform": "twitter", "verdict": "PASS", "issues": []}],
    })
    drafts = {"twitter": {"caption": "hi there", "hashtags": ["a"]}}
    out = review.review({"sensitivity_flags": ["war"]}, {}, drafts)
    assert out["sensitive"] is True
    assert out["recommendation"] == "HOLD"
    assert out["requires_human"] is True
    assert out["gate"]["twitter"]["verdict"] == "HOLD"


def test_review_revise_from_hard_limit(monkeypatch):
    monkeypatch.setattr(review, "critique", lambda b, a, d: {
        "overall": {"recommendation": "PASS", "summary": "fine"},
        "platforms": [{"platform": "twitter", "verdict": "PASS", "issues": []}],
    })
    drafts = {"twitter": {"caption": "x" * 300, "hashtags": []}}  # over limit
    out = review.review({"sensitivity_flags": []}, {}, drafts)
    assert out["gate"]["twitter"]["verdict"] == "REVISE"
    assert out["requires_human"] is False
