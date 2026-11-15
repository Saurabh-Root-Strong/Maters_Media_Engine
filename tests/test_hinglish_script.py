"""Hinglish is Roman script in the copy; Hindi spellings only in tags."""

from engine import content


def _pkg(desc, tags=()):
    return {"titles": [{"text": "Bro, paisa kamao 💸"}], "description": desc,
            "pinned_comment": "Tumhara sukoon kya hai?", "tags": list(tags), "hashtags": ["Paisa"]}


def _script_issue(issues):
    return [i for i in issues if "Roman letters" in i]


def test_devanagari_in_a_hinglish_description_is_flagged():
    issues = content.lint(_pkg("ब्रो, पैसा कमाओ। Asli motivation 💸"), "paisa motivation", "", None, "hinglish")
    assert _script_issue(issues)


def test_hindi_spellings_in_tags_are_fine():
    issues = content.lint(_pkg("Bro, paisa kamao 💸", ["paisa motivation", "पैसा मोटिवेशन"]),
                          "paisa motivation", "", None, "hinglish")
    assert not _script_issue(issues)


def test_hindi_packages_may_use_devanagari():
    issues = content.lint(_pkg("ब्रो, पैसा कमाओ। 💸"), "paisa motivation", "", None, "hindi")
    assert not _script_issue(issues)
