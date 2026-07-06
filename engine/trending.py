"""Trending topic suggester.

Scouts the preferred sources for genuinely current, specific, postable stories
in the brand's niche, skipping anything recently covered. Returns a short list
the user can click to generate — no more guessing a topic.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import llm, memory
from .research import _sources_block

MAX_AGE_DAYS = 10  # hard freshness cutoff for trending suggestions

_IST = timezone(timedelta(hours=5, minutes=30))


def _today() -> datetime:
    return datetime.now(_IST)


def _scout_system() -> str:
    today = _today()
    cutoff = today - timedelta(days=MAX_AGE_DAYS)
    return (
        f"TODAY'S DATE IS {today:%d %B %Y} (IST). Use this as your anchor for "
        "what counts as recent — do not trust your memory of 'current' events.\n"
        "You are a markets news scout for an Indian finance social page. Search "
        "the web RIGHT NOW for the most current, genuinely trending, SPECIFIC "
        "and postable stories. STRICT freshness rule: only stories PUBLISHED "
        f"after {cutoff:%d %B %Y} (last {MAX_AGE_DAYS} days) — strongly prefer "
        "the last 24-72 hours. For EVERY story, note its publication date from "
        "the source; if you cannot confirm it is recent, DISCARD it. Prefer "
        "concrete stories — a named company, a number, an event, a policy move — "
        "over vague themes or evergreen explainers. Return dense notes with each "
        "story, its publication date, why it matters to investors, and source "
        "URLs."
    )

# Trending categories the user can pick in the dashboard. `focus` steers the
# scout; `n` = how many suggestions to return.
CATEGORIES = {
    "all": {
        "name": "🔥 All (my niche)",
        "focus": None,  # falls back to the brand niche
        "n": 6,
    },
    "geopolitics": {
        "name": "🌍 World geopolitics",
        "focus": "World geopolitics and global events that move markets — wars, "
                 "sanctions, oil/shipping routes, trade deals, elections, "
                 "central-bank moves — with the India/market impact spelled out",
        "n": 6,
    },
    "fraud": {
        "name": "🕵️ Indian stocks fraud / corporate news",
        "focus": "Indian corporate fraud, scams, SEBI/regulator probes, "
                 "accounting scandals, promoter issues, big corporate "
                 "governance news and stock crashes",
        "n": 6,
    },
    "twitter": {
        "name": "🐦 Latest trends on X/Twitter",
        "focus": "What is trending RIGHT NOW on X/Twitter in India around "
                 "finance, markets, business and economy — viral discussions, "
                 "trending hashtags and the stories behind them",
        "n": 6,
    },
    "stocks": {
        "name": "📈 Stock market news (top 10)",
        "focus": "Top Indian stock market stories right now — Sensex/Nifty "
                 "moves, big gainers/losers, results, deals, FII/DII flows, "
                 "sector rallies, IPOs",
        "n": 10,
    },
}

_EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "topics": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},   # postable as a generation topic
                    "why": {"type": "string"},     # one line: why it matters now
                    "date": {"type": "string"},    # publication date, YYYY-MM-DD
                },
                "required": ["topic", "why", "date"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["topics"],
    "additionalProperties": False,
}


def _fresh_enough(date_str: str) -> bool:
    """Code-level enforcement of the age cutoff. Unparseable date -> drop."""
    try:
        d = datetime.strptime(date_str.strip()[:10], "%Y-%m-%d").replace(tzinfo=_IST)
    except ValueError:
        return False
    return (_today() - d).days <= MAX_AGE_DAYS


def suggest(n: int | None = None, use_search: bool = True,
            category: str = "all") -> list[dict]:
    # "Trending" is meaningless without live data — a model-knowledge answer
    # would present stale guesses as current. Search is always forced on.
    use_search = True
    cat = CATEGORIES.get(category, CATEGORIES["all"])
    n = n or cat["n"]
    niche = (cat["focus"]
             or memory.brand_profile().get("niche")
             or "Indian stock markets, economy and policy")
    avoid = memory.recent_topics(20)
    avoid_line = ("\n\nSkip anything close to these already-covered topics:\n- "
                  + "\n- ".join(avoid)) if avoid else ""

    notes = llm.run_with_web_search(
        _scout_system() + _sources_block(),
        f"Niche: {niche}\n\nFind the top {n} trending, specific, postable stories "
        f"right now.{avoid_line}",
        use_search=use_search,
    )
    today = _today()
    extract_system = (
        f"Today is {today:%Y-%m-%d}. From the research notes, return the {n} "
        "best distinct, specific, postable story ideas. `topic` = a concrete "
        "headline-style generation topic; `why` = one line on why it matters "
        "now; `date` = the story's publication date as YYYY-MM-DD taken from "
        f"the notes. EXCLUDE anything older than {MAX_AGE_DAYS} days or whose "
        "date you cannot determine from the notes."
    )
    result = llm.structured(extract_system, f"NOTES:\n\n{notes}", _EXTRACT_SCHEMA)
    # Belt and braces: enforce the cutoff in code too.
    fresh = [t for t in result["topics"] if _fresh_enough(t.get("date", ""))]
    return fresh[:n]
