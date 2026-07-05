"""Dashboard — server-side enforcement + error paths (no LLM key needed)."""

import time

import pytest

import app as app_module


@pytest.fixture
def client():
    return app_module.app.test_client()


def test_index_loads(client):
    r = client.get("/")
    assert r.status_code == 200 and b"Media-Engine" in r.data


def test_generate_empty_topic(client):
    r = client.post("/api/generate", json={"topic": ""})
    assert r.status_code == 400


def test_generate_no_platforms(client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    r = client.post("/api/generate", json={"topic": "hi", "platforms": []})
    assert r.status_code == 400 and "platform" in r.get_json()["error"].lower()


def test_generate_no_key(client, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    import importlib

    from engine import llm
    importlib.reload(llm)
    r = client.post("/api/generate", json={"topic": "hi"})
    assert r.status_code == 400


def test_generate_happy_path_mocked(client, monkeypatch):
    """Mock the pipeline so we exercise the payload/postability without an API."""
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    import importlib

    from engine import llm
    importlib.reload(llm)
    fake = {
        "topic": "monsoon",
        "brief": {"headline_summary": "h", "sentiment": "s", "trending_keywords": ["k"],
                  "sensitivity_flags": [], "sources": ["u"]},
        "angle": {"angle_type": "explainer", "angle": "a", "key_message": "m", "rationale": "r"},
        "drafts": {"twitter": {"caption": "hi", "hashtags": ["a"]}},
        "image_error": None,
        "review": {"recommendation": "PASS", "critique_summary": "ok", "sensitive": False,
                   "gate": {"twitter": {"verdict": "PASS", "limit_issues": [], "editorial_issues": []}}},
    }
    monkeypatch.setattr(app_module.orchestrator, "run",
                        lambda topic, platforms_selected=None, use_search=None, image_backend=None: fake)
    r = client.post("/api/generate", json={"topic": "monsoon", "platforms": ["twitter"]})
    d = r.get_json()
    assert r.status_code == 200
    assert d["platforms"]["twitter"]["state"] == "ready"
    assert "run_id" in d


def _inject(review):
    rid = "test-run"
    app_module._RUNS[rid] = {"frozen": {"topic": "t", "drafts": {"twitter": {"caption": "hi", "hashtags": []}}},
                             "review": review}
    return rid


def test_publish_unknown_run(client):
    r = client.post("/api/publish", json={"run_id": "nope", "platform": "twitter"})
    assert r.status_code == 404


def test_publish_revise_blocked(client):
    rid = _inject({"sensitive": False, "gate": {"twitter": {"verdict": "REVISE"}}})
    r = client.post("/api/publish", json={"run_id": rid, "platform": "twitter", "confirm": True})
    assert r.get_json()["status"] == "blocked"


def test_publish_sensitive_needs_confirm(client):
    rid = _inject({"sensitive": True, "gate": {"twitter": {"verdict": "PASS"}}})
    r = client.post("/api/publish", json={"run_id": rid, "platform": "twitter", "confirm": False})
    assert r.get_json()["status"] == "needs_confirm"
    r2 = client.post("/api/publish", json={"run_id": rid, "platform": "twitter", "confirm": True})
    assert r2.get_json()["status"] in ("dry-run", "skipped", "posted", "error")


def test_schedule_past_rejected(client):
    rid = _inject({"sensitive": False, "gate": {"twitter": {"verdict": "PASS"}}})
    r = client.post("/api/schedule", json={"run_id": rid, "platform": "twitter",
                                           "when_epoch": time.time() - 100})
    assert r.status_code == 400


def test_schedule_future_ok_then_cancel(client):
    rid = _inject({"sensitive": False, "gate": {"twitter": {"verdict": "PASS"}}})
    r = client.post("/api/schedule", json={"run_id": rid, "platform": "twitter",
                                           "when_epoch": time.time() + 600, "when_label": "soon"})
    assert r.get_json()["status"] == "scheduled"
    jid = r.get_json()["job"]["id"]
    c = client.post("/api/scheduled/cancel", json={"id": jid})
    assert c.get_json()["cancelled"] is True
