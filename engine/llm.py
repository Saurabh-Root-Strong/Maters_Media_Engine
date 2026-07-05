"""LLM seam — provider-agnostic.

The whole pipeline calls just two functions:
  run_with_web_search(system, user) -> str   (news gathering)
  structured(system, user, schema)  -> dict  (brief / angle / drafts / etc.)

Provider is chosen by MEDIA_ENGINE_PROVIDER, else auto-detected from whichever
API key is present (OpenAI preferred). Swap providers with a key + env var;
nothing else in the codebase changes.
"""

from __future__ import annotations

import json
import os


def _detect_provider() -> str:
    p = os.environ.get("MEDIA_ENGINE_PROVIDER", "").strip().lower()
    if p in ("openai", "anthropic"):
        return p
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return "openai"


PROVIDER = _detect_provider()

_OPENAI_MODEL = os.environ.get("MEDIA_ENGINE_OPENAI_MODEL", "gpt-5.4-mini")
_ANTHROPIC_MODEL = os.environ.get("MEDIA_ENGINE_MODEL", "claude-opus-4-8")
_ANTHROPIC_EFFORT = os.environ.get("MEDIA_ENGINE_EFFORT", "high")

_client = None


def key_var() -> str:
    return "OPENAI_API_KEY" if PROVIDER == "openai" else "ANTHROPIC_API_KEY"


def has_api_key() -> bool:
    return bool(os.environ.get(key_var()))


# =============================== OpenAI =====================================

def _openai():
    global _client
    if _client is None:
        from openai import OpenAI
        # max_retries covers transient 429/5xx/connection errors with backoff.
        _client = OpenAI(max_retries=4, timeout=120.0)
    return _client


def _openai_web(system: str, user: str) -> str:
    resp = _openai().responses.create(
        model=_OPENAI_MODEL,
        tools=[{"type": "web_search_preview"}],
        input=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return (resp.output_text or "").strip()


def _openai_structured(system: str, user: str, schema: dict) -> dict:
    resp = _openai().chat.completions.create(
        model=_OPENAI_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "result", "strict": True, "schema": schema},
        },
    )
    return json.loads(resp.choices[0].message.content)


# ============================= Anthropic ====================================

_WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 8}


def _anthropic():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic()
    return _client


def _anthropic_text(response) -> str:
    return "".join(b.text for b in response.content if b.type == "text").strip()


def _anthropic_web(system: str, user: str, max_continuations: int = 6) -> str:
    client = _anthropic()
    kwargs = dict(
        model=_ANTHROPIC_MODEL, max_tokens=8000, system=system,
        thinking={"type": "adaptive"}, output_config={"effort": _ANTHROPIC_EFFORT},
        tools=[_WEB_SEARCH_TOOL],
    )
    resp = client.messages.create(messages=[{"role": "user", "content": user}], **kwargs)
    cont = 0
    while resp.stop_reason == "pause_turn" and cont < max_continuations:
        resp = client.messages.create(
            messages=[
                {"role": "user", "content": user},
                {"role": "assistant", "content": resp.content},
            ],
            **kwargs,
        )
        cont += 1
    return _anthropic_text(resp)


def _anthropic_structured(system: str, user: str, schema: dict, max_tokens: int) -> dict:
    resp = _anthropic().messages.create(
        model=_ANTHROPIC_MODEL, max_tokens=max_tokens, system=system,
        thinking={"type": "adaptive"},
        output_config={"effort": _ANTHROPIC_EFFORT,
                       "format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": user}],
    )
    return json.loads(_anthropic_text(resp))


# =============================== dispatch ===================================

def run_with_web_search(system: str, user: str) -> str:
    if PROVIDER == "openai":
        return _openai_web(system, user)
    return _anthropic_web(system, user)


def structured(system: str, user: str, schema: dict, max_tokens: int = 4000) -> dict:
    if PROVIDER == "openai":
        return _openai_structured(system, user, schema)
    return _anthropic_structured(system, user, schema, max_tokens)
