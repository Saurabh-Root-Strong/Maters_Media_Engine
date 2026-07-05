"""Developer memory engine.

Two persistent stores:
  brand   (config/brand.yaml)   — who the user is, voice, do/don't. Injected
          into angle + drafting so every post is on-brand.
  history (output/history.json) — topics already generated, so we don't repeat
          and trending suggestions can skip covered ground.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import yaml

_BRAND_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "brand.yaml")
_HISTORY_PATH = os.path.join(os.path.dirname(__file__), "..", "output", "history.json")


# --- brand ---

def brand_profile() -> dict:
    try:
        with open(_BRAND_PATH, encoding="utf-8") as f:
            return (yaml.safe_load(f) or {}).get("brand", {}) or {}
    except OSError:
        return {}


def brand_block() -> str:
    """A prompt block describing the brand, or '' if none configured."""
    b = brand_profile()
    if not b:
        return ""
    lines = ["\n\nBRAND CONTEXT — write in this identity and voice:"]
    if b.get("name"):
        lines.append(f"Brand: {b['name']}")
    if b.get("niche"):
        lines.append(f"Niche: {b['niche']}")
    if b.get("audience"):
        lines.append(f"Audience: {b['audience']}")
    if b.get("voice"):
        lines.append(f"Voice: {str(b['voice']).strip()}")
    if b.get("do"):
        lines.append("Always: " + "; ".join(b["do"]))
    if b.get("dont"):
        lines.append("Never: " + "; ".join(b["dont"]))
    return "\n".join(lines)


# --- history ---

def recent_topics(n: int = 30) -> list[str]:
    try:
        with open(_HISTORY_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return [e["topic"] for e in data.get("entries", [])][-n:]
    except (OSError, ValueError):
        return []


def record_topic(topic: str) -> None:
    try:
        with open(_HISTORY_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {"entries": []}
    data.setdefault("entries", []).append(
        {"topic": topic, "at": datetime.now(timezone.utc).isoformat()}
    )
    data["entries"] = data["entries"][-200:]  # cap
    os.makedirs(os.path.dirname(_HISTORY_PATH), exist_ok=True)
    with open(_HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
