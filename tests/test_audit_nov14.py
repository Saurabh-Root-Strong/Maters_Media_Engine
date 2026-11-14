"""Findings of the 14 Nov 2026 audit (model drop-down), pinned as tests."""

import threading

from engine import content, llm


def test_a_joined_emoji_counts_once():
    # woman-in-lotus = person + ZWJ + female sign: one emoji to a viewer
    assert content._emojis("sukoon \U0001F9D8‍♀️") == 1
    assert content._emojis("ok \U0001F44D\U0001F3FD and ❤️") == 2
    assert content._emojis("plain text") == 0


def test_extra_emojis_are_removed_by_code_not_noted():
    text = "Chart \U0001F4F1 on, net \U0001F4CA on, sukoon \U0001F9D8‍♀️. Pool \U0001F30A✨"
    out = content._trim_emojis(text, 3)
    assert content._emojis(out) == 3 and "\U0001F30A" not in out and "Pool" in out
    assert content._trim_emojis("no emoji here", 1) == "no emoji here"
    assert content._trim_emojis("Sukoon \U0001F9D8 \U0001F4F1 !", 1) == "Sukoon \U0001F9D8!"


def test_off_video_pattern_reads_camelcase_hashtags():
    import re
    words = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", "BankNifty")
    assert content._OFF_VIDEO_TAG.search(words)
    assert content._OFF_VIDEO_TAG.search("stock market today") and content._OFF_VIDEO_TAG.search("option trading")
    assert not content._OFF_VIDEO_TAG.search("trader lifestyle status")


def test_the_pinned_model_is_per_thread():
    """Picking Claude in one request must not pin (or strip the fallback of) another."""
    seen = {}
    llm.force("claude:sonnet")
    try:
        t = threading.Thread(target=lambda: seen.setdefault("other", llm._forced.get()))
        t.start()
        t.join()
        assert llm.writers() == [("claude", "sonnet")] and seen["other"] is None
    finally:
        llm.force(None)


def test_menu_lists_only_models_that_answered(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    monkeypatch.setattr(llm, "claude_cli", lambda: None)
    ids = [c["id"] for c in llm.choices()]
    assert "gemini:gemini-pro-latest" not in ids and "gemini:gemini-flash-latest" not in ids
    assert ids[0] == "" and "gemini:gemini-3.5-flash" in ids and "openai:gpt-5.2" in ids


def test_claude_cli_passes_low_effort_and_reports_limits_plainly(monkeypatch):
    import json
    import subprocess

    import pytest
    monkeypatch.setattr(llm, "claude_cli", lambda: "claude.exe")
    seen = {}

    class Ok:
        returncode, stderr = 0, ""
        stdout = json.dumps({"is_error": False, "result": "hello"})

    def run(cmd, **kw):
        seen["cmd"] = cmd
        return Ok()
    monkeypatch.setattr(subprocess, "run", run)
    assert llm._claude_complete("sonnet", "s", "u") == "hello"
    assert seen["cmd"][seen["cmd"].index("--effort") + 1] == "low"

    class Limit:
        returncode, stderr = 1, ""
        stdout = json.dumps({"is_error": True, "result": "Usage limit reached. Resets at 3pm."})
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Limit())
    with pytest.raises(RuntimeError, match="Resets at 3pm"):
        llm._claude_complete("sonnet", "s", "u")


def test_a_timeout_is_not_retried_as_a_schema_problem(monkeypatch):
    import pytest
    calls = []

    class APITimeoutError(Exception):
        pass

    class Client:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    calls.append(1)
                    raise APITimeoutError("Request timed out.")
    monkeypatch.setattr(llm, "_compat", lambda p: Client)
    with pytest.raises(APITimeoutError):
        llm._compat_structured("gemini", "m", "s", "u", {"type": "object", "required": []})
    assert len(calls) == 1
