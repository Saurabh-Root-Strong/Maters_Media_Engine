"""Platform-algorithm awareness — playbook, prompt injection, the fit check."""

import pytest

import app as app_module
from engine import algo, angle, drafters


def checks(platform, caption, hashtags=(), sources=None):
    f = algo.fit(platform, {"caption": caption, "hashtags": list(hashtags)}, sources)
    return f, {c["id"]: c for c in f["checks"]}


GOOD_TWEET = ("🚨 Crude up 5% in a day — bad for Indian stocks? Aviation and paints face margin "
              "pressure, while ONGC and Oil India gain. Is this a spike or a new range?")


def test_well_shaped_tweet_scores_full_marks():
    f, c = checks("twitter", GOOD_TWEET, ["CrudeOil"])
    assert f["score"] == 100 and all(x["ok"] for x in c.values())


def test_link_in_post_is_the_heaviest_twitter_failure():
    f, c = checks("twitter", GOOD_TWEET + " https://example.com/story", ["CrudeOil"])
    assert c["no_link"]["ok"] is False and f["score"] == 75
    assert f["checks"][0]["id"] == "no_link"          # failures listed first, heaviest first
    _, c = checks("twitter", "Read more at moneycontrol.com for the full story on crude oil prices today?")
    assert c["no_link"]["ok"] is False                # bare domain counts too


def test_twitter_statement_with_no_question_and_hashtag_stack():
    _, c = checks("twitter", "Crude oil is trending today.", ["a", "b", "c"])
    assert c["asks_question"]["ok"] is False
    assert c["max_hashtags"]["ok"] is False and c["min_chars"]["ok"] is False


@pytest.mark.parametrize("text", [
    "Like if you agree with this take on the RBI.", "Comment YES if you want part 2",
    "Tag a friend who needs to see this", "Follow for more market updates",
    "RT if you think Nifty hits 25k", "Double tap if you hold HDFC", "Like and share this post"])
def test_engagement_bait_is_caught(text):
    _, c = checks("instagram", text)
    assert c["no_bait"]["ok"] is False


@pytest.mark.parametrize("text", [
    "Save tax if you invest in ELSS before March.", "Should you follow the rally if rates rise?",
    "Shares fell 4% — like-for-like sales were weak.", "Investors tag this a turnaround story."])
def test_ordinary_finance_wording_is_not_flagged_as_bait(text):
    _, c = checks("instagram", text)
    assert c["no_bait"]["ok"] is True


def test_instagram_hashtag_cap_and_mention_spam():
    _, c = checks("instagram", "🚨 Markets closed Friday!\n\n@nseindia @bseindia @moneycontrolcom @etmarkets",
                  ["a", "b", "c", "d", "e", "f"])
    assert c["max_hashtags"]["ok"] is False and "6" in c["max_hashtags"]["measured"]
    assert c["max_mentions"]["ok"] is False and c["max_mentions"]["basis"] == "heuristic"
    assert c["max_hashtags"]["basis"] == "official"


def test_linkedin_hook_depth_and_question_position():
    body = "\n\n".join(["A short factual line about the story."] * 30)
    f, c = checks("linkedin", "🚨 Jio IPO: the valuation nobody is questioning\n\n" + body
                  + "\n\nWhat would make you pay 11 lakh crore?", ["Jio", "IPO", "Markets"])
    assert f["score"] == 100
    # a question buried mid-post does not count — it must be where the reader finishes
    _, c = checks("linkedin", "Is this fair?\n\n" + body + "\n\nThat is the story.")
    assert c["asks_question"]["ok"] is False
    _, c = checks("linkedin", "x" * 300 + "\n\nshort")
    assert c["hook_chars"]["ok"] is False and c["depth_chars"]["ok"] is False and c["line_breaks"]["ok"] is False


def test_trailing_mentions_do_not_fake_a_question():
    _, c = checks("twitter", "Statement only.\n\n@nseindia @bseindia")
    assert c["asks_question"]["ok"] is False
    _, c = checks("twitter", "Is this a spike or a new range?\n\n@nseindia @bseindia")
    assert c["asks_question"]["ok"] is True


def test_first_comment_carries_the_link_where_the_platform_penalises_it():
    src = ["not a url", "https://economictimes.com/x"]
    assert checks("twitter", GOOD_TWEET, sources=src)[0]["first_comment"] == "Source: https://economictimes.com/x"
    assert checks("linkedin", GOOD_TWEET, sources=src)[0]["first_comment"].startswith("Source: https://")
    assert checks("instagram", GOOD_TWEET, sources=src)[0]["first_comment"] is None
    assert checks("twitter", GOOD_TWEET, sources=[])[0]["first_comment"] is None


def test_unknown_platform_has_no_fit():
    assert algo.fit("mastodon", {"caption": "x"}) is None


def test_every_check_in_the_playbook_is_implemented_and_tagged():
    for platform in ("twitter", "linkedin", "instagram"):
        p = algo.playbook()[platform]
        assert p["write_rules"] and p["optimises_for"]
        for name, rule in p["checks"].items():
            assert name in algo.LABELS, name
            assert rule["basis"] in ("official", "code", "reported", "heuristic") and rule["why"]
            assert rule["weight"] > 0


def test_ranking_rules_reach_the_drafting_prompt():
    specs = drafters._load_platforms()
    for key in ("twitter", "linkedin", "instagram"):
        prompt = drafters._system_for(specs[key], None, key)
        assert "WRITE FOR THE RANKING ALGORITHM" in prompt
        assert "FORMAT" in prompt                      # the user's own template is still there
    assert "reply underneath" in drafters._system_for(specs["twitter"], None, "twitter")
    assert "first comment" in drafters._system_for(specs["linkedin"], None, "linkedin")
    # without a platform key (legacy callers) the prompt is unchanged
    assert "RANKING ALGORITHM" not in drafters._system_for(specs["twitter"])


def test_hashtag_limits_follow_the_platforms():
    specs = drafters._load_platforms()
    assert specs["twitter"]["hashtags_max"] == 2
    assert (specs["linkedin"]["hashtags_min"], specs["linkedin"]["hashtags_max"]) == (3, 5)
    assert specs["instagram"]["hashtags_max"] == 5      # Instagram's enforced cap


def test_trend_stage_changes_the_angle_prompt(monkeypatch):
    seen = []
    monkeypatch.setattr(angle.llm, "structured", lambda system, user, schema: seen.append(system) or {})
    angle.choose({"facts": []}, "PEAKING")
    angle.choose({"facts": []}, None)
    angle.choose({"facts": []}, "NOT_A_STATE")
    assert "TREND STAGE — PEAKING" in seen[0] and "contrarian" in seen[0]
    assert "TREND STAGE" not in seen[1] and "TREND STAGE" not in seen[2]


def test_fit_is_advisory_and_never_changes_the_gate():
    drafts = {"twitter": {"caption": "LIKE IF YOU AGREE https://spam.example", "hashtags": []}}
    rev = {"sensitive": False, "gate": {"twitter": {"verdict": "PASS"}}}
    p = app_module._postability(rev, drafts, ["https://src.example/a"])["twitter"]
    assert p["state"] == "ready"                        # the review gate alone decides posting
    assert p["fit"]["score"] < 60 and p["fit"]["first_comment"] == "Source: https://src.example/a"
