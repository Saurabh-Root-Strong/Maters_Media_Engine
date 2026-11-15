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

_OPENAI_MODEL = os.environ.get("MEDIA_ENGINE_OPENAI_MODEL", "gpt-4o-mini")
_ANTHROPIC_MODEL = os.environ.get("MEDIA_ENGINE_MODEL", "claude-opus-4-8")
_ANTHROPIC_EFFORT = os.environ.get("MEDIA_ENGINE_EFFORT", "high")

# Web search is the dominant per-run cost. Off -> research uses the model's own
# knowledge (much cheaper, but not live-trending). Default on.
_WEB_SEARCH = os.environ.get("MEDIA_ENGINE_WEB_SEARCH", "on").strip().lower() not in (
    "off", "false", "0", "no",
)


def web_search_enabled() -> bool:
    return _WEB_SEARCH

# One cached client per provider — a single shared slot would hand back the
# wrong client type if both providers get used in one process (e.g. tests).
_openai_client = None
_anthropic_client = None


def key_var() -> str:
    return "OPENAI_API_KEY" if PROVIDER == "openai" else "ANTHROPIC_API_KEY"


def has_api_key() -> bool:
    # Any writer in the fallback chain with a key is enough to generate.
    return bool(os.environ.get(key_var())) or bool(writers())


# =============================== OpenAI =====================================

def _openai():
    global _openai_client
    if _openai_client is None:
        from openai import OpenAI
        # max_retries covers transient 429/5xx/connection errors with backoff.
        _openai_client = OpenAI(max_retries=4, timeout=120.0)
    return _openai_client


def _openai_web(system: str, user: str, use_search: bool) -> str:
    kwargs = dict(
        model=_OPENAI_MODEL,
        input=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    if use_search:
        kwargs["tools"] = [{"type": "web_search_preview"}]
    resp = _openai().responses.create(**kwargs)
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
    content = resp.choices[0].message.content
    if not content:  # refusal / filtered — surface a clear error, not a TypeError
        refusal = getattr(resp.choices[0].message, "refusal", None)
        raise RuntimeError(f"Model returned no structured output: {refusal or 'empty response'}")
    return json.loads(content)


# ============================= Anthropic ====================================

_WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 8}


def _anthropic():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic
        _anthropic_client = anthropic.Anthropic()
    return _anthropic_client


def _anthropic_text(response) -> str:
    return "".join(b.text for b in response.content if b.type == "text").strip()


def _anthropic_web(system: str, user: str, use_search: bool, max_continuations: int = 6) -> str:
    client = _anthropic()
    kwargs = dict(
        model=_ANTHROPIC_MODEL, max_tokens=8000, system=system,
        thinking={"type": "adaptive"}, output_config={"effort": _ANTHROPIC_EFFORT},
    )
    if use_search:
        kwargs["tools"] = [_WEB_SEARCH_TOOL]
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


# ================= Claude Code CLI (the subscription, no API key) ===========
#
# The creator already pays for Claude Code. Its CLI answers headless
# (`claude -p`) using that subscription's sign-in, so the "claude" writer
# needs no ANTHROPIC_API_KEY. Tools and project settings are switched off:
# it is a plain model call, not an agent run.

# Haiku was offered as "fastest" and was neither: 233-238s a package through the
# CLI (Sonnet 64-79s) and it drifted into Devanagari on Hinglish (audit 14 Nov).
# Auto is free and faster for a quick draft, so Haiku is not offered.
_CLAUDE_MODELS = [("sonnet", "best balance"), ("opus", "strongest, slowest")]
# Default effort took ~115s a call on Sonnet (measured); a title-and-tags package
# does not need that much thinking. MEDIA_ENGINE_CLAUDE_EFFORT=medium|high to raise it.
_CLAUDE_EFFORT = os.environ.get("MEDIA_ENGINE_CLAUDE_EFFORT", "low").strip().lower()
_claude_bin: str | None | bool = False       # False = not looked up yet


def claude_cli() -> str | None:
    """Path to the Claude Code executable, or None when it is not installed."""
    global _claude_bin
    if _claude_bin is False:
        import shutil
        p = os.environ.get("MEDIA_ENGINE_CLAUDE_BIN") or shutil.which("claude")
        if p and p.lower().endswith((".ps1", ".cmd", ".bat")):
            # npm shims re-quote arguments through cmd/pwsh and mangle JSON —
            # call the real executable next to them instead.
            exe = os.path.join(os.path.dirname(p), "node_modules", "@anthropic-ai",
                               "claude-code", "bin", "claude.exe")
            p = exe if os.path.exists(exe) else None
        _claude_bin = p
    return _claude_bin


_CLAUDE_TIMEOUT = 300       # seconds for one call; a package is 2-4 calls of 15-60s each


def _run_bounded(cmd: list[str], text_in: str, limit: float):
    """subprocess.run with a limit that really ends the call.

    Plain `subprocess.run(timeout=...)` kills only the process it started. The
    CLI's own child processes keep the output pipes open, so on Windows the
    call never returned: one stuck request (the laptop slept mid-call) sat for
    hours, holding the dashboard's request with it. Kill the whole tree."""
    import subprocess
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, encoding="utf-8", errors="replace")
    try:
        out, err = p.communicate(text_in, timeout=limit)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, check=False)
        else:
            p.kill()
        try:
            p.communicate(timeout=10)
        except Exception:  # noqa: BLE001 — the pipes may never close; do not wait on them again
            pass
        raise RuntimeError(f"Claude Code did not answer within {int(limit)} seconds and was stopped. "
                           "Try again, or pick another model.") from None
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)


def _claude_run(model: str, system: str, user: str, extra: list[str]) -> dict:
    cmd = [claude_cli(), "-p", "--model", model, "--system-prompt", system,
           "--tools", "", "--setting-sources", "", "--no-session-persistence",
           "--output-format", "json", *extra]
    if _CLAUDE_EFFORT in ("low", "medium", "high"):
        cmd += ["--effort", _CLAUDE_EFFORT]
    r = _run_bounded(cmd, user, _CLAUDE_TIMEOUT)
    if r.returncode != 0 and not (r.stdout or "").lstrip().startswith("{"):
        raise RuntimeError(f"claude CLI exit {r.returncode}: {(r.stderr or r.stdout)[:200]}")
    out = json.loads(r.stdout)
    if out.get("is_error") or r.returncode != 0:
        # e.g. the plan's usage limit: the CLI says when it resets — pass that on.
        # Some failures carry no `result` at all (only a subtype and an `errors`
        # list): reporting just `result` showed the creator "Claude Code:" and nothing.
        errs = out.get("errors")
        why = " | ".join(str(x) for x in (
            out.get("result"), "; ".join(map(str, errs)) if isinstance(errs, list) else errs,
            out.get("subtype") if out.get("subtype") != "success" else None,
            f"API status {out['api_error_status']}" if out.get("api_error_status") else None,
            (r.stderr or "").strip()) if x)
        raise RuntimeError(f"Claude Code: {(why or f'failed with no reason given (exit {r.returncode})')[:300]}")
    # "opus" is an alias: record the model that really answered, so the page
    # can show it and a silent downgrade on the plan would be visible.
    used = out.get("modelUsage") or {}
    _state.last_model = max(used, key=lambda k: (used[k] or {}).get("outputTokens", 0)) if used else ""
    return out


# Not worth a second try: the plan is out of usage, or nobody is signed in.
_CLAUDE_FINAL = ("limit", "log in", "login", "logged", "auth", "credit", "subscription", "billing",
                 "did not answer")   # a 5-minute hang is not worth a second 5 minutes


def _claude_structured(model: str, system: str, user: str, schema: dict) -> dict:
    try:
        out = _claude_run(model, system, user, ["--json-schema", json.dumps(schema)])
        data = out.get("structured_output")
        return data if isinstance(data, dict) else _parse_json(out.get("result", ""), schema)
    except Exception as exc:  # noqa: BLE001
        if any(w in str(exc).lower() for w in _CLAUDE_FINAL):
            raise
        first = str(exc)
    # Second way in: the CLI's schema mode can give up (its own retry cap, or a
    # reply it could not validate) with no reason. Ask for the JSON as plain text
    # and check it here instead — a pinned model has no other writer to fall to.
    try:
        out = _claude_run(model, system + "\n\nReply with ONE JSON object and nothing else — no prose, no code "
                          "fence — matching this JSON Schema exactly:\n" + json.dumps(schema), user, [])
        return _parse_json(out.get("result", ""), schema)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"{first} | second attempt: {str(exc)[:160]}") from None


def _claude_complete(model: str, system: str, user: str) -> str:
    text = (_claude_run(model, system, user, []).get("result") or "").strip()
    if not text:
        raise RuntimeError("claude CLI returned no text")
    return text


# ===================== writers: any OpenAI-compatible API ====================
#
# Most providers speak the OpenAI chat-completions format, so one code path
# covers them all. MEDIA_ENGINE_WRITERS is an ordered fallback chain of
# "provider:model" entries, e.g.
#
#   MEDIA_ENGINE_WRITERS=gemini:gemini-3.8-flash,openai:gpt-4o-mini
#
# Each writing call tries them in order; a free tier that is rate-limited or
# down falls through to the next. Unset = the single PROVIDER above, as before.

COMPAT = {
    "openai":     {"base": None, "key": "OPENAI_API_KEY"},
    "gemini":     {"base": "https://generativelanguage.googleapis.com/v1beta/openai/",
                   "key": "GEMINI_API_KEY"},
    "openrouter": {"base": "https://openrouter.ai/api/v1", "key": "OPENROUTER_API_KEY"},
    "groq":       {"base": "https://api.groq.com/openai/v1", "key": "GROQ_API_KEY"},
    "deepseek":   {"base": "https://api.deepseek.com", "key": "DEEPSEEK_API_KEY"},
    "kimi":       {"base": "https://api.moonshot.ai/v1", "key": "MOONSHOT_API_KEY"},
    "mistral":    {"base": "https://api.mistral.ai/v1", "key": "MISTRAL_API_KEY"},
}
_compat_clients: dict[str, object] = {}
import contextvars  # noqa: E402
# The model picked in the UI for this run. Per request/thread: a plain global
# would also pin (and remove the fallback of) any other request in flight.
_forced: contextvars.ContextVar = contextvars.ContextVar("media_engine_writer", default=None)


def force(writer: str | None) -> None:
    """Pin every writing call of this run to one 'provider:model' (None resets).

    A pinned run does NOT fall back: the person chose that model, so a quiet
    swap to another one would misreport who wrote the package."""
    if not writer:
        _forced.set(None)
        return
    prov, _, model = writer.partition(":")
    _forced.set((prov.strip().lower(), model.strip()))


def choices() -> list[dict]:
    """Models the UI can offer, only those this machine can actually reach."""
    out = [{"id": "", "label": "Auto — free Gemini first, falls back down the chain"}]
    if claude_cli():
        out += [{"id": f"claude:{m}", "label": f"Claude {m.capitalize()} — {note}, your Claude Code plan"}
                for m, note in _CLAUDE_MODELS]
    if os.environ.get("GEMINI_API_KEY"):
        out += [{"id": f"gemini:{m}", "label": f"Gemini {m.split('gemini-')[-1]} — free tier"}
                for m in ("gemini-3.5-flash", "gemini-3.5-flash-lite")]
    if os.environ.get("OPENAI_API_KEY"):
        out += [{"id": f"openai:{m}", "label": f"OpenAI {m} — paid key"}
                for m in ("gpt-5.2", "gpt-5-mini", _OPENAI_MODEL)]
    seen: set[str] = set()
    return [c for c in out if not (c["id"] in seen or seen.add(c["id"]))]


def writers() -> list[tuple[str, str]]:
    """The fallback chain as [(provider, model)], entries without a key dropped."""
    if _forced.get():
        return [_forced.get()]
    out = []
    for entry in os.environ.get("MEDIA_ENGINE_WRITERS", "").split(","):
        prov, _, model = entry.strip().partition(":")
        prov = prov.strip().lower()
        if prov == "anthropic" and os.environ.get("ANTHROPIC_API_KEY"):
            out.append((prov, model.strip() or _ANTHROPIC_MODEL))
        elif prov in COMPAT and model.strip() and os.environ.get(COMPAT[prov]["key"]):
            out.append((prov, model.strip()))
    return out


def _compat(provider: str):
    if provider not in _compat_clients:
        from openai import OpenAI
        cfg = COMPAT[provider]
        paid = provider == "openai"
        # The paid default waits and retries hard. A free tier that is
        # overloaded must fail fast so the chain moves on — retrying a 503
        # cost over two minutes on one call before this was tightened.
        import httpx
        # Separate CONNECT timeout: when the network is down every attempt
        # would otherwise wait the full read timeout — measured at over ten
        # minutes for one request across the chain before this was added.
        kwargs = {"api_key": os.environ.get(cfg["key"]),
                  "timeout": httpx.Timeout(120.0 if paid else 45.0, connect=8.0),
                  "max_retries": 2 if paid else 0}
        if cfg["base"]:
            kwargs["base_url"] = cfg["base"]
        _compat_clients[provider] = OpenAI(**kwargs)
    return _compat_clients[provider]


def _parse_json(text: str, schema: dict) -> dict:
    """Parse a model's JSON reply, tolerating code fences and stray prose, and
    check the top-level required keys are there."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        data = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise RuntimeError("model did not return JSON") from None
        data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise RuntimeError("model returned JSON that is not an object")
    missing = [k for k in schema.get("required", []) if k not in data]
    if missing:
        raise RuntimeError(f"model reply is missing {', '.join(missing)}")
    return data


def _compat_structured(provider: str, model: str, system: str, user: str, schema: dict) -> dict:
    client = _compat(provider)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    try:
        resp = client.chat.completions.create(
            model=model, messages=messages,
            response_format={"type": "json_schema",
                             "json_schema": {"name": "result", "strict": True, "schema": schema}})
    except Exception as exc:  # noqa: BLE001
        # Not every model accepts a strict schema. Rate limits and auth errors
        # are not that — re-raise them so the chain moves to the next writer.
        status = getattr(exc, "status_code", None)
        if status in (401, 403, 429) or (status and status >= 500):
            raise
        if type(exc).__name__ in ("APITimeoutError", "APIConnectionError"):
            raise           # retrying a dead line doubled the wait to 90s (measured)
        messages[0]["content"] = (system + "\n\nReply with ONE JSON object and nothing else, "
                                  "matching this JSON Schema exactly:\n" + json.dumps(schema))
        resp = client.chat.completions.create(model=model, messages=messages,
                                              response_format={"type": "json_object"})
    content = resp.choices[0].message.content
    if not content:
        refusal = getattr(resp.choices[0].message, "refusal", None)
        raise RuntimeError(f"Model returned no structured output: {refusal or 'empty response'}")
    _served(resp)
    return _parse_json(content, schema)


def _served(resp) -> None:
    """Remember the model id the provider says answered (an alias such as
    'gemini-flash-latest' or 'gpt-5.2' resolves to a dated model)."""
    name = getattr(resp, "model", "")
    _state.last_model = name.split("/")[-1] if isinstance(name, str) else ""


def _compat_complete(provider: str, model: str, system: str, user: str) -> str:
    r = _compat(provider).chat.completions.create(
        model=model, messages=[{"role": "system", "content": system},
                               {"role": "user", "content": user}])
    _served(r)
    text = (r.choices[0].message.content or "").strip()
    if not text:
        # e.g. finish_reason "MALFORMED_FUNCTION_CALL": the prompt made the
        # model reach for a tool it was not given. Say so, don't just say empty.
        raise RuntimeError(f"empty response (finish_reason={r.choices[0].finish_reason!r})")
    return text


def structured_on(provider: str, model: str, system: str, user: str, schema: dict,
                  max_tokens: int = 4000) -> dict:
    """One structured call on one named writer (no fallback)."""
    if provider == "anthropic":
        return _anthropic_structured(system, user, schema, max_tokens)
    if provider == "claude":
        return _claude_structured(model, system, user, schema)
    return _compat_structured(provider, model, system, user, schema)


def _chain(call) -> object:
    """Run `call(provider, model)` down the writer chain; first success wins."""
    errors: list[str] = []
    for provider, model in writers():
        try:
            result = call(provider, model)
        except Exception as exc:  # noqa: BLE001 — any failure means "try the next writer"
            errors.append(f"{provider}:{model} — {type(exc).__name__}: {str(exc)[:160]}")
            continue
        _state.last_writer, _state.last_errors = f"{provider}:{model}", errors
        return result
    _state.last_errors = errors
    raise RuntimeError("every writer failed — " + " | ".join(errors))


# --- live search through Gemini (free quota), no OpenAI needed -----------------

def _gemini_search(system: str, user: str, model: str) -> str:
    """Research with Google Search grounding via Gemini's own API."""
    import urllib.error
    import urllib.request
    body = {"system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "tools": [{"google_search": {}}]}
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "x-goog-api-key": os.environ.get("GEMINI_API_KEY", "")})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:  # noqa: S310 — fixed https host
            data = json.loads(r.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Gemini search HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:200]}") from None
    cand = (data.get("candidates") or [{}])[0]
    text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", [])).strip()
    if not text:
        raise RuntimeError("Gemini search returned no text")
    # The source list lives in the grounding metadata, not the prose — append
    # it so the brief keeps real links.
    chunks = (cand.get("groundingMetadata") or {}).get("groundingChunks") or []
    links = [f"- {c['web'].get('title', '')}: {c['web']['uri']}" for c in chunks
             if isinstance(c.get("web"), dict) and c["web"].get("uri")]
    return text + ("\n\nSOURCES:\n" + "\n".join(dict.fromkeys(links)) if links else "")


def search_route() -> tuple[str, str] | None:
    """MEDIA_ENGINE_SEARCH=gemini:<model> routes live research through Gemini."""
    prov, _, model = os.environ.get("MEDIA_ENGINE_SEARCH", "").strip().partition(":")
    if prov.strip().lower() == "gemini" and model.strip() and os.environ.get("GEMINI_API_KEY"):
        return "gemini", model.strip()
    return None


# =============================== dispatch ===================================

def run_with_web_search(system: str, user: str, use_search: bool | None = None) -> str:
    """use_search: None -> env default (_WEB_SEARCH); True/False overrides it."""
    flag = _WEB_SEARCH if use_search is None else bool(use_search)
    route = search_route()
    if flag and route:
        try:
            return _gemini_search(system, user, route[1])
        except Exception:  # noqa: BLE001 — fall back to the default provider's search
            if not os.environ.get(key_var()):
                raise
    if not flag and writers():
        return _chain(lambda p, m: _anthropic_web(system, user, False) if p == "anthropic"
                      else _claude_complete(m, system, user) if p == "claude"
                      else _compat_complete(p, m, system, user))
    if PROVIDER == "openai":
        return _openai_web(system, user, flag)
    return _anthropic_web(system, user, flag)


def structured(system: str, user: str, schema: dict, max_tokens: int = 4000) -> dict:
    if writers():
        return _chain(lambda p, m: structured_on(p, m, system, user, schema, max_tokens))
    if PROVIDER == "openai":
        return _openai_structured(system, user, schema)
    return _anthropic_structured(system, user, schema, max_tokens)


def complete(system: str, user: str, max_tokens: int = 1500) -> str:
    """Plain text completion (no tools, no schema)."""
    if writers():
        return _chain(lambda p, m: _anthropic_text(_anthropic().messages.create(
            model=m, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}])) if p == "anthropic"
            else _claude_complete(m, system, user) if p == "claude"
            else _compat_complete(p, m, system, user))
    if PROVIDER == "openai":
        r = _openai().chat.completions.create(
            model=_OPENAI_MODEL,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
        )
        return (r.choices[0].message.content or "").strip()
    r = _anthropic().messages.create(
        model=_ANTHROPIC_MODEL, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user}],
    )
    return _anthropic_text(r)


# --- who answered: per thread ------------------------------------------------
#
# The dashboard serves each request on its own thread. As plain module globals,
# two packages written at once (two tabs) overwrote each other's "written by"
# label, and the reviewer could skip the wrong model as "the writer".
# `llm.last_writer` etc. keep working everywhere, but each thread sees its own.

import sys as _sys  # noqa: E402
import threading as _threading  # noqa: E402
import types as _types  # noqa: E402

_state = _threading.local()
_DEFAULTS = {"last_writer": "", "last_errors": [], "last_model": ""}


def _per_thread(name):
    def get(_mod):
        return getattr(_state, name, list(_DEFAULTS[name]) if isinstance(_DEFAULTS[name], list) else _DEFAULTS[name])

    def put(_mod, value):
        setattr(_state, name, value)
    return property(get, put)


class _LLMModule(_types.ModuleType):
    last_writer = _per_thread("last_writer")       # "provider:model" that answered the latest writing call
    last_errors = _per_thread("last_errors")       # why earlier entries in the chain were skipped
    last_model = _per_thread("last_model")         # exact model id the provider reported


_sys.modules[__name__].__class__ = _LLMModule
