"""Fail-closed gate, crash-safe scheduler, store isolation and input hardening."""

import json
import os
import time
from datetime import timedelta

import pytest

import app as app_module
from engine import autopost, memory, orchestrator, policy, research, review, trending
from engine import scheduler as sched_mod
from engine.scheduler import Scheduler

FROZEN = {"topic": "RBI rate", "drafts": {"twitter": {"caption": "hi", "hashtags": []}}}


@pytest.fixture
def client():
    return app_module.app.test_client()


# --- review gate fails closed --------------------------------------------------

def _crit(platforms):
    return lambda b, a, d: {"overall": {"recommendation": "PASS", "summary": "ok"},
                            "platforms": platforms}


def test_review_holds_platform_the_critic_skipped(monkeypatch):
    monkeypatch.setattr(review, "critique", _crit([]))
    out = review.review({"sensitivity_flags": []}, {},
                        {"twitter": {"caption": "hi there", "hashtags": []}})
    assert out["gate"]["twitter"]["verdict"] == "HOLD"
    assert out["requires_human"] is True


def test_review_matches_platform_case_insensitively(monkeypatch):
    monkeypatch.setattr(review, "critique", _crit(
        [{"platform": " Twitter ", "verdict": "REVISE", "issues": ["invented number"]}]))
    out = review.review({"sensitivity_flags": []}, {},
                        {"twitter": {"caption": "hi there", "hashtags": []}})
    assert out["gate"]["twitter"]["verdict"] == "REVISE"
    assert out["gate"]["twitter"]["editorial_issues"] == ["invented number"]


def test_review_unknown_verdict_becomes_hold(monkeypatch):
    monkeypatch.setattr(review, "critique", _crit(
        [{"platform": "twitter", "verdict": "LOOKS_FINE", "issues": []}]))
    out = review.review({"sensitivity_flags": []}, {},
                        {"twitter": {"caption": "hi there", "hashtags": []}})
    assert out["gate"]["twitter"]["verdict"] == "HOLD"


def test_critique_schema_pins_platform_to_draft_keys(monkeypatch):
    seen = {}
    monkeypatch.setattr(review.llm, "structured",
                        lambda s, u, schema, max_tokens=0: seen.setdefault("schema", schema))
    review.critique({}, {}, {"twitter": {}, "linkedin": {}})
    enum = seen["schema"]["properties"]["platforms"]["items"]["properties"]["platform"]["enum"]
    assert enum == ["twitter", "linkedin"]
    # the module-level schema must not have been mutated
    assert "enum" not in review._CRITIQUE_SCHEMA["properties"]["platforms"]["items"][
        "properties"]["platform"]


@pytest.mark.parametrize("gate", [{}, {"verdict": "LOOKS_FINE"}])
def test_policy_never_publishes_without_explicit_pass(gate):
    cfg = policy.Config(auto_publish=True, live=True)
    d = policy.decide({"sensitive": False, "gate": {"twitter": gate}}, {"twitter": True}, cfg)
    assert d["twitter"]["action"] == policy.HOLD_FOR_HUMAN


def test_dashboard_missing_verdict_needs_confirm(client):
    app_module._RUNS["nv"] = {"frozen": FROZEN, "review": {"sensitive": False, "gate": {}}}
    r = client.post("/api/publish", json={"run_id": "nv", "platform": "twitter"})
    assert r.get_json()["status"] == "needs_confirm"


# --- scheduler crash safety ----------------------------------------------------

def test_firing_claim_is_persisted_before_the_fire(monkeypatch):
    s = Scheduler()
    job = s.add("twitter", time.time() - 1, "now", FROZEN, live=False)
    on_disk = {}

    def spy(j):
        with open(s._path(j["id"]), encoding="utf-8") as f:
            on_disk["status"] = json.load(f)["status"]
        j["status"] = "posted"

    monkeypatch.setattr(s, "_fire", spy)
    s._tick()
    assert on_disk["status"] == "firing"
    assert s._jobs[job["id"]]["status"] == "posted"


def test_interrupted_fire_reloads_as_error_not_pending():
    s = Scheduler()
    job = s.add("twitter", time.time() - 1, "now", FROZEN, live=True)
    s._jobs[job["id"]]["status"] = "firing"      # simulate a crash mid-fire
    s._save(s._jobs[job["id"]])

    fresh = Scheduler()
    fresh.load()
    reloaded = fresh._jobs[job["id"]]
    assert reloaded["status"] == "error" and "interrupted" in reloaded["detail"]
    fresh._tick()                                # must not fire it again
    assert fresh._jobs[job["id"]]["status"] == "error"


def test_error_job_can_be_dismissed():
    s = Scheduler()
    job = s.add("twitter", time.time() + 999, "later", FROZEN, live=False)
    s._jobs[job["id"]]["status"] = "error"
    assert len(s.list()) == 1
    assert s.cancel(job["id"]) is True
    assert s.list() == []
    assert s.cancel(["not", "a", "string"]) is False


# --- stores are isolated + history ---------------------------------------------

def test_suite_never_touches_real_output_dir():
    real = os.path.join(os.path.dirname(app_module.__file__), "output")
    assert not os.path.abspath(memory._HISTORY_PATH).startswith(os.path.abspath(real))
    assert not os.path.abspath(sched_mod._DIR).startswith(os.path.abspath(real))


def test_recent_topics_dedupes_keeping_latest_order():
    for t in ("a", "b", "a", "c", "a"):
        memory.record_topic(t)
    assert memory.recent_topics(10) == ["b", "c", "a"]
    assert memory.recent_topics(2) == ["c", "a"]


# --- research / trending -------------------------------------------------------

def test_research_refuses_to_distill_empty_notes(monkeypatch):
    monkeypatch.setattr(research, "gather", lambda t, s=None: "   ")
    with pytest.raises(RuntimeError, match="no notes"):
        research.research("x")


def test_placeholder_sensitivity_flags_are_dropped(monkeypatch):
    monkeypatch.setattr(research.llm, "structured",
                        lambda s, u, schema: {"sensitivity_flags": ["None", "N/A", " none. "]})
    assert research.distill("notes")["sensitivity_flags"] == []
    monkeypatch.setattr(research.llm, "structured",
                        lambda s, u, schema: {"sensitivity_flags": ["none", "ongoing SEBI probe"]})
    assert research.distill("notes")["sensitivity_flags"] == ["ongoing SEBI probe"]


def test_trending_rejects_future_and_stale_dates():
    today = trending._today()
    fmt = "%Y-%m-%d"
    assert trending._fresh_enough(today.strftime(fmt)) is True
    assert trending._fresh_enough((today + timedelta(days=30)).strftime(fmt)) is False
    assert trending._fresh_enough((today - timedelta(days=30)).strftime(fmt)) is False
    assert trending._fresh_enough("last week") is False


# --- orchestrator --------------------------------------------------------------

def _stub_pipeline(monkeypatch, redraft_caption):
    calls = {"redraft": 0, "image_paths": []}
    monkeypatch.setattr(orchestrator.research, "research",
                        lambda t, use_search=None: {"sensitivity_flags": [], "sources": []})
    monkeypatch.setattr(orchestrator.angle_stage, "choose", lambda b, t=None: {"angle": "a"})
    monkeypatch.setattr(orchestrator.drafters, "draft_all",
                        lambda b, a, sel, fm: {"twitter": {"caption": "x" * 400, "hashtags": []}})

    def redraft(key, spec, brief, angle, issues, fmt_id=None):
        calls["redraft"] += 1
        return {"caption": redraft_caption(calls["redraft"]), "hashtags": []}

    monkeypatch.setattr(orchestrator.drafters, "redraft", redraft)
    monkeypatch.setattr(orchestrator.review, "critique", lambda b, a, d: {
        "overall": {"recommendation": "PASS", "summary": "ok"},
        "platforms": [{"platform": k, "verdict": "PASS", "issues": []} for k in d]})
    return calls


def test_auto_revise_retries_then_passes_and_records_topic(monkeypatch):
    # first redraft still too long, second fits
    calls = _stub_pipeline(monkeypatch, lambda n: "x" * (400 if n == 1 else 100))
    out = orchestrator.run("Nifty record", platforms_selected=["twitter"])
    assert calls["redraft"] == 2
    assert out["review"]["gate"]["twitter"]["verdict"] == "PASS"
    assert memory.recent_topics() == ["Nifty record"]


def test_auto_revise_is_capped_and_survivor_is_blocked(monkeypatch):
    calls = _stub_pipeline(monkeypatch, lambda n: "x" * 400)
    out = orchestrator.run("Nifty record", platforms_selected=["twitter"])
    assert calls["redraft"] == orchestrator._MAX_REVISE_ROUNDS
    assert out["review"]["gate"]["twitter"]["verdict"] == "REVISE"


def test_hashtags_are_dropped_before_a_tweet_is_blocked_for_length(monkeypatch):
    # 275 chars + "\n\n#JioIPO #Reliance" = 294: over. Without tags it fits — and tags add no reach on X.
    _stub_pipeline(monkeypatch, lambda n: "x" * 275)
    monkeypatch.setattr(orchestrator.drafters, "draft_all", lambda b, a, sel, fm: {
        "twitter": {"caption": "x" * 275, "hashtags": ["JioIPO", "Reliance"]}})
    monkeypatch.setattr(orchestrator.drafters, "redraft",
                        lambda key, spec, brief, angle, issues, fmt_id=None:
                        {"caption": "x" * 275, "hashtags": ["JioIPO", "Reliance"]})
    out = orchestrator.run("Jio IPO valuation", platforms_selected=["twitter"])
    assert out["drafts"]["twitter"]["hashtags"] == []
    assert out["review"]["gate"]["twitter"]["verdict"] == "PASS"


def test_hashtags_are_kept_when_only_one_needs_to_go(monkeypatch):
    _stub_pipeline(monkeypatch, lambda n: "x" * 265)
    draft = {"caption": "x" * 265, "hashtags": ["JioIPO", "Reliance"]}       # 265+2+7+1+9 = 284
    monkeypatch.setattr(orchestrator.drafters, "draft_all", lambda b, a, sel, fm: {"twitter": dict(draft)})
    monkeypatch.setattr(orchestrator.drafters, "redraft",
                        lambda key, spec, brief, angle, issues, fmt_id=None: dict(draft))
    out = orchestrator.run("Jio IPO valuation", platforms_selected=["twitter"])
    assert out["drafts"]["twitter"]["hashtags"] == ["JioIPO"]               # 274 fits


def test_image_filename_is_unique_per_run(monkeypatch):
    _stub_pipeline(monkeypatch, lambda n: "ok")
    monkeypatch.setattr(orchestrator.drafters, "draft_all", lambda b, a, sel, fm: {
        "instagram": {"caption": "ok", "hashtags": ["a", "b", "c"]}})
    paths = []
    monkeypatch.setattr(orchestrator.imagegen, "generate",
                        lambda d, a, out_path, **kw: paths.append(out_path) or {"path": out_path})
    orchestrator.run("Same topic", platforms_selected=["instagram"])
    name = os.path.basename(paths[0])
    assert name.startswith("same-topic-") and name.endswith(".png")
    assert name != "same-topic.png"


def test_autopost_drafts_only_enabled_platforms(monkeypatch):
    seen = {}

    def fake_run(topic, platforms_selected=None, on_progress=None):
        seen["selected"] = platforms_selected
        return {"topic": topic, "brief": {"sources": []}, "drafts": {},
                "review": {"recommendation": "PASS", "sensitive": False, "gate": {}}}

    monkeypatch.setattr(autopost.orchestrator, "run", fake_run)
    autopost.run("t", policy.Config(enabled={"twitter"}))
    assert seen["selected"] == ["twitter"]
    with pytest.raises(ValueError):
        autopost.run("t", policy.Config(enabled={"mastodon"}))


# --- dashboard input hardening -------------------------------------------------

@pytest.mark.parametrize("name", ["history.json", "scheduled/abc.json", "x.approved.json",
                                  "sub/pic.png"])
def test_output_route_serves_images_only(client, name):
    assert client.get(f"/output/{name}").status_code == 404


@pytest.mark.parametrize("platforms", [None, "twitter", 5, [None], [["twitter"]]])
def test_generate_bad_platforms_is_400_not_500(client, monkeypatch, platforms):
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    r = client.post("/api/generate", json={"topic": "hi", "platforms": platforms})
    assert r.status_code == 400


def test_publish_and_schedule_reject_platform_without_a_draft(client):
    app_module._RUNS["nd"] = {"frozen": FROZEN, "review": {
        "sensitive": False, "gate": {"twitter": {"verdict": "PASS"}}}}
    r = client.post("/api/publish", json={"run_id": "nd", "platform": "linkedin"})
    assert r.status_code == 400
    r = client.post("/api/schedule", json={"run_id": "nd", "platform": "linkedin",
                                           "when_epoch": time.time() + 600})
    assert r.status_code == 400


@pytest.mark.parametrize("body", [{"run_id": ["a"], "platform": "twitter"},
                                  {"run_id": "nope", "platform": ["twitter"]}])
def test_publish_unhashable_ids_do_not_500(client, body):
    assert client.post("/api/publish", json=body).status_code in (400, 404)


def test_postability_reports_the_count_the_gate_judges():
    drafts = {"twitter": {"caption": "🚨 Nifty up", "hashtags": ["Nifty"]}}
    rev = {"sensitive": False, "gate": {"twitter": {"verdict": "PASS"}}}
    p = app_module._postability(rev, drafts)["twitter"]
    # "🚨 Nifty up" = 10 chars + 1 extra for the emoji, "\n\n" = 2, "#Nifty" = 6
    assert p["chars"] == 19 and p["limit"] == 280
