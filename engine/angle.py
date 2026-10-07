"""Stage 2 — angle / hook selection.

Picks ONE narrative angle from the brief so all three platform drafts stay
coherent (same story, same hook) instead of drifting apart. Output feeds every
drafter.
"""

from __future__ import annotations

import json

from . import algo, llm, memory

_SYSTEM = (
    "You are a content strategist. Given a research brief, pick the single most "
    "engaging TRUE angle to build a social campaign around — the hook that will "
    "stop the scroll while staying honest to the facts. Choose one of: "
    "contrarian, human-story, data-shock, timeline, or explainer. Then state "
    "the one key message every post should land. If the brief has sensitivity "
    "flags, keep the angle respectful — no sensationalising tragedy."
)

_SCHEMA = {
    "type": "object",
    "properties": {
        "angle_type": {
            "type": "string",
            "enum": ["contrarian", "human-story", "data-shock", "timeline", "explainer"],
        },
        "angle": {"type": "string"},
        "key_message": {"type": "string"},
        "rationale": {"type": "string"},
    },
    "required": ["angle_type", "angle", "key_message", "rationale"],
    "additionalProperties": False,
}


def choose(brief: dict, trend_state: str | None = None) -> dict:
    """trend_state (from the trend board) shifts the angle: the take that works
    on a rising story is worthless once everyone has posted the headline."""
    return llm.structured(
        _SYSTEM + algo.trend_hint(trend_state) + memory.brand_block(),
        "Research brief:\n\n" + json.dumps(brief, indent=2), _SCHEMA,
    )
