"""Platform-algorithm awareness.

Reads config/algorithms.yaml — what each platform's ranking system is known to
reward — and applies it two ways:

  prompt_block()  the platform's writing rules, added to the drafting prompt
  fit()           a pure-code check of a finished draft against those rules,
                  giving a 0-100 "algorithm fit" score and the reasons

fit() is advisory. It never blocks a post: the review gate (engine/review.py)
decides what may publish; this only says how well a draft is shaped for reach.
"""

from __future__ import annotations

import os
import re

import yaml

_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "algorithms.yaml")

_URL = re.compile(r"(https?://|www\.)\S+|\b[\w-]+\.(com|in|org|net|co|io)\b(/\S*)?", re.I)
_MENTION = re.compile(r"(?<![\w.])@\w{2,}")
# Asking for the interaction itself rather than earning it.
_BAIT = re.compile(
    # "like if you agree" — but not "save tax if you invest" or "follow the rally if rates rise"
    r"\b(like|share|retweet|rt|repost|comment)\s+(this\s+|it\s+|now\s+|below\s+)?if\s+you\b"
    r"|\bcomment\s+(yes|no|['\"“]\w+['\"”]\s+(to|for|and\s+i))"
    r"|\btag\s+(a|your|someone|\d+)\b|\bfollow\s+(me\s+)?for\s+more\b"
    r"|\b(smash|hit)\s+(the\s+)?(like|follow)\b|\bdouble[\s-]tap\b|\blike\s+(&|and)\s+share\b"
    r"|\bshare\s+(this\s+)?with\s+\d+\b", re.I)
_CAPS_RUN = re.compile(r"\b[A-Z]{4,}\b(?:\s+\b[A-Z]{2,}\b){2,}")   # 3+ shouted words in a row

LABELS = {
    "no_link": "No link in the post", "asks_question": "Invites a reply",
    "max_hashtags": "Hashtag count", "hook_chars": "Hook fits the first line",
    "min_chars": "Enough to read", "depth_chars": "Depth for reading time",
    "line_breaks": "Easy to scan", "no_bait": "No engagement bait",
    "max_mentions": "@mention count", "no_shouting": "No shouting",
}


def playbook() -> dict:
    try:
        with open(_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except OSError:
        return {}


def prompt_block(platform: str) -> str:
    """The platform's ranking-aware writing rules, or '' if none configured."""
    p = playbook().get(platform) or {}
    rules = p.get("write_rules") or []
    if not rules:
        return ""
    head = "\n\nWRITE FOR THE RANKING ALGORITHM — these decide how far the post travels:\n"
    why = f"What it rewards: {' '.join(str(p.get('optimises_for', '')).split())}\n" if p.get("optimises_for") else ""
    return head + why + "\n".join(f"- {r}" for r in rules)


def trend_hint(state: str | None) -> str:
    """Angle guidance for where the topic is in its lifecycle, or ''."""
    text = (playbook().get("trend_angles") or {}).get(state or "")
    return f"\n\nTREND STAGE — {state}: {' '.join(str(text).split())}" if text else ""


# --- the checks ----------------------------------------------------------------

def _hook(caption: str) -> str:
    """What shows before the cut: the first non-empty line."""
    return next((ln.strip() for ln in caption.splitlines() if ln.strip()), "")


def _first_sentence(caption: str) -> str:
    return re.split(r"(?<=[.!?])\s", _hook(caption), maxsplit=1)[0]


def _run(name: str, rule: dict, caption: str, hashtags: list, platform: str) -> tuple[bool, str]:
    """-> (passed, what was measured)."""
    limit = rule.get("limit")
    if name == "no_link":
        m = _URL.search(caption)
        return (m is None, "none" if m is None else f"found “{m.group(0)[:40]}”")
    if name == "asks_question":
        # the question has to be where a reader finishes, not buried mid-post
        tail = "\n".join([ln for ln in caption.splitlines() if ln.strip()][-3:])
        tail = _MENTION.sub("", tail)
        return ("?" in tail, "ends on a question" if "?" in tail else "no question near the end")
    if name == "max_hashtags":
        n = len(hashtags) + len(re.findall(r"(?<!\w)#\w+", caption))
        return (n <= limit, f"{n} (limit {limit})")
    if name == "hook_chars":
        hook = _first_sentence(caption) if platform == "twitter" else _hook(caption)
        return (0 < len(hook) <= limit, f"{len(hook)} characters (limit {limit})")
    if name in ("min_chars", "depth_chars"):
        return (len(caption) >= limit, f"{len(caption)} characters (aim for {limit}+)")
    if name == "line_breaks":
        n = caption.count("\n")
        return (n >= limit, f"{n} line breaks (aim for {limit}+)")
    if name == "no_bait":
        m = _BAIT.search(caption)
        return (m is None, "none" if m is None else f"found “{m.group(0)}”")
    if name == "max_mentions":
        n = len(_MENTION.findall(caption))
        return (n <= limit, f"{n} (limit {limit})")
    if name == "no_shouting":
        m = _CAPS_RUN.search(caption)
        return (m is None, "none" if m is None else f"found “{m.group(0)[:40]}”")
    return (True, "")


def fit(platform: str, draft: dict, sources: list | None = None) -> dict | None:
    """Score a draft against the platform's playbook. None if no playbook."""
    p = playbook().get(platform) or {}
    rules = p.get("checks") or {}
    if not rules:
        return None
    caption = draft.get("caption") or ""
    hashtags = draft.get("hashtags") or []
    checks, earned, total = [], 0.0, 0.0
    for name, rule in rules.items():
        rule = rule or {}
        weight = float(rule.get("weight", 0))
        ok, measured = _run(name, rule, caption, hashtags, platform)
        total += weight
        earned += weight if ok else 0
        checks.append({"id": name, "label": LABELS.get(name, name), "ok": ok,
                       "measured": measured, "why": rule.get("why", ""),
                       "basis": rule.get("basis", "heuristic"), "weight": weight})
    # failures first, heaviest first — the list reads as "fix this, then this"
    checks.sort(key=lambda c: (c["ok"], -c["weight"]))
    link = next((s for s in sources or [] if isinstance(s, str) and s.startswith("http")), None)
    return {
        "score": round(100 * earned / total) if total else None,
        "checks": checks,
        "advice": p.get("advice") or [],
        "optimises_for": " ".join(str(p.get("optimises_for", "")).split()),
        # Where the link goes instead of the post body.
        "first_comment": f"Source: {link}" if p.get("first_comment") and link else None,
    }
