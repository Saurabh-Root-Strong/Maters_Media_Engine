"""Instagram publisher (Graph API).

Two-step: create a media container from a PUBLIC image URL + caption, then
publish it. The Graph API cannot read a local file, so the rendered PNG must
first be hosted at a public URL (S3, Cloudinary, etc.) — surfaced in notes.
Requires an IG Business/Creator account linked to a Facebook Page.
"""

from __future__ import annotations

from .base import Publisher, _post_text

_API = "https://graph.facebook.com/v21.0"


class InstagramPublisher(Publisher):
    key = "instagram"
    label = "Instagram"
    required_env = ["IG_ACCESS_TOKEN", "IG_BUSINESS_ACCOUNT_ID"]

    def build_request(self, manifest: dict) -> dict:
        draft = manifest["drafts"][self.key]
        caption = _post_text(draft)
        image_path = manifest.get("image_path")
        return {
            "method": "POST (x2)",
            "steps": [
                {
                    "url": f"{_API}/{{ig_business_account_id}}/media",
                    "body": {
                        "image_url": "<PUBLIC URL of the rendered card>",
                        "caption": caption,
                    },
                    "returns": "creation_id",
                },
                {
                    "url": f"{_API}/{{ig_business_account_id}}/media_publish",
                    "body": {"creation_id": "<from step 1>"},
                },
            ],
            "auth": "Long-lived Graph API access token (Bearer)",
            "local_image": image_path,
            "notes": [
                "Graph API needs a PUBLIC image_url — upload the local PNG first.",
                "Requires IG Business/Creator account linked to a FB Page.",
                f"alt_text available: {draft.get('alt_text', '')[:80]}",
            ],
        }
