"""A stuck CLI call must end at its limit, even when a child process keeps the pipes open."""

import sys
import time

import pytest

from engine import llm


def test_a_hung_call_is_stopped_with_its_children():
    # parent starts a child that inherits stdout and sleeps: the case where
    # subprocess.run(timeout=...) never returned on Windows
    child = "import time; time.sleep(60)"
    parent = f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(60)"
    t = time.time()
    with pytest.raises(RuntimeError, match="did not answer within 2 seconds"):
        llm._run_bounded([sys.executable, "-c", parent], "", 2)
    assert time.time() - t < 25


def test_a_normal_call_returns_its_output():
    r = llm._run_bounded([sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"], "hello", 20)
    assert r.returncode == 0 and r.stdout.strip() == "HELLO"


def test_a_timeout_does_not_trigger_the_second_attempt(monkeypatch):
    calls = []

    def run(model, system, user, extra):
        calls.append(extra)
        raise RuntimeError("Claude Code did not answer within 300 seconds and was stopped.")
    monkeypatch.setattr(llm, "_claude_run", run)
    with pytest.raises(RuntimeError, match="did not answer"):
        llm._claude_structured("opus", "s", "u", {"type": "object", "required": []})
    assert len(calls) == 1
