"""Stage 3 — platform drafting.

One draft per platform, each constrained by config/platforms.yaml (style +
hard limits). All drafts share the same brief + angle so they tell one story
in three native voices. Instagram additionally gets an image prompt + alt text
(the actual image render lands in v1).
"""

from __future__ import annotations

import json
import os

import yaml

from . import llm

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "platforms.yaml")


def _load_platforms() -> dict:
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _schema_for(spec: dict) -> dict:
    props = {
        "caption": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
    }
    required = ["caption", "hashtags"]
    if spec.get("needs_image"):
        props["image_prompt"] = {"type": "string"}
        props["alt_text"] = {"type": "string"}
        required += ["image_prompt", "alt_text"]
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


def _system_for(spec: dict) -> str:
    limits = [f"Caption must be <= {spec['caption_max_chars']} characters."]
    if spec.get("caption_min_chars"):
        limits.append(f"Caption should be >= {spec['caption_min_chars']} characters.")
    limits.append(
        f"Use between {spec['hashtags_min']} and {spec['hashtags_max']} hashtags"
        " (in the hashtags field, without the # sign)."
    )
    if spec.get("needs_image"):
        limits.append(
            "Also provide image_prompt: a concrete visual brief for a news-style"
            " graphic (readable headline text over a fitting background), and"
            " alt_text describing it."
        )
    fmt = (spec.get("format") or "").strip()
    format_block = (
        f"\n\nFORMAT — follow this exact structure/style, it overrides the "
        f"general style above:\n{fmt}\n"
        if fmt else ""
    )
    return (
        f"You are an expert {spec['label']} writer.\n\n"
        f"Style: {spec['style'].strip()}"
        + format_block
        + "\n\nRules:\n- " + "\n- ".join(limits) + "\n\n"
        "Write the post so it lands the given key message using the chosen "
        "angle and the verified facts. Do not invent facts beyond the brief. "
        "Respect any sensitivity flags."
    )


def draft_one(platform: str, spec: dict, brief: dict, angle: dict) -> dict:
    user = (
        "ANGLE:\n" + json.dumps(angle, indent=2)
        + "\n\nBRIEF:\n" + json.dumps(brief, indent=2)
        + f"\n\nWrite the {spec['label']} post now."
    )
    return llm.structured(_system_for(spec), user, _schema_for(spec), max_tokens=3000)


def redraft(platform: str, spec: dict, brief: dict, angle: dict, issues: list[str]) -> dict:
    """Re-draft one platform with reviewer issues fed back in (auto-revise)."""
    user = (
        "ANGLE:\n" + json.dumps(angle, indent=2)
        + "\n\nBRIEF:\n" + json.dumps(brief, indent=2)
        + "\n\nYour previous draft failed review. FIX EXACTLY THESE ISSUES:\n- "
        + "\n- ".join(issues)
        + f"\n\nRewrite the {spec['label']} post, keeping everything else good."
    )
    draft = llm.structured(_system_for(spec), user, _schema_for(spec), max_tokens=3000)
    if spec.get("mentions"):
        draft["caption"] = draft["caption"].rstrip() + "\n\n" + " ".join(spec["mentions"])
    return draft


def draft_all(brief: dict, angle: dict, selected: list[str] | None = None) -> dict:
    """Draft all selected platforms in ONE call (cheaper than one call each).

    A combined schema returns a draft per platform; each platform's own style +
    limits go in the shared system prompt. Same output shape as before:
    {platform_key: draft_dict}.
    """
    platforms = _load_platforms()
    if selected:
        platforms = {k: v for k, v in platforms.items() if k in selected}

    props = {key: _schema_for(spec) for key, spec in platforms.items()}
    schema = {
        "type": "object",
        "properties": props,
        "required": list(platforms),
        "additionalProperties": False,
    }

    rule_blocks = "\n\n".join(
        f"=== {spec['label']} (key: {key}) ===\n{_system_for(spec)}"
        for key, spec in platforms.items()
    )
    system = (
        "You are an expert multi-platform social writer. Write ONE post for EACH "
        "platform below, each in its own native voice, all landing the same key "
        "message using only verified facts from the brief. Return one object with "
        f"a key per platform ({', '.join(platforms)}).\n\n" + rule_blocks
    )
    user = (
        "ANGLE:\n" + json.dumps(angle, indent=2)
        + "\n\nBRIEF:\n" + json.dumps(brief, indent=2)
        + "\n\nWrite every post now."
    )
    drafts = llm.structured(system, user, schema, max_tokens=3500)

    # Append fixed account @mentions verbatim (kept out of the LLM so handles
    # are never misspelled or invented).
    for key, spec in platforms.items():
        mentions = spec.get("mentions")
        if mentions and key in drafts:
            drafts[key]["caption"] = drafts[key]["caption"].rstrip() + "\n\n" + " ".join(mentions)
    return drafts
