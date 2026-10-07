"""Lifecycle scoring — pure functions, no I/O.

The score is SHARE OF VOICE, not raw volume: what fraction of a source's total
activity in a time window belongs to this topic. Raw counts fall every night
and every weekend, which would mark the whole board "fading" at 2am; a share
does not move unless the topic really lost ground to other topics.

A topic's lifecycle state then comes from comparing its share in the most
recent window with the window before: up past the band = rising, down past it
= fading, inside it = holding steady (peaking).
"""

from __future__ import annotations

EMERGING, RISING, PEAKING, FADING = "EMERGING", "RISING", "PEAKING", "FADING"
WATCH = "WATCH"   # seen, but not enough history yet to say which way it is going
DEAD = "DEAD"     # no activity for too long — dropped from the board

DEFAULTS = {"rising_ratio": 1.3, "fading_ratio": 0.7,
            "emerging_hours": 6, "emerging_max_breadth": 5, "dead_hours": 48}

# A window is only trusted for a source when the source saw this much in it.
MIN_TOTAL = {"news": 8, "gtrends": 1, "youtube": 1000}


def usable(totals: dict[str, float]) -> set[str]:
    """Sources with enough activity in a window to compute a meaningful share."""
    return {s for s, v in totals.items() if v >= MIN_TOTAL.get(s, 1)}


def window_score(topic_vals: dict[str, float], totals: dict[str, float],
                 weights: dict[str, float], sources: set[str]) -> float | None:
    """Weighted share of voice over `sources`; None when none are usable."""
    wsum = sum(weights.get(s, 1.0) for s in sources)
    if not sources or wsum <= 0:
        return None
    return sum(weights.get(s, 1.0) * topic_vals.get(s, 0.0) / totals[s] for s in sources) / wsum


def classify(recent: float | None, prev: float | None, peak: float,
             age_hours: float, idle_hours: float, cfg: dict | None = None,
             breadth: int = 0) -> tuple[str, float | None]:
    """-> (state, velocity). velocity = recent/prev - 1, or None when unknown.

    recent / prev must be computed over the SAME set of sources, otherwise a
    source that was merely not collected last window fakes a rise.

    breadth = how many distinct publishers/channels carry it. EMERGING means
    young AND not yet everywhere — the window where posting is early. A story
    six outlets deep within hours is already mainstream: that is RISING.
    """
    c = {**DEFAULTS, **(cfg or {})}
    if idle_hours >= c["dead_hours"]:
        return DEAD, None
    if recent is None:
        return WATCH, None
    if recent <= 0:
        return FADING, (-1.0 if prev else None)
    new = age_hours <= c["emerging_hours"] and breadth <= c["emerging_max_breadth"]
    young = age_hours <= c["emerging_hours"]
    if prev is None:
        # No comparable earlier window: a young topic can only be on the way
        # up; for an older one the direction is simply not known.
        return (EMERGING if new else RISING if young else WATCH), None
    if prev <= 0:
        return (EMERGING if new else RISING), None
    ratio = recent / prev
    velocity = ratio - 1.0
    eps = 1e-9   # 0.07/0.10 is 0.7000000000000001 in floating point — exactly -30% must count
    if ratio >= c["rising_ratio"] - eps:
        return (EMERGING if new else RISING), velocity
    if ratio <= c["fading_ratio"] + eps:
        return FADING, velocity
    # Inside the band = holding steady. One rule, no exceptions: a topic down
    # 1% is not "fading", whatever its earlier high was.
    return (EMERGING if new else PEAKING), velocity
