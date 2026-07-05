"""Auto-post runner — the top of the "give a topic, it fires" path.

Runs the full pipeline, asks the policy what to do per platform, then executes:
  PUBLISH  -> live post (guarded by creds + a wired _send)
  DRY_RUN  -> build the call, send nothing
  HOLD_*   -> not fired here; returned as held for a human to approve
  BLOCK/SKIP -> reported, not fired

Automation NEVER fires a held item. Sensitive topics are always held (enforced
in policy.decide). This function has no interactivity — the CLI handles the
human approval of held items.
"""

from __future__ import annotations

from . import manifest, orchestrator, policy
from .publishers import BY_KEY, PUBLISHERS


def _creds_map() -> dict[str, bool]:
    return {p.key: p.creds() is not None for p in PUBLISHERS}


def run(topic: str, config: policy.Config, on_progress=None) -> dict:
    result = orchestrator.run(topic, on_progress=on_progress)
    frozen = manifest.build(result, approved_by="auto-policy")
    decisions = policy.decide(result["review"], _creds_map(), config)

    outcomes: dict[str, dict] = {}
    held: list[str] = []
    for platform, d in decisions.items():
        action = d["action"]
        pub = BY_KEY.get(platform)
        if action == policy.PUBLISH and pub:
            outcomes[platform] = pub.publish(frozen, live=True)
        elif action == policy.DRY_RUN and pub:
            outcomes[platform] = pub.publish(frozen, live=False)
        else:
            outcomes[platform] = {"platform": platform, "status": action.lower(),
                                  "reason": d["reason"]}
            if action == policy.HOLD_FOR_HUMAN:
                held.append(platform)

    return {
        "result": result,
        "frozen": frozen,
        "decisions": decisions,
        "outcomes": outcomes,
        "held": held,
    }
