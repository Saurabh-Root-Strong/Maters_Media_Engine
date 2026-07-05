"""Trending topic suggester.

Scouts the preferred sources for genuinely current, specific, postable stories
in the brand's niche, skipping anything recently covered. Returns a short list
the user can click to generate — no more guessing a topic.
"""

from __future__ import annotations

from . import llm, memory
from .research import _sources_block

_SCOUT_SYSTEM = (
    "You are a markets news scout for an Indian finance social page. Search the "
    "web RIGHT NOW for the most current, genuinely trending, SPECIFIC and "
    "postable stories (last 24-48h) in this niche. Prefer concrete stories — a "
    "named company, a number, an event, a policy move — over vague themes. "
    "Return dense notes with the story and why it matters to investors, plus "
    "source URLs."
)

_EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "topics": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},   # postable as a generation topic
                    "why": {"type": "string"},     # one line: why it matters now
                },
                "required": ["topic", "why"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["topics"],
    "additionalProperties": False,
}


def suggest(n: int = 6, use_search: bool | None = None) -> list[dict]:
    niche = (memory.brand_profile().get("niche")
             or "Indian stock markets, economy and policy")
    avoid = memory.recent_topics(20)
    avoid_line = ("\n\nSkip anything close to these already-covered topics:\n- "
                  + "\n- ".join(avoid)) if avoid else ""

    notes = llm.run_with_web_search(
        _SCOUT_SYSTEM + _sources_block(),
        f"Niche: {niche}\n\nFind the top {n} trending, specific, postable stories "
        f"right now.{avoid_line}",
        use_search=use_search,
    )
    extract_system = (
        f"From the research notes, return the {n} best distinct, specific, "
        "postable story ideas. `topic` should read like a clear generation topic "
        "(a concrete headline-style phrase), `why` one line on why it matters now."
    )
    result = llm.structured(extract_system, f"NOTES:\n\n{notes}", _EXTRACT_SCHEMA)
    return result["topics"][:n]
