"""Stage 6 — publish.

Reads an approved manifest and dispatches it to every platform publisher.
Dry-run by default: returns the exact request each platform would fire, without
sending. Pass live=True to attempt real posts (only platforms with creds and a
wired _send actually go out).
"""

from __future__ import annotations

from .publishers import PUBLISHERS


def run(manifest: dict, live: bool = False) -> list[dict]:
    return [p.publish(manifest, live=live) for p in PUBLISHERS]
