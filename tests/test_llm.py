"""LLM seam — the writer fallback chain. No network: providers are stubbed."""

import pytest

from engine import llm

SCHEMA = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"],
          "additionalProperties": False}


@pytest.fixture
def chain(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("MEDIA_ENGINE_WRITERS",
                       "gemini:flash-lite, openrouter:x/y:free ,openai:gpt-4o-mini, bogus:m, gemini:")
    return monkeypatch


def test_chain_parsing_drops_entries_without_a_key_or_model(chain):
    # openrouter has no key; "bogus" is not a provider; "gemini:" has no model
    assert llm.writers() == [("gemini", "flash-lite"), ("openai", "gpt-4o-mini")]


def test_model_ids_containing_colons_survive(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("MEDIA_ENGINE_WRITERS", "openrouter:nvidia/nemotron:free")
    assert llm.writers() == [("openrouter", "nvidia/nemotron:free")]


def test_unset_chain_keeps_the_old_single_provider(monkeypatch):
    monkeypatch.setenv("MEDIA_ENGINE_WRITERS", "")
    called = []
    monkeypatch.setattr(llm, "_openai_structured", lambda s, u, sc: called.append(1) or {"a": "x"})
    monkeypatch.setattr(llm, "PROVIDER", "openai")
    assert llm.writers() == [] and llm.structured("s", "u", SCHEMA) == {"a": "x"} and called


def test_first_writer_that_answers_wins(chain):
    calls = []
    chain.setattr(llm, "structured_on", lambda p, m, *a, **k: calls.append(p) or {"a": p})
    assert llm.structured("s", "u", SCHEMA) == {"a": "gemini"}
    assert calls == ["gemini"] and llm.last_writer == "gemini:flash-lite" and llm.last_errors == []


def test_a_failing_free_writer_falls_through_to_the_next(chain):
    def fake(p, m, *a, **k):
        if p == "gemini":
            raise RuntimeError("503 high demand")
        return {"a": p}
    chain.setattr(llm, "structured_on", fake)
    assert llm.structured("s", "u", SCHEMA) == {"a": "openai"}
    assert llm.last_writer == "openai:gpt-4o-mini"
    assert len(llm.last_errors) == 1 and "gemini:flash-lite" in llm.last_errors[0] and "503" in llm.last_errors[0]


def test_every_writer_failing_raises_with_all_reasons(chain):
    def boom(p, m, *a, **k):
        raise RuntimeError(f"{p} down")
    chain.setattr(llm, "structured_on", boom)
    with pytest.raises(RuntimeError, match="every writer failed") as e:
        llm.structured("s", "u", SCHEMA)
    assert "gemini down" in str(e.value) and "openai down" in str(e.value)


def test_has_api_key_is_true_with_only_a_free_writer(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)   # the developer's .env may have set it
    monkeypatch.setenv("MEDIA_ENGINE_WRITERS", "gemini:flash-lite")
    assert llm.has_api_key() is False            # no Gemini key either
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    assert llm.has_api_key() is True


@pytest.mark.parametrize("text", ['{"a": "x"}', '```json\n{"a": "x"}\n```', 'Sure! Here it is:\n{"a": "x"}\nHope that helps.'])
def test_json_parsing_tolerates_fences_and_prose(text):
    assert llm._parse_json(text, SCHEMA) == {"a": "x"}


@pytest.mark.parametrize("text,err", [("no json here", "did not return JSON"), ('["a"]', "did not return JSON|not an object"),
                                      ('{"b": 1}', "missing a"), ("", "did not return JSON")])
def test_json_parsing_rejects_bad_replies(text, err):
    with pytest.raises(RuntimeError, match=err):
        llm._parse_json(text, SCHEMA)


def test_search_route_needs_provider_model_and_key(monkeypatch):
    monkeypatch.setenv("MEDIA_ENGINE_SEARCH", "gemini:flash-lite")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert llm.search_route() is None
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    assert llm.search_route() == ("gemini", "flash-lite")
    monkeypatch.setenv("MEDIA_ENGINE_SEARCH", "openai:gpt")
    assert llm.search_route() is None


def test_search_off_uses_the_free_chain_for_research_text(chain):
    chain.setattr(llm, "_compat_complete", lambda p, m, s, u: f"notes from {p}")
    assert llm.run_with_web_search("s", "u", use_search=False) == "notes from gemini"

# ---------------- the model picked in the UI (force / choices / claude) -----

@pytest.fixture
def unforced():
    yield
    llm.force(None)


def test_a_forced_writer_replaces_the_whole_chain(chain, unforced):
    llm.force("claude:sonnet")
    assert llm.writers() == [("claude", "sonnet")]
    llm.force(None)
    assert llm.writers()[0] == ("gemini", "flash-lite")


def test_a_forced_writer_does_not_fall_back(chain, unforced):
    def boom(p, m, *a, **k):
        raise RuntimeError(f"{p} down")
    chain.setattr(llm, "structured_on", boom)
    llm.force("claude:opus")
    with pytest.raises(RuntimeError, match="claude down"):
        llm.structured("s", "u", SCHEMA)


def test_choices_only_offer_what_this_machine_can_reach(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(llm, "claude_cli", lambda: None)
    assert [c["id"] for c in llm.choices()] == [""]
    monkeypatch.setattr(llm, "claude_cli", lambda: r"C:\x\claude.exe")
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    ids = [c["id"] for c in llm.choices()]
    assert "claude:sonnet" in ids and "gemini:gemini-3.5-flash" in ids
    assert not any(i.startswith("openai:") for i in ids)


def test_claude_cli_structured_prefers_structured_output(monkeypatch):
    import subprocess
    monkeypatch.setattr(llm, "claude_cli", lambda: "claude.exe")

    class R:
        returncode = 0
        stderr = ""
        stdout = __import__("json").dumps({"is_error": False, "structured_output": {"a": "x"},
                                           "result": '```json\n{"a": "from text"}\n```'})
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["input"] = cmd, kw.get("input")
        return R()
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert llm._claude_structured("sonnet", "sys", "user", SCHEMA) == {"a": "x"}
    assert "--json-schema" in seen["cmd"] and seen["input"] == "user"
    assert seen["cmd"][seen["cmd"].index("--model") + 1] == "sonnet"


def test_claude_cli_errors_are_plain(monkeypatch):
    import subprocess
    monkeypatch.setattr(llm, "claude_cli", lambda: "claude.exe")

    class R:
        returncode = 1
        stderr = "not logged in"
        stdout = ""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    with pytest.raises(RuntimeError, match="not logged in"):
        llm._claude_complete("haiku", "s", "u")
