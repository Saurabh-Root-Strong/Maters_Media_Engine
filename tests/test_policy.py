"""Auto-publish policy — the full scenario matrix, incl. the safety invariants."""

from engine import policy
from engine.policy import (
    BLOCK, DRY_RUN, HOLD_FOR_HUMAN, PUBLISH, SKIP, Config, decide,
)

ALL = ["twitter", "instagram", "linkedin"]


def _review(verdict, sensitive=False, platforms=ALL):
    return {"sensitive": sensitive, "gate": {p: {"verdict": verdict} for p in platforms}}


def _actions(rev, creds, cfg):
    return {p: d["action"] for p, d in decide(rev, creds, cfg).items()}


ALL_CREDS = {p: True for p in ALL}
NO_CREDS = {p: False for p in ALL}


def test_sensitive_always_holds_even_fully_authorized():
    got = _actions(_review("PASS", sensitive=True), ALL_CREDS, Config(True, True))
    assert set(got.values()) == {HOLD_FOR_HUMAN}


def test_sensitive_beats_missing_creds():
    got = _actions(_review("PASS", sensitive=True), NO_CREDS, Config(True, True))
    assert set(got.values()) == {HOLD_FOR_HUMAN}


def test_hold_verdict_holds():
    assert _actions(_review("HOLD"), ALL_CREDS, Config(True, True)) == {p: HOLD_FOR_HUMAN for p in ALL}


def test_revise_blocks():
    assert _actions(_review("REVISE"), ALL_CREDS, Config(True, True)) == {p: BLOCK for p in ALL}


def test_clean_auto_live_creds_publishes():
    assert _actions(_review("PASS"), ALL_CREDS, Config(True, True)) == {p: PUBLISH for p in ALL}


def test_clean_auto_live_no_creds_skips():
    assert _actions(_review("PASS"), NO_CREDS, Config(True, True)) == {p: SKIP for p in ALL}


def test_dry_run_when_not_live():
    assert _actions(_review("PASS"), ALL_CREDS, Config(True, False)) == {p: DRY_RUN for p in ALL}


def test_auto_off_holds_for_human():
    assert _actions(_review("PASS"), ALL_CREDS, Config(False, True)) == {p: HOLD_FOR_HUMAN for p in ALL}


def test_disabled_platform_skips():
    d = decide(_review("PASS"), ALL_CREDS, Config(True, True, enabled={"twitter"}))
    assert d["twitter"]["action"] == PUBLISH
    assert d["instagram"]["action"] == SKIP


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("MEDIA_ENGINE_AUTOPUBLISH", "true")
    monkeypatch.setenv("MEDIA_ENGINE_LIVE", "false")
    monkeypatch.setenv("MEDIA_ENGINE_PLATFORMS", "twitter,linkedin")
    c = Config.from_env()
    assert c.auto_publish is True and c.live is False
    assert c.enabled == {"twitter", "linkedin"}
