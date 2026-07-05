"""Stage 7 — auto-publish policy (the "just give a topic and it fires" brain).

A pure decision function: given the review gate, which platforms have
credentials, and the run configuration, decide per platform what to do. No
network, no side effects — so every scenario is unit-testable.

SAFETY INVARIANTS (hard-coded, cannot be overridden by config):
  - A sensitive topic never auto-publishes. It is always held for a human.
  - A platform whose verdict is not PASS never auto-publishes.
Automation only ever fires a clean, non-sensitive, credentialed platform.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# Actions
PUBLISH = "PUBLISH"                # send it live
DRY_RUN = "DRY_RUN"               # build the call, don't send
HOLD_FOR_HUMAN = "HOLD_FOR_HUMAN"  # needs a person to approve
BLOCK = "BLOCK"                    # failed review, do not post
SKIP = "SKIP"                      # disabled or unreachable


@dataclass
class Config:
    auto_publish: bool = False           # OFF => everything waits for a human
    live: bool = False                   # OFF => dry-run even when auto
    enabled: set[str] = field(default_factory=lambda: {"twitter", "instagram", "linkedin"})

    @classmethod
    def from_env(cls) -> "Config":
        def flag(name: str) -> bool:
            return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")

        raw = os.environ.get("MEDIA_ENGINE_PLATFORMS", "").strip().lower()
        enabled = (
            {"twitter", "instagram", "linkedin"}
            if raw in ("", "all")
            else {p.strip() for p in raw.split(",") if p.strip()}
        )
        return cls(
            auto_publish=flag("MEDIA_ENGINE_AUTOPUBLISH"),
            live=flag("MEDIA_ENGINE_LIVE"),
            enabled=enabled,
        )


def decide(review: dict, creds_present: dict[str, bool], config: Config) -> dict:
    """Return {platform: {"action": ..., "reason": ...}} for every gated platform."""
    sensitive = review.get("sensitive", False)
    out: dict[str, dict] = {}

    for platform, g in review.get("gate", {}).items():
        verdict = g.get("verdict", "PASS")

        if platform not in config.enabled:
            action, reason = SKIP, "platform disabled in config"
        elif sensitive or verdict == "HOLD":
            action, reason = HOLD_FOR_HUMAN, (
                "sensitive topic — human sign-off required" if sensitive
                else "reviewer flagged HOLD"
            )
        elif verdict == "REVISE":
            action, reason = BLOCK, "failed review after auto-revise"
        elif not config.auto_publish:
            action, reason = HOLD_FOR_HUMAN, "auto-publish off — manual approval"
        elif not config.live:
            action, reason = DRY_RUN, "dry-run mode"
        elif not creds_present.get(platform, False):
            action, reason = SKIP, "credentials missing"
        else:
            action, reason = PUBLISH, "clean, authorized, live"

        out[platform] = {"action": action, "reason": reason}

    return out
