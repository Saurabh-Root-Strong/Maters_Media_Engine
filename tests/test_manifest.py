"""Manifest — build + write (approved/held)."""

import json
import os

from engine import manifest

RESULT = {
    "topic": "RBI rate decision",
    "brief": {"sources": ["u1", "u2"]},
    "drafts": {"twitter": {"caption": "hi", "hashtags": []}},
    "image": {"path": "output/x.png"},
    "review": {"recommendation": "PASS"},
}


def test_build_freezes_payload():
    m = manifest.build(RESULT)
    assert m["topic"] == RESULT["topic"]
    assert m["image_path"] == "output/x.png"
    assert m["sources"] == ["u1", "u2"]
    assert "approved_at" in m


def test_write_approved_and_held(tmp_path):
    p1, m1 = manifest.write(RESULT, str(tmp_path), state="approved")
    p2, m2 = manifest.write(RESULT, str(tmp_path), state="held")
    assert p1.endswith(".approved.json") and p2.endswith(".held.json")
    assert m1["state"] == "approved" and m2["state"] == "held"
    assert json.load(open(p1, encoding="utf-8"))["topic"] == RESULT["topic"]


def test_build_handles_missing_image():
    m = manifest.build({**RESULT, "image": None})
    assert m["image_path"] is None
