"""Twitter/X publisher.

POST /2/tweets — a single text tweet. Easiest to wire; needs the paid API tier
for write access and OAuth 1.0a user-context (or OAuth 2.0 user token) signing.
"""

from __future__ import annotations

from .base import Publisher, _post_text


class TwitterPublisher(Publisher):
    key = "twitter"
    label = "Twitter/X"
    required_env = ["X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET"]

    def build_request(self, manifest: dict) -> dict:
        text = _post_text(manifest["drafts"][self.key])
        return {
            "method": "POST",
            "url": "https://api.twitter.com/2/tweets",
            "auth": "OAuth 1.0a user context (API key/secret + access token/secret)",
            "headers": {"Content-Type": "application/json"},
            "body": {"text": text},
            "notes": [
                "Write access requires a paid X API tier (~$100/mo Basic).",
                f"Text is {len(text)} chars (limit 280 — enforced by the gate).",
            ],
        }
