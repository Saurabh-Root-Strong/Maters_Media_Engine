"""Content generator — topic insight, strategy read, limit enforcement, routes."""

import json
from contextlib import closing

import pytest

import app as app_module
from engine import content
from engine.trends import insight, service, store
from tests.test_trends import CFG, H, NOW, _filler, _story, fetcher

JIO = "Jio Platforms IPO valuation soars"
PLAN = {"audience_intent": "retail investors asking who qualifies", "angle": "eligibility, not valuation",
        "promise": "know if you qualify", "primary_keyword": "jio ipo shareholder quota",
        "secondary_keywords": ["jio ipo price band"], "specific_hashtags": ["JioIPO"], "avoid": "valuation hype"}
insight._real = insight.analyse      # kept so a test can re-point analyse() at the fixed test clock


def _seed():
    news = _story(JIO, 9, 3, "a") + _story(JIO, 4, 9, "b") + _filler(9, 9, "p") + _filler(4, 3, "r")
    service.collect(now=NOW, cfg=CFG, fetch=fetcher(news=news))
    with closing(store.connect()) as conn:           # two competing videos with view counts
        for i, (title, views, age_h) in enumerate((("Jio IPO: apply or avoid?", 90000, 10),
                                                   ("Jio IPO GMP today", 5000, 5))):
            cur = conn.execute(
                "INSERT INTO items (source, ext_id, title, url, publisher, published_at, first_seen)"
                " VALUES ('youtube',?,?,?,?,?,?)",
                (f"v{i}", title, f"https://youtu.be/v{i}", f"Chan{i}", NOW - age_h * H, NOW))
            conn.execute("INSERT INTO observations VALUES (?,?,?)", (cur.lastrowid, NOW, views))
        conn.commit()
    return service.compute(now=NOW, cfg=CFG, write=True)


def suggest_fetch(url):
    assert "suggestqueries" in url
    return json.dumps(["jio ipo", ["jio ipo", "jio ipo price band", "jio ipo shareholder quota"]])


# --- insight -------------------------------------------------------------------

def test_insight_finds_the_subject_and_ranks_videos_by_views_per_hour():
    board = _seed()
    ev = insight.analyse("Jio IPO", now=NOW, board=board, suggest_feed="yt", fetch=suggest_fetch)
    assert ev["matched"] == 15 and ev["publishers"] == 13
    assert [v["title"] for v in ev["videos"]] == ["Jio IPO: apply or avoid?", "Jio IPO GMP today"]
    assert ev["videos"][0]["views_per_hour"] == 9000 and ev["videos"][1]["views_per_hour"] == 1000
    assert ev["searches"] == ["jio ipo", "jio ipo price band", "jio ipo shareholder quota"]
    assert ev["suggest_feed"] == "YouTube search"
    assert ev["now"]["label"] == JIO and ev["now"]["state"] in ("RISING", "PEAKING", "EMERGING")
    assert {"phrase": "jio platforms", "count": 13} in ev["phrases"]
    assert sum(d["articles"] for d in ev["daily"]) == 13 and sum(d["videos"] for d in ev["daily"]) == 2
    assert ev["past"] and ev["past"][0]["label"] == JIO


def test_insight_generic_words_alone_do_not_match_everything():
    _seed()
    assert insight.analyse("share price today", now=NOW)["matched"] == 0
    assert insight.analyse("Tata Motors demerger", now=NOW)["matched"] == 0
    assert insight.analyse("   ", now=NOW)["matched"] == 0


def test_a_descriptive_topic_line_still_finds_the_whole_subject():
    _seed()
    # subject + angle + format word: must find the Jio IPO coverage, not just near-identical titles
    assert insight.analyse("Jio IPO shareholder quota explained", now=NOW)["matched"] == 15
    _, match = insight._matcher("Jio IPO shareholder quota explained")
    assert match({"jio", "ipo", "open"}) and match({"jio", "quota"})
    assert not match({"jio", "recharge", "plan"})            # the name alone is not the subject
    assert not match({"reliance", "shareholder", "meeting"})
    assert not match({"ipo", "allotment", "status"})         # generic overlap only


def test_suggest_backs_off_to_shorter_prefixes():
    asked = []

    def fetch(url):
        q = url.split("&q=")[1].replace("%20", " ")
        asked.append(q)
        return json.dumps([q, ["jio ipo", "jio ipo gmp"] if q == "jio ipo" else []])

    assert insight.suggest("jio ipo shareholder quota explained", fetch=fetch) == ["jio ipo", "jio ipo gmp"]
    assert asked == ["jio ipo shareholder quota explained", "jio ipo shareholder quota",
                     "jio ipo shareholder", "jio ipo"]


def test_suggest_is_fail_soft():
    def boom(url):
        raise OSError("down")
    assert insight.suggest("jio ipo", fetch=boom) == []
    assert insight.suggest("jio ipo", fetch=lambda u: "not json") == []
    assert insight.suggest("", fetch=suggest_fetch) == []
    ev = insight.analyse("Jio IPO", now=NOW, suggest_feed="yt", fetch=boom)
    assert ev["searches"] == []                       # analysis survives a dead suggestion feed


# --- research with search off --------------------------------------------------

def test_search_off_research_is_grounded_in_collected_headlines_not_a_search_prompt(monkeypatch):
    from engine import research
    _seed()
    monkeypatch.setattr(insight, "analyse", lambda topic, **k: insight.__dict__["_real"](topic, now=NOW))
    seen = {}
    monkeypatch.setattr(research.llm, "run_with_web_search",
                        lambda system, user, use_search=None: seen.update(s=system, u=user, f=use_search) or "notes")
    assert research.gather("Jio IPO", use_search=False) == "notes"
    assert seen["f"] is False
    # the bug: telling a model with no tools to "search the web" made Gemini return nothing
    assert "search the web" not in seen["s"].lower() and "no search tool" in seen["s"]
    assert "COLLECTED HEADLINES" in seen["u"] and JIO in seen["u"] and "13 publishers" in seen["u"]
    assert "Jio IPO: apply or avoid?" in seen["u"]            # competing video included


def test_search_off_research_says_so_when_the_store_has_nothing(monkeypatch):
    from engine import research
    seen = {}
    monkeypatch.setattr(research.llm, "run_with_web_search",
                        lambda system, user, use_search=None: seen.update(u=user) or "notes")
    research.gather("Some unknown subject xyz", use_search=False)
    assert "none found for this topic" in seen["u"]


def test_search_on_research_still_uses_the_live_search_prompt(monkeypatch):
    from engine import research
    seen = {}
    monkeypatch.setattr(research.llm, "run_with_web_search",
                        lambda system, user, use_search=None: seen.update(s=system, f=use_search) or "notes")
    research.gather("Jio IPO", use_search=True)
    assert seen["f"] is True and "search the web" in seen["s"]


# --- strategy read -------------------------------------------------------------

def test_strategy_follows_the_trend_stage():
    base = {"matched": 5, "videos": [], "searches": [], "days_of_history": 30}
    read = lambda state, v=None: content.strategy(  # noqa: E731
        {**base, "now": {"state": state, "velocity_pct": v}})
    assert read("RISING", 120)["mode"] == "news" and "+120%" in read("RISING", 120)["headline"]
    assert read("PEAKING", 4)["mode"] == "angle"
    assert read("FADING", -60)["mode"] == "search" and "-60%" in read("FADING", -60)["headline"]
    assert content.strategy({**base, "now": None})["mode"] == "search"
    assert "Nothing found on this subject" in content.strategy({"matched": 0})["headline"]


def test_strategy_admits_thin_history():
    s = content.strategy({"matched": 0, "days_of_history": 1})
    assert any("1 day(s) of trend history" in n for n in s["notes"])
    assert not any("history" in n for n in content.strategy({"matched": 0, "days_of_history": 30})["notes"])


# --- limits enforced in code ---------------------------------------------------

YT = content.formats()["youtube"]["fields"]


def test_enforce_trims_tags_to_the_character_limit_and_dedupes():
    tags = ["Jio IPO", "jio ipo", "#jio, ipo review"] + [f"long tag number {i:03d}" for i in range(60)]
    pkg, notes = content.enforce({"titles": [], "description": "", "tags": tags, "hashtags": [],
                                  "thumbnail_text": [], "pinned_comment": ""}, YT)
    assert pkg["tags"][:2] == ["Jio IPO", "jio ipo review"]          # duplicate dropped, # and comma removed
    assert pkg["tags_chars"] <= 500 == YT["tags"]["max_total"]
    assert pkg["tags_chars"] == sum(map(len, pkg["tags"])) + len(pkg["tags"]) - 1
    assert any("Tags trimmed" in n for n in notes)


def test_enforce_flags_long_titles_but_never_silently_cuts_them():
    long_title = "x" * 120
    pkg, notes = content.enforce({"titles": [{"text": long_title, "why": ""}, {"text": "y" * 70, "why": ""},
                                             {"text": "Jio IPO explained", "why": ""}],
                                  "description": "d", "tags": [], "hashtags": ["#Jio IPO", "jioipo", "Markets", "x", "y"],
                                  "thumbnail_text": ["one two three four five six", "ok"], "pinned_comment": ""}, YT)
    t = pkg["titles"]
    assert t[0]["text"] == long_title and t[0]["over_limit"] is True
    assert (t[1]["over_limit"], t[1]["truncated_in_search"]) == (False, True)
    assert (t[2]["over_limit"], t[2]["truncated_in_search"]) == (False, False)
    assert pkg["hashtags"] == ["JioIPO", "Markets", "x"]              # cleaned, de-duplicated, capped at 3
    assert any("over the 100-character limit" in n for n in notes)
    assert any("Hashtags cut" in n for n in notes) and any("thumbnail text" in n for n in notes)


def test_every_platform_has_a_usable_spec():
    ids = [p["id"] for p in content.platforms()]
    assert ids == ["youtube", "youtube_shorts", "instagram_reel", "facebook", "twitter", "linkedin"]
    assert content.languages() == ["english", "hinglish", "hindi"]
    for pid in ids:
        spec = content.formats()[pid]
        schema = content._schema(spec["fields"])
        assert schema["required"] == list(schema["properties"]) and schema["additionalProperties"] is False
        assert spec["rules"] and "hashtags" in spec["fields"]
    assert content.formats()["instagram_reel"]["fields"]["hashtags"]["max"] == 5   # Instagram's cap


# --- generate ------------------------------------------------------------------

def test_generate_feeds_evidence_and_rules_to_the_writer_and_saves(monkeypatch):
    board = _seed()
    seen = {}

    def fake(system, user, schema, max_tokens=0):
        seen.update(system=system, user=user)
        return {"titles": [{"text": "Jio IPO Shareholder Quota Explained", "why": "top suggestion"}] * 3,
                "description": "What the Jio IPO shareholder quota means.", "tags": ["jio ipo"],
                "hashtags": ["JioIPO", "IPO"], "thumbnail_text": ["QUOTA TRICK?"], "pinned_comment": "Applying?"}

    monkeypatch.setattr(content.llm, "structured", fake)
    out = content.generate("youtube", "Jio IPO", "price band, quota rules", "hinglish", board, suggest_fetch)
    assert "jio ipo shareholder quota" in seen["user"]               # real search wording reached the writer
    assert "Jio IPO: apply or avoid?" in seen["user"] and "9,000/h" in seen["user"]
    assert "price band, quota rules" in seen["user"]
    assert "Never invent timestamps" in seen["system"] and "Hinglish" in seen["system"]
    assert "No guaranteed returns" in seen["system"]
    assert out["package"]["titles"][0]["chars"] == 35 and out["limit_notes"] == []
    saved = content.recent()[0]
    assert saved["platform"] == "youtube" and saved["topic"] == "Jio IPO" and saved["trend_state"]


def test_lint_catches_filler_repeated_thumbnail_and_figures_not_in_notes():
    pkg = {"titles": [{"text": "Understanding Jio IPO Shareholder Quota"},
                      {"text": "Jio IPO: ₹11 Lakh Crore Valuation"}],
           "description": "Discover everything about the quota. The band is ₹1,048 and 35% is reserved.",
           "thumbnail_text": ["Shareholder Quota Explained", "WHO GETS IN?"],
           "pinned_comment": "What are your thoughts?"}
    issues = lint_text = "\n".join(content.lint(pkg, "Jio IPO shareholder quota", "band ₹1,048; who qualifies"))
    assert "opens with a filler word" in issues
    assert "“Discover everything" in issues and "“What are your thoughts”" in lint_text
    assert "“Shareholder Quota Explained” repeats the title" in issues and "WHO GETS IN" not in issues
    assert "“₹11 Lakh Crore”" in issues and "“35%”" in issues
    assert "1,048" not in issues                         # the creator gave that figure


def test_lint_flags_a_title_left_as_a_raw_lowercase_search_query():
    issues = content.lint({"titles": [{"text": "rbi repo rate cut effect on home loan"},
                                      {"text": "RBI Repo Rate hike का Home Loan EMI पर असर"},
                                      {"text": "निफ्टी कल कैसा रहेगा"}]}, "RBI repo rate", "")
    assert len(issues) == 1 and "all lower-case" in issues[0] and "rbi repo rate cut" in issues[0]


def test_clean_copy_passes_lint():
    pkg = {"titles": [{"text": "Jio IPO Shareholder Quota: Who Qualifies?"}],
           "description": "Who qualifies for the Reliance shareholder quota and how to apply.",
           "thumbnail_text": ["ARE YOU ELIGIBLE?"], "pinned_comment": "Applying under the quota or retail?"}
    assert content.lint(pkg, "Jio IPO shareholder quota", "") == []


def test_generate_rewrites_once_when_lint_fails_and_reports_what_remains(monkeypatch):
    drafts = iter([
        {"description": "Discover everything about Jio.", "hashtags": ["Jio"], "hooks": []},      # weak
        {"description": "Still gaining momentum, sadly.", "hashtags": ["Jio"], "hooks": []},       # still weak
    ])
    prompts = []

    def fake(s, u, sc, max_tokens=0):
        if "audience_intent" in sc["properties"]:        # the planning step, not a draft
            return PLAN
        prompts.append(u)
        return next(drafts)
    monkeypatch.setattr(content.llm, "structured", fake)
    out = content.generate("twitter", "Jio IPO", fetch=suggest_fetch)
    assert len(prompts) == 2 and "Discover everything" in prompts[1] and "Fix exactly these" in prompts[1]
    assert "STRATEGY TO EXECUTE" in prompts[0] and "who qualifies" in prompts[0]      # the plan reaches the writer
    assert out["plan"] == PLAN
    assert out["revised"] is True and any("gaining momentum" in q for q in out["quality_notes"])

    prompts.clear()
    drafts = iter([{"description": "Jio lists Friday.", "hashtags": ["JioIPO"], "hooks": []}])
    out = content.generate("twitter", "Jio IPO", fetch=suggest_fetch)
    assert len(prompts) == 1 and out["revised"] is False and out["quality_notes"] == []


def test_generate_without_notes_tells_the_writer_not_to_guess(monkeypatch):
    seen = {}
    monkeypatch.setattr(content.llm, "structured",
                        lambda s, u, sc, max_tokens=0: seen.update(user=u) or {"description": "x", "hashtags": []})
    content.generate("twitter", "Jio IPO", fetch=suggest_fetch)
    assert "do not guess specifics" in seen["user"]
    with pytest.raises(ValueError):
        content.analyse("mastodon", "Jio IPO")


# --- routes --------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(insight, "suggest", lambda *a, **k: ["jio ipo price band"])
    app_module.trends.service._board = None
    return app_module.app.test_client()


def test_content_route_validation(client):
    post = lambda **b: client.post("/api/content", json=b)  # noqa: E731
    assert post(platform="mastodon", topic="x").status_code == 400
    assert post(platform=["youtube"], topic="x").status_code == 400
    assert post(platform="youtube", topic="  ").status_code == 400
    assert post(platform="youtube", topic="x" * 201).status_code == 400
    d = client.get("/api/content/formats").get_json()
    assert len(d["platforms"]) == 6 and d["recent"] == []


def test_analyse_only_is_free_and_needs_no_key(client, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(content.llm, "structured", lambda *a, **k: pytest.fail("LLM must not be called"))
    r = client.post("/api/content", json={"platform": "youtube", "topic": "Jio IPO", "analyse_only": True})
    d = r.get_json()
    assert r.status_code == 200 and "package" not in d
    assert d["evidence"]["searches"] == ["jio ipo price band"] and d["strategy"]["mode"] == "search"


def test_generate_route_needs_a_key(client, monkeypatch):
    monkeypatch.setattr(app_module.llm, "has_api_key", lambda: False)
    r = client.post("/api/content", json={"platform": "youtube", "topic": "Jio IPO"})
    assert r.status_code == 400 and "Analyse trends" in r.get_json()["error"]
