"""Claude Code CLI writer: failures must say why, and schema mode has a second way in."""

import json
import subprocess

import pytest

from engine import llm

SCHEMA = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"],
          "additionalProperties": False}


class Reply:
    def __init__(self, body, code=0, stderr=""):
        self.returncode, self.stderr = code, stderr
        self.stdout = body if isinstance(body, str) else json.dumps(body)


@pytest.fixture
def cli(monkeypatch):
    monkeypatch.setattr(llm, "claude_cli", lambda: "claude.exe")
    calls = []

    def script(*replies):
        queue = list(replies)

        def run(cmd, **kw):
            calls.append(cmd)
            return queue.pop(0)
        monkeypatch.setattr(llm, "_run_bounded", lambda cmd, text, limit, _f=run: _f(cmd, input=text))
        return calls
    return script


def test_an_error_without_a_result_still_says_why(cli):
    # what the creator saw on 14 Nov: "RuntimeError: Claude Code:" and nothing after it
    cli(Reply({"is_error": True, "subtype": "error_max_structured_output_retries",
               "errors": ["could not produce valid output"]}, code=1))
    with pytest.raises(RuntimeError) as e:
        llm._claude_complete("opus", "s", "u")
    assert "could not produce valid output" in str(e.value) and "error_max_structured_output_retries" in str(e.value)


def test_an_error_with_nothing_at_all_is_never_blank(cli):
    cli(Reply({"is_error": True}, code=1))
    with pytest.raises(RuntimeError, match="no reason given"):
        llm._claude_complete("opus", "s", "u")


def test_schema_mode_failing_falls_back_to_plain_json(cli):
    calls = cli(Reply({"is_error": True, "subtype": "error_during_execution"}, code=1),
                Reply({"is_error": False, "result": '```json\n{"a": "x"}\n```'}))
    assert llm._claude_structured("opus", "sys", "user", SCHEMA) == {"a": "x"}
    assert "--json-schema" in calls[0] and "--json-schema" not in calls[1]
    assert "JSON Schema" in calls[1][calls[1].index("--system-prompt") + 1]


def test_a_usage_limit_is_not_retried(cli):
    calls = cli(Reply({"is_error": True, "result": "Claude usage limit reached. Resets at 6pm."}, code=1))
    with pytest.raises(RuntimeError, match="Resets at 6pm"):
        llm._claude_structured("opus", "sys", "user", SCHEMA)
    assert len(calls) == 1


def test_both_attempts_failing_reports_both(cli):
    cli(Reply({"is_error": True, "subtype": "error_during_execution"}, code=1),
        Reply({"is_error": False, "result": "Sorry, here is some prose."}))
    with pytest.raises(RuntimeError) as e:
        llm._claude_structured("opus", "sys", "user", SCHEMA)
    assert "error_during_execution" in str(e.value) and "second attempt" in str(e.value)
