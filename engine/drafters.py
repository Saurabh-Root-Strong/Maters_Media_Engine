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

from . import llm, memory

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "platforms.yaml")


def _load_platforms() -> dict:
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _formats(spec: dict) -> list[dict]:
    """Template options for a platform. Supports the new `formats` list and the
    legacy single `format` string."""
    fmts = spec.get("formats")
    if isinstance(fmts, list) and fmts:
        return fmts
    if spec.get("format"):
        return [{"id": "default", "name": "Default", "template": spec["format"]}]
    return []


def _format_text(spec: dict, fmt_id: str | None) -> str:
    """The chosen template's text (default = first) or '' if none."""
    fmts = _formats(spec)
    if not fmts:
        return ""
    chosen = next((f for f in fmts if f.get("id") == fmt_id), fmts[0])
    return (chosen.get("template") or "").strip()


def platform_templates() -> dict:
    """{platform: [{id, name}]} for the dashboard pickers."""
    return {
        key: [{"id": f.get("id"), "name": f.get("name", f.get("id"))} for f in _formats(spec)]
        for key, spec in _load_platforms().items()
    }


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


def _system_for(spec: dict, fmt_id: str | None = None) -> str:
    limits = [
        f"Caption PLUS the hashtags (which get appended as '#tag #tag') must "
        f"together fit within {spec['caption_max_chars']} characters — leave "
        f"room for the hashtags."
    ]
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
    fmt = _format_text(spec, fmt_id)
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


def draft_one(platform: str, spec: dict, brief: dict, angle: dict,
              fmt_id: str | None = None) -> dict:
    user = (
        "ANGLE:\n" + json.dumps(angle, indent=2)
        + "\n\nBRIEF:\n" + json.dumps(brief, indent=2)
        + f"\n\nWrite the {spec['label']} post now."
    )
    return llm.structured(_system_for(spec, fmt_id), user, _schema_for(spec), max_tokens=3000)


def redraft(platform: str, spec: dict, brief: dict, angle: dict, issues: list[str],
            fmt_id: str | None = None) -> dict:
    """Re-draft one platform with reviewer issues fed back in (auto-revise)."""
    user = (
        "ANGLE:\n" + json.dumps(angle, indent=2)
        + "\n\nBRIEF:\n" + json.dumps(brief, indent=2)
        + "\n\nYour previous draft failed review. FIX EXACTLY THESE ISSUES:\n- "
        + "\n- ".join(issues)
        + f"\n\nRewrite the {spec['label']} post, keeping everything else good."
    )
    draft = llm.structured(_system_for(spec, fmt_id), user, _schema_for(spec), max_tokens=3000)
    _apply_fixed(draft, spec)
    return draft


def draft_all(brief: dict, angle: dict, selected: list[str] | None = None,
              formats_map: dict | None = None) -> dict:
    """Draft all selected platforms in ONE call (cheaper than one call each).

    A combined schema returns a draft per platform; each platform's own style +
    limits go in the shared system prompt. Same output shape as before:
    {platform_key: draft_dict}.
    """
    platforms = _load_platforms()
    if selected:
        platforms = {k: v for k, v in platforms.items() if k in selected}
    formats_map = formats_map or {}

    props = {key: _schema_for(spec) for key, spec in platforms.items()}
    schema = {
        "type": "object",
        "properties": props,
        "required": list(platforms),
        "additionalProperties": False,
    }

    rule_blocks = "\n\n".join(
        f"=== {spec['label']} (key: {key}) ===\n{_system_for(spec, formats_map.get(key))}"
        for key, spec in platforms.items()
    )
    system = (
        "You are an expert multi-platform social writer. Write ONE post for EACH "
        "platform below, each in its own native voice, all landing the same key "
        "message using only verified facts from the brief. Return one object with "
        f"a key per platform ({', '.join(platforms)}).\n\n" + rule_blocks
        + memory.brand_block()
    )
    user = (
        "ANGLE:\n" + json.dumps(angle, indent=2)
        + "\n\nBRIEF:\n" + json.dumps(brief, indent=2)
        + "\n\nWrite every post now."
    )
    drafts = llm.structured(system, user, schema, max_tokens=3500)
    for key, spec in platforms.items():
        if key in drafts:
            _apply_fixed(drafts[key], spec)
    return drafts


def _apply_fixed(draft: dict, spec: dict) -> None:
    """Apply deterministic, config-driven bits the LLM shouldn't own:
    fixed @mentions (never misspelled) and always-on brand hashtags."""
    mentions = spec.get("mentions")
    if mentions:
        draft["caption"] = draft["caption"].rstrip() + "\n\n" + " ".join(mentions)

    always = spec.get("always_hashtags")
    if always:
        forced = [h.lstrip("#") for h in always]
        lower = {f.lower() for f in forced}
        existing = [h.lstrip("#") for h in draft.get("hashtags", []) if h.lstrip("#").lower() not in lower]
        merged = forced + existing
        cap = spec.get("hashtags_max")
        draft["hashtags"] = merged[:cap] if cap else merged
