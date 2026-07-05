"""LinkedIn publisher.

POST /v2/ugcPosts — a text share (the storytelling post). Needs an approved
LinkedIn app with the w_member_social scope; app review can take weeks, so
start that early. author is a person/org URN.
"""

from __future__ import annotations

from .base import Publisher, _post_text


class LinkedInPublisher(Publisher):
    key = "linkedin"
    label = "LinkedIn"
    required_env = ["LINKEDIN_ACCESS_TOKEN", "LINKEDIN_AUTHOR_URN"]

    def build_request(self, manifest: dict) -> dict:
        text = _post_text(manifest["drafts"][self.key])
        return {
            "method": "POST",
            "url": "https://api.linkedin.com/v2/ugcPosts",
            "auth": "OAuth 2.0 access token (Bearer), scope w_member_social",
            "headers": {
                "Authorization": "Bearer <token>",
                "Content-Type": "application/json",
                "X-Restli-Protocol-Version": "2.0.0",
            },
            "body": {
                "author": "<LINKEDIN_AUTHOR_URN, e.g. urn:li:person:xxxx>",
                "lifecycleState": "PUBLISHED",
                "specificContent": {
                    "com.linkedin.ugc.ShareContent": {
                        "shareCommentary": {"text": text},
                        "shareMediaCategory": "NONE",
                    }
                },
                "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
            },
            "notes": [
                "Needs an approved LinkedIn app (w_member_social) — review takes weeks.",
                f"Text is {len(text)} chars.",
            ],
        }
