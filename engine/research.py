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

from . import llm

_GATHER_SYSTEM = (
    "You are a news researcher. Given a topic, search the web for the most "
    "recent and most discussed reporting (prefer the last 24-72 hours). "
    "Identify what is actually trending right now and why. Return dense notes: "
    "the key verified facts, the specific angles outlets are leading with, the "
    "words/phrases/hashtags recurring across coverage, and the overall "
    "sentiment. List every source URL you used. Do not write social posts yet."
)

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


def gather(topic: str) -> str:
    return llm.run_with_web_search(
        _GATHER_SYSTEM, f"Topic: {topic}\n\nResearch what is trending about this now."
    )


def distill(notes: str) -> dict:
    return llm.structured(
        _DISTILL_SYSTEM, f"Research notes:\n\n{notes}", _BRIEF_SCHEMA
    )


def research(topic: str) -> dict:
    """Topic -> structured brief dict."""
    return distill(gather(topic))
