"""Two packages written at once (two tabs) must not swap their 'written by'."""

import threading

from engine import llm


def test_who_answered_is_kept_per_thread(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    gate = threading.Barrier(2)
    seen = {}

    def fake(p, m, *a, **k):
        gate.wait(timeout=5)          # both threads are mid-call at the same moment
        return {"a": p}
    monkeypatch.setattr(llm, "structured_on", fake)

    def run(writer):
        llm.force(writer)
        try:
            llm.structured("s", "u", {"type": "object", "required": ["a"]})
            gate.wait(timeout=5)      # the other thread has also finished writing its own state
            seen[writer] = llm.last_writer
        finally:
            llm.force(None)
    threads = [threading.Thread(target=run, args=(w,)) for w in ("claude:opus", "openai:gpt-5.2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert seen == {"claude:opus": "claude:opus", "openai:gpt-5.2": "openai:gpt-5.2"}


def test_module_attribute_still_reads_and_writes():
    llm.last_writer = "gemini:x"
    assert llm.last_writer == "gemini:x"
    llm.last_writer = ""
    assert llm.last_errors == [] or isinstance(llm.last_errors, list)
