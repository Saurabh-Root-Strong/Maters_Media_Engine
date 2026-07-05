"""Runs the v0 pipeline: topic -> research -> angle -> 3 drafts.

No image render, no safety gate, no publishing yet (those are v1-v4). Returns
a single result dict so the CLI (or a future API) can render it however.
"""

from __future__ import annotations

import os

from . import angle as angle_stage
from . import drafters, imagegen, research, review
from .drafters import _load_platforms

_OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "output")


def _slug(topic: str) -> str:
    keep = "".join(c if c.isalnum() or c in " -_" else "" for c in topic)
    return "-".join(keep.lower().split())[:50] or "post"


def run(topic: str, platforms_selected: list[str] | None = None,
        use_search: bool | None = None, on_progress=None) -> dict:
    def note(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    note("Researching + detecting trends...")
    brief = research.research(topic, use_search=use_search)

    note("Choosing the angle...")
    angle = angle_stage.choose(brief)

    platforms = _load_platforms()

    note("Drafting selected platforms...")
    drafts = drafters.draft_all(brief, angle, platforms_selected)

    # Auto-revise: one re-draft per platform that breaks a hard limit.
    violations = review.hard_violations(review.validate(drafts, platforms))
    if violations:
        note(f"Auto-revising: {', '.join(violations)}...")
        for key, issues in violations.items():
            drafts[key] = drafters.redraft(key, platforms[key], brief, angle, issues)

    note("Reviewing (limits + facts + sensitivity)...")
    gate = review.review(brief, angle, drafts, platforms)

    result = {
        "topic": topic,
        "brief": brief,
        "angle": angle,
        "drafts": drafts,
        "review": gate,
    }

    # Stage 4 — Instagram image. Fail-soft: a render error must not lose drafts.
    if "instagram" in drafts:
        note("Rendering the Instagram image...")
        out_path = os.path.join(_OUT_DIR, f"{_slug(topic)}.png")
        try:
            result["image"] = imagegen.generate(drafts["instagram"], angle, out_path)
        except Exception as exc:  # noqa: BLE001 — keep the drafts regardless
            result["image_error"] = str(exc)

    return result
