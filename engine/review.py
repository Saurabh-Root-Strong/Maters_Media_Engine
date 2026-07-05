"""Stage 5 — review / safety gate (headless).

Three layers, escalating in severity:
  validate()  pure-code checks of the hard limits (chars, hashtag counts).
  critique()  Claude self-critique: facts grounded in the brief, tone,
              sensitivity, on-message. Per-platform PASS / REVISE / HOLD.
  review()    fuses both + the brief's sensitivity flags into one gate verdict
              and decides whether a human must sign off.

The interactive human approval itself lives in the CLI — this module only
produces the verdict a human (or, later, an auto-publish policy) acts on.
"""

from __future__ import annotations

import json

from . import llm
from .drafters import _load_platforms

# verdict ordering: worst wins when fusing layers
_ORDER = {"PASS": 0, "REVISE": 1, "HOLD": 2}


def _worst(*verdicts: str) -> str:
    return max(verdicts, key=lambda v: _ORDER.get(v, 1))


# --- layer 1: deterministic limits -------------------------------------------

def validate(drafts: dict, platforms: dict | None = None) -> dict:
    """Return {platform: [ {level, msg} ]}. level is 'hard' (blocks) or 'soft'."""
    platforms = platforms or _load_platforms()
    out: dict[str, list[dict]] = {}
    for key, draft in drafts.items():
        spec = platforms.get(key, {})
        issues: list[dict] = []
        caption = draft.get("caption", "") or ""
        n = len(caption)

        if not caption.strip():
            issues.append({"level": "hard", "msg": "caption is empty"})
        cap_max = spec.get("caption_max_chars")
        if cap_max and n > cap_max:
            issues.append({"level": "hard", "msg": f"caption {n} chars > limit {cap_max}"})
        cap_min = spec.get("caption_min_chars")
        if cap_min and n < cap_min:
            issues.append({"level": "soft", "msg": f"caption {n} chars < target {cap_min}"})

        tags = draft.get("hashtags", []) or []
        lo, hi = spec.get("hashtags_min"), spec.get("hashtags_max")
        if lo is not None and hi is not None and not (lo <= len(tags) <= hi):
            issues.append({"level": "hard", "msg": f"hashtags {len(tags)} outside [{lo},{hi}]"})

        out[key] = issues
    return out


def hard_violations(validation: dict) -> dict:
    """{platform: [msgs]} for hard issues only — the drivers of an auto-revise."""
    return {
        k: [i["msg"] for i in v if i["level"] == "hard"]
        for k, v in validation.items()
        if any(i["level"] == "hard" for i in v)
    }


# --- layer 2: LLM self-critique ----------------------------------------------

_CRITIQUE_SYSTEM = (
    "You are a strict editorial reviewer for social posts. For EACH platform "
    "draft, judge it on: (1) factual grounding — every claim must be supported "
    "by the brief; flag anything invented or exaggerated; (2) tone and "
    "sensitivity — respectful handling of the topic, no sensationalising; "
    "(3) on-message — lands the angle's key message. Verdicts: PASS (ship it), "
    "REVISE (a fixable problem — say what), HOLD (needs a human — risky, "
    "unverifiable, or sensitive). If the brief has sensitivity flags, lean "
    "toward HOLD. Be specific in issues; do not rewrite the post."
)

_CRITIQUE_SCHEMA = {
    "type": "object",
    "properties": {
        "overall": {
            "type": "object",
            "properties": {
                "recommendation": {"type": "string", "enum": ["PASS", "REVISE", "HOLD"]},
                "summary": {"type": "string"},
            },
            "required": ["recommendation", "summary"],
            "additionalProperties": False,
        },
        "platforms": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "platform": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["PASS", "REVISE", "HOLD"]},
                    "issues": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["platform", "verdict", "issues"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["overall", "platforms"],
    "additionalProperties": False,
}


def critique(brief: dict, angle: dict, drafts: dict) -> dict:
    user = (
        "BRIEF:\n" + json.dumps(brief, indent=2)
        + "\n\nANGLE:\n" + json.dumps(angle, indent=2)
        + "\n\nDRAFTS:\n" + json.dumps(drafts, indent=2)
        + "\n\nReview every draft."
    )
    return llm.structured(_CRITIQUE_SYSTEM, user, _CRITIQUE_SCHEMA, max_tokens=3000)


# --- fusion ------------------------------------------------------------------

def review(brief: dict, angle: dict, drafts: dict, platforms: dict | None = None) -> dict:
    """Fuse deterministic + critique + sensitivity into one gate verdict."""
    platforms = platforms or _load_platforms()
    validation = validate(drafts, platforms)
    crit = critique(brief, angle, drafts)
    crit_by_platform = {p["platform"]: p for p in crit["platforms"]}
    sensitive = bool(brief.get("sensitivity_flags"))

    gate: dict[str, dict] = {}
    for key in drafts:
        det_verdict = "REVISE" if any(
            i["level"] == "hard" for i in validation.get(key, [])
        ) else "PASS"
        llm_verdict = crit_by_platform.get(key, {}).get("verdict", "PASS")
        verdict = _worst(det_verdict, llm_verdict)
        if sensitive:
            verdict = _worst(verdict, "HOLD")
        gate[key] = {
            "verdict": verdict,
            "limit_issues": validation.get(key, []),
            "editorial_issues": crit_by_platform.get(key, {}).get("issues", []),
        }

    recommendation = _worst(*[g["verdict"] for g in gate.values()]) if gate else "PASS"
    requires_human = recommendation == "HOLD" or sensitive

    return {
        "gate": gate,
        "recommendation": recommendation,
        "requires_human": requires_human,
        "sensitive": sensitive,
        "critique_summary": crit["overall"]["summary"],
    }
