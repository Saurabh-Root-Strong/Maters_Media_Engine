"""Stage 1 — research / trend detection.

Two passes:
  gather()   uses the web_search tool to pull the freshest reporting on the
             topic and dumps raw notes + source URLs as text.
  distill()  turns those notes into a clean, structured brief (facts, trending
             keywords, hashtags, sentiment, sensitivity flags).

Split on purpose: gathering needs live web access; distilling is pure reasoning
over the gathered text. Keeps each call simple and debuggable.
"""

from __future__ import annotations

import os

import yaml

from . import llm

_SOURCES_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "sources.yaml")

_GATHER_BASE = (
    "You are an investigative news researcher. Given a topic, search the web for "
    "the most recent and most discussed reporting (prefer the last 24-72 hours). "
    "Find the REAL, SPECIFIC story — not a vague summary.\n"
    "Dig for concrete specifics: exactly what happened, the precise cause and "
    "mechanism, hard numbers (%, ₹, dates, counts), named people/companies/"
    "places, direct consequences, and notable quotes. If people are protesting "
    "or angry, pin down the exact reason (e.g. a specific product defect, a "
    "specific cost, a specific harm) — not a generic 'policy concern'.\n"
    "Return dense notes: the specific verified facts, the angle outlets are "
    "leading with, the words/phrases/hashtags recurring across coverage, and the "
    "sentiment. List every source URL. Do not write social posts yet."
)


def _sources_block() -> str:
    try:
        with open(_SOURCES_PATH, encoding="utf-8") as f:
            sources = (yaml.safe_load(f) or {}).get("sources", [])
    except OSError:
        return ""
    if not sources:
        return ""
    lines = "\n".join(f"- {s['name']}: {s.get('hint', '')}" for s in sources)
    return (
        "\n\nPRIORITISE these sources — run a targeted search on each (e.g. "
        "'<topic> site:economictimes.indiatimes.com'), and cross-check the story "
        "across at least THREE of them before trusting a claim:\n" + lines
        + "\n\nFALLBACK: if these sources don't have solid, recent, specific "
        "coverage of the topic, broaden to a general Google/web search and keep "
        "searching until you find concrete verifiable facts. NEVER stop at thin, "
        "vague, or generic notes — if the first searches come up weak, try "
        "different queries and more sources before returning."
    )


def _gather_system() -> str:
    return _GATHER_BASE + _sources_block()

_BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "headline_summary": {"type": "string"},
        "facts": {"type": "array", "items": {"type": "string"}},
        "trending_keywords": {"type": "array", "items": {"type": "string"}},
        "best_hashtags": {"type": "array", "items": {"type": "string"}},
        "sentiment": {"type": "string"},
        "sensitivity_flags": {"type": "array", "items": {"type": "string"}},
        "sources": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "headline_summary",
        "facts",
        "trending_keywords",
        "best_hashtags",
        "sentiment",
        "sensitivity_flags",
        "sources",
    ],
    "additionalProperties": False,
}

_DISTILL_SYSTEM = (
    "You distill raw research notes into a structured brief. Be faithful to the "
    "notes — do not invent facts or sources. facts: the load-bearing verified "
    "points. trending_keywords: the terms genuinely spiking in the coverage. "
    "best_hashtags: what you'd actually post with. sensitivity_flags: note any "
    "reason this topic needs care (death, conflict, politics, legal exposure, "
    "unverified claims) or leave empty if none."
)


def gather(topic: str, use_search: bool | None = None) -> str:
    return llm.run_with_web_search(
        _gather_system(),
        f"Topic: {topic}\n\nResearch what is trending about this now.",
        use_search=use_search,
    )


def distill(notes: str) -> dict:
    return llm.structured(
        _DISTILL_SYSTEM, f"Research notes:\n\n{notes}", _BRIEF_SCHEMA
    )


def research(topic: str, use_search: bool | None = None) -> dict:
    """Topic -> structured brief dict."""
    return distill(gather(topic, use_search))
