"""Publisher interface.

A publisher turns an approved manifest into the exact API call it would make.
In dry-run (the v3 default) publish() returns that call without firing. Live
sending is deliberately unwired — _send() raises — so nothing posts by accident
until each platform is explicitly implemented and its credentials are present.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod


def _post_text(draft: dict) -> str:
    """caption + hashtags, the way it should appear in the post body."""
    caption = draft.get("caption", "").strip()
    tags = " ".join(f"#{h.lstrip('#')}" for h in draft.get("hashtags", []))
    return f"{caption}\n\n{tags}".strip() if tags else caption


class Publisher(ABC):
    key: str          # matches the draft key in the manifest
    label: str
    required_env: list[str] = []

    def creds(self) -> dict | None:
        """All required env vars present -> dict of them; else None."""
        values = {k: os.environ.get(k) for k in self.required_env}
        return values if all(values.values()) else None

    def missing_env(self) -> list[str]:
        return [k for k in self.required_env if not os.environ.get(k)]

    @abstractmethod
    def build_request(self, manifest: dict) -> dict:
        """The exact call this platform would make. Auth values are redacted."""

    def _send(self, request: dict, creds: dict) -> dict:  # noqa: ARG002
        raise NotImplementedError(
            f"{self.label} live posting is not wired yet (v3 is dry-run). "
            "Implement _send against build_request()'s spec when credentials "
            "and API access are ready."
        )

    def publish(self, manifest: dict, live: bool = False) -> dict:
        draft = manifest["drafts"].get(self.key)
        if draft is None:
            return {"platform": self.key, "status": "skipped", "reason": "no draft"}

        request = self.build_request(manifest)

        if not live:
            return {"platform": self.key, "status": "dry-run", "request": request}

        creds = self.creds()
        if not creds:
            return {
                "platform": self.key,
                "status": "skipped",
                "reason": "missing credentials",
                "missing": self.missing_env(),
            }
        try:
            return {"platform": self.key, "status": "posted", **self._send(request, creds)}
        except Exception as exc:  # noqa: BLE001
            return {"platform": self.key, "status": "error", "error": str(exc)}
