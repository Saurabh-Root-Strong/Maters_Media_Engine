"""Scheduler — firing, past/future, cancel, persistence."""

import time

import pytest

from engine import scheduler as sched_mod
from engine.scheduler import Scheduler

FROZEN = {"topic": "RBI rate", "drafts": {"twitter": {"caption": "hi", "hashtags": []}}}


@pytest.fixture
def sched(tmp_path, monkeypatch):
    monkeypatch.setattr(sched_mod, "_DIR", str(tmp_path))
    return Scheduler()


def test_due_job_fires_dry_run(sched):
    sched.add("twitter", time.time() - 1, "now", FROZEN, live=False)
    sched.add("twitter", time.time() + 999, "later", FROZEN, live=False)
    sched._tick()
    statuses = sorted(j["status"] for j in sched._jobs.values())
    assert statuses == ["pending", "posted"]
    posted = [j for j in sched._jobs.values() if j["status"] == "posted"]
    assert posted[0]["detail"] == "dry-run"


def test_future_job_not_fired(sched):
    sched.add("twitter", time.time() + 999, "later", FROZEN, live=False)
    sched._tick()
    assert all(j["status"] == "pending" for j in sched._jobs.values())
    assert len(sched.list()) == 1


def test_cancel(sched):
    job = sched.add("twitter", time.time() + 999, "later", FROZEN, live=False)
    assert sched.cancel(job["id"]) is True
    assert sched.list() == []
    assert sched.cancel("nonexistent") is False


def test_persistence_reload(sched, tmp_path, monkeypatch):
    sched.add("twitter", time.time() + 999, "later", FROZEN, live=False)
    fresh = Scheduler()
    monkeypatch.setattr(sched_mod, "_DIR", str(tmp_path))
    fresh.load()
    assert len(fresh._jobs) == 1


def test_public_view_drops_frozen(sched):
    sched.add("twitter", time.time() + 999, "later", FROZEN, live=True)
    j = sched.list()[0]
    assert "frozen" not in j
    assert j["topic"] == "RBI rate" and j["live"] is True
