"""Approved-post manifest — the handoff from the gate to publishing.

When a human (or, later, an auto-publish policy) approves a run, we freeze
exactly what will ship into one JSON file. v3 publishers read this; they never
re-run the pipeline, so what you approved is what posts.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone


def _slug(topic: str) -> str:
    keep = "".join(c if c.isalnum() or c in " -_" else "" for c in topic)
    return "-".join(keep.lower().split())[:50] or "post"


def build(result: dict, approved_by: str = "human") -> dict:
    """The frozen payload — exactly what will publish."""
    return {
        "topic": result["topic"],
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "approved_by": approved_by,
        "recommendation": result["review"]["recommendation"],
        "drafts": result["drafts"],
        "image_path": (result.get("image") or {}).get("path"),
        "sources": result["brief"].get("sources", []),
    }


def write(result: dict, out_dir: str, approved_by: str = "human",
          state: str = "approved") -> tuple[str, dict]:
    """state: 'approved' (ready to publish) or 'held' (awaiting human review)."""
    manifest = build(result, approved_by)
    manifest["state"] = state
    path = os.path.join(out_dir, f"{_slug(result['topic'])}.{state}.json")
    os.makedirs(out_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    return path, manifest
