"""Content generator — packaging for a video you have already made.

Pick a platform, say what the video is about, and get the title / description
/ tags / hashtags that platform takes, written from evidence rather than
guesswork:

  1. insight.analyse()  pulls what the trend store knows about the subject —
     its live state, which competing titles are getting views, what people are
     searching, how it has moved over past days. Free, no LLM.
  2. one LLM call writes the package for the chosen platform from that
     evidence, the platform's rules (config/content_formats.yaml) and the brand.
  3. code enforces the platform's hard limits on the result and reports
     anything it had to trim or could not fix.
"""

from __future__ import annotations

import json
import os
import re
import time
from contextlib import closing

import yaml

from . import algo, analytics, llm, memory
from .trends import insight, lookup
from .trends.text import tokens as _tokens

_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "content_formats.yaml")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS content_packages (
    id INTEGER PRIMARY KEY,
    created_at REAL NOT NULL,
    platform TEXT NOT NULL,
    topic TEXT NOT NULL,
    trend_state TEXT,
    package TEXT NOT NULL
);
"""


def formats() -> dict:
    with open(_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def platforms() -> list[dict]:
    """[{id, label}] for the picker."""
    return [{"id": k, "label": v.get("label", k)} for k, v in formats().items()
            if isinstance(v, dict) and "fields" in v]


def languages() -> list[str]:
    return list((formats().get("languages") or {}).keys())


# --- what the trend stage means for packaging ----------------------------------

_SHARE_TYPES = {"mood", "humour"}

# How to package each kind of content. Search-led copy on a mood reel, or a
# feelings caption on a results explainer, both miss what the viewer came for.
_PLAYBOOK = {
    "mood": "MOOD / EMOTIONAL. It spreads by people sending it, not by search. Lead with its core line or the "
            "feeling, in the viewer's own words. Make someone think 'this is about me' or 'I must send this to "
            "him'. Promise nothing it does not give: no 'learn', 'how to', 'tips', 'guide', and no title posing a "
            "debate or claim the video does not take up. Do not explain it. Short. End on a question people can "
            "answer from their own life; never ask them to tag, share or like. Search words only where natural.",
    "humour": "HUMOUR. It spreads by people sending it. Set up the joke, never explain it. Short. No lessons.",
    "news_explainer": "NEWS EXPLAINER. It wins on speed and clarity. Lead with the specific event and number, then "
                      "the so-what. Use the search phrase people type today.",
    "education": "EDUCATION. It wins on search and saves. Lead with the exact question or outcome. Say what the viewer "
                 "will be able to do. Worth saving for later.",
    "stock_analysis": "STOCK ANALYSIS. It wins on a specific, checkable view, and carries regulatory risk. Name the "
                      "subject. Never repeat buy/sell calls, targets or stop-losses in the copy; frame it as "
                      "analysis and add a short educational-only line.",
    "story": "STORY. It wins on tension. Open on the moment things turned, not the background. Do not give the ending "
             "away in the title.",
    "interview": "INTERVIEW. It wins on the guest's single strongest claim, attributed to them by name.",
    "promo": "PROMOTION. Say plainly what is being offered and why it matters now. Disclose any paid relationship.",
}

_LOOKUP_READ = {
    "TRENDING": ("news", "Interest in this subject is rising right now. Publish soon and lead with the "
                         "wording people are searching today."),
    "STEADY": ("evergreen", "Steady, evergreen interest — people look this up every day, news or no news. "
                            "Package it for search: the title should answer the question they type."),
    "FADING": ("search", "Interest is falling from a recent high. Skip the news angle — frame it as the "
                         "lesson, the explainer or the what-happens-next so it keeps earning from search."),
    "DORMANT": ("search", "Very little current interest in this subject on its own. It needs a hook into "
                          "something people care about now — a comparison, an anniversary, a current event."),
}


def strategy(evidence: dict) -> dict:
    """Plain-language read of the evidence. Deterministic — the same evidence
    always gives the same advice."""
    now = evidence.get("now")
    look = evidence.get("lookup") or {}
    verdict = look.get("verdict") or {}
    notes = []
    state = None
    if evidence.get("searchable") is False:
        return {"mode": "search", "notes": [], "state": None,
                "headline": "That topic has no words to look up (only symbols or filler words). "
                            "Name the subject — a company, an event, a number."}
    if evidence.get("content_type") in _SHARE_TYPES and not now:
        # Trend lines measure a topic. A feeling spreads through people sending
        # it, and "low search interest" says nothing about whether it will.
        state, mode = "EVERGREEN", "share"
        headline = ("A feeling, not a news topic: it does not depend on the news cycle and can be posted any "
                    "time. It spreads when people send it and save it, so the opening line has to make someone "
                    "think of one specific person.")
    elif now:
        # On the live board of the tracked niche: the most precise read there is.
        state = now["state"]
        v = now["velocity_pct"]
        change = "" if v is None else f" ({'+' if v > 0 else ''}{v}% on the window before)"
        if state in ("EMERGING", "RISING"):
            headline = (f"{state.capitalize()} now{change}. Publish as soon as you can and lead the "
                        "title with the phrase people are searching today.")
            mode = "news"
        elif state == "PEAKING":
            headline = (f"Holding steady{change} — widely covered already. The plain headline is "
                        "taken; the title needs an angle others have not used.")
            mode = "angle"
        else:
            headline = (f"Fading{change}. The news window has passed — title it as an explainer or "
                        "a what-happens-next so it keeps earning from search.")
            mode = "search"
    elif verdict.get("state"):
        # Not on the tracked board: read the subject's own lines, looked up live.
        state = verdict["state"]
        mode, headline = _LOOKUP_READ[state]
    elif not evidence.get("matched"):
        headline = ("Nothing found on this subject in the tracked sources, and the live lookup returned "
                    "nothing either — package it for search.")
        mode = "search"
    else:
        headline = (f"{evidence['matched']} related items found, but the subject is not on the live "
                    "trend board — treat it as a search / evergreen video.")
        mode = "search"
    notes += verdict.get("reasons") or []
    yt = look.get("youtube") or {}
    if yt.get("videos"):
        top = yt["videos"][0]
        notes.append(f"Across YouTube in the last 30 days: {yt['found']} videos on it; the biggest is "
                     f"“{top['title']}” ({top['channel']}) with {top['views']:,} views in {top['age_days']:g} days.")
        if yt.get("shorts_share_top", 0) >= 0.5:
            notes.append(f"{round(yt['shorts_share_top'] * 100)}% of the top videos are Shorts — the "
                         "audience for this subject is watching short-form.")
        elif yt.get("top_kind_mix", {}).get("long", 0) >= 6:
            notes.append("The top videos are mostly long-form — depth is being rewarded here.")
        if yt.get("hindi_title_share", 0) >= 0.4:
            notes.append(f"{round(yt['hindi_title_share'] * 100)}% of the top titles are in Hindi script — "
                         "consider a Hindi or Hinglish title.")
        if yt.get("uploads_last_7d", 0) >= 10:
            notes.append(f"{yt['uploads_last_7d']} of them were uploaded in the last 7 days — crowded; "
                         "a distinct angle matters more than speed.")
    elif look.get("youtube_note"):
        notes.append(look["youtube_note"])
    vids = evidence.get("videos") or []
    if vids:
        top = vids[0]
        who = "creator" if top.get("creator") else "channel"
        beat = (f", {top['outlier']}× that channel's usual pace" if top.get("outlier") else "")
        notes.append(f"Leading {who} video on it: “{top['title']}” ({top['channel']}) — about "
                     f"{top['views_per_hour']:,} views/hour{beat}.")
        creators = [v for v in vids if v.get("creator")]
        if not creators:
            notes.append("None of the preferred creators has covered it yet — room to be first.")
        if len(vids) >= 3:
            avg = round(sum(v["title_chars"] for v in vids[:3]) / 3)
            notes.append(f"The top three titles average {avg} characters.")
    if evidence.get("searches"):
        notes.append("Live search suggestions show the exact wording viewers type — the strongest "
                     "source for the title's first words.")
    days = evidence.get("days_of_history") or 0
    if days < 7 and not verdict.get("state"):
        notes.append(f"Only {days:g} day(s) of trend history collected so far, so “past trends” is "
                     "thin. It fills in as the tracker keeps running.")
    return {"mode": mode, "headline": headline, "notes": notes, "state": state}


# --- prompt + schema -----------------------------------------------------------

def _schema(fields: dict) -> dict:
    props: dict = {}
    text = {"type": "string"}
    if "titles" in fields:
        props["titles"] = {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["text", "why"],
            "properties": {"text": text, "why": text}}}
    if "description" in fields:
        props["description"] = text
    if "tags" in fields:
        props["tags"] = {"type": "array", "items": text}
    if "hashtags" in fields:
        props["hashtags"] = {"type": "array", "items": text}
    for key in ("thumbnail_text", "cover_text"):
        if key in fields:
            props[key] = {"type": "array", "items": text}
    if "pinned_comment" in fields:
        props["pinned_comment"] = text
    if "hooks" in fields:
        props["hooks"] = {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["text", "why"],
            "properties": {"text": text, "why": text}}}
    return {"type": "object", "properties": props, "required": list(props),
            "additionalProperties": False}


def _field_brief(fields: dict) -> str:
    lines = []
    for name, f in fields.items():
        f = f or {}
        label = f.get("name") or name.replace("_", " ")
        if name == "titles":
            lines.append(f"- titles: {f.get('n', 3)} different options, each at most {f['max']} "
                         f"characters and ideally under {f.get('show', f['max'])}. For each give `why` "
                         "— one line naming the evidence it is built on.")
        elif name == "description":
            lines.append(f"- description ({label}): at most {f['max']} characters; the first "
                         f"{f.get('hook', 125)} characters must work alone.")
        elif name == "tags":
            lines.append(f"- tags: about {f.get('aim', 10)} tags, each under {f.get('max_each', 30)} "
                         f"characters, {f['max_total']} characters in total. No # sign.")
        elif name == "hashtags":
            lines.append(f"- hashtags: between {f.get('min', 0)} and {f['max']}, without the # sign, "
                         "no spaces.")
        elif name in ("thumbnail_text", "cover_text"):
            lines.append(f"- {name}: {f.get('n', 3)} options of at most {f.get('max_words', 4)} words.")
        elif name == "pinned_comment":
            lines.append(f"- pinned_comment: at most {f['max']} characters.")
        elif name == "hooks":
            lines.append(f"- hooks: {f.get('n', 3)} ALTERNATIVE opening lines for the same post, each at most "
                         f"{f.get('max', 110)} characters and each a different way in — (1) a specific number "
                         "or fact, (2) a question the viewer already has, (3) a contrarian or surprising "
                         "claim the video backs up. For each give `why`: which evidence it is built on.")
    return "\n".join(lines)


def _evidence_block(ev: dict, strat: dict) -> str:
    parts = [f"TREND READ: {strat['headline']}"]
    if ev.get("now"):
        parts.append("LIVE SIGNALS: " + " | ".join(ev["now"]["signals"]))
    if ev.get("searches"):
        parts.append(f"WHAT PEOPLE TYPE ({ev.get('suggest_feed', 'search')} suggestions, in order):\n- "
                     + "\n- ".join(ev["searches"]))
    if ev.get("trending_searches"):
        parts.append("TRENDING ON GOOGLE INDIA: " + ", ".join(t["query"] for t in ev["trending_searches"]))
    if ev.get("videos"):
        parts.append("COMPETING VIDEOS, preferred creators first, then by how far each beats its "
                     "channel's normal pace:\n" + "\n".join(
            f"- {v['views_per_hour']:,}/h"
            + (f", {v['outlier']}x its channel's usual" if v.get("outlier") else "")
            + f" — “{v['title']}” ({v['channel']}{', creator' if v.get('creator') else ''})" for v in ev["videos"]))
    if ev.get("headlines"):
        parts.append("RECENT HEADLINES:\n" + "\n".join(f"- {h['title']}" for h in ev["headlines"][:6]))
    if ev.get("phrases"):
        parts.append("WORD PAIRS USED MOST: " + ", ".join(p["phrase"] for p in ev["phrases"]))
    look = ev.get("lookup") or {}
    if (look.get("verdict") or {}).get("reasons"):
        parts.append("THIS SUBJECT'S OWN TREND LINES:\n- " + "\n- ".join(look["verdict"]["reasons"]))
    yt = look.get("youtube") or {}
    if yt.get("videos"):
        parts.append("TOP VIDEOS ON THIS SUBJECT ACROSS ALL OF YOUTUBE (last 30 days, by views):\n" + "\n".join(
            f"- {v['views']:,} views in {v['age_days']:g}d, {v['kind']} — “{v['title']}” ({v['channel']})"
            for v in yt["videos"]))
        if yt.get("phrases"):
            parts.append("WORD PAIRS IN THOSE TITLES: " + ", ".join(p["phrase"] for p in yt["phrases"]))
    news = look.get("news") or {}
    if news.get("headlines") and not ev.get("headlines"):
        parts.append("RECENT HEADLINES ON IT:\n" + "\n".join(f"- {h['title']}" for h in news["headlines"][:6]))
    return "\n\n".join(parts)


_PLAN_SYSTEM = (
    "You are a senior content strategist for {platform}, planning how to package ONE finished "
    "video for an Indian audience. You are given live evidence: what people type into search, "
    "which videos on this subject are getting views, and whether interest is rising, steady or "
    "falling. Decide the plan — do not write the copy yet.\n"
    "- audience_intent: in one sentence, who searches for this and what they want to walk away with.\n"
    "- angle: the single way in that the evidence shows is open — name the winning videos' "
    "pattern and how this video differs. Not a summary of the subject.\n"
    "- promise: the one specific thing the viewer gets, stated so the video can actually deliver it "
    "(only from what the creator says the video covers).\n"
    "- primary_keyword: the exact search phrase to lead with, taken from the suggestions or top titles.\n"
    "- secondary_keywords: 3 to 6 further real phrases from the evidence.\n"
    "- specific_hashtags: 2 to 4 hashtags that NAME this subject (people, company, event) — never "
    "only broad ones like StockMarket or Investing.\n"
    "- avoid: one line on the trap to stay out of (a crowded angle, an over-claim, a wrong-meaning "
    "search phrase).\n"
    "Use only the evidence and the creator's notes. Never invent facts or numbers."
)

_PLAN_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "audience_intent": {"type": "string"}, "angle": {"type": "string"}, "promise": {"type": "string"},
        "primary_keyword": {"type": "string"},
        "secondary_keywords": {"type": "array", "items": {"type": "string"}},
        "specific_hashtags": {"type": "array", "items": {"type": "string"}},
        "avoid": {"type": "string"},
    },
    "required": ["audience_intent", "angle", "promise", "primary_keyword", "secondary_keywords",
                 "specific_hashtags", "avoid"],
}


def _system(spec: dict, language: str) -> str:
    lang = (formats().get("languages") or {}).get(language, "")
    return (
        f"You package a finished video for {spec['label']}. You are a specialist in how this "
        "platform surfaces video, writing for an Indian markets creator.\n\n"
        "You are given EVIDENCE gathered from live data: what people are searching, which "
        "competing titles are getting views, and where the subject is in its trend cycle. Build "
        "on it — use the real search wording, learn from the winning titles without copying "
        "them, and follow the trend read.\n\n"
        "HOW TO USE THE EVIDENCE:\n"
        "- The evidence is for YOU. Never mention it in the copy: no 'gaining momentum', "
        "'trending', 'lots of interest', 'in recent news' — viewers want the subject, not a "
        "report on its popularity. Reader counts, search volumes and other videos' view counts "
        "are research, never content.\n"
        "- Use the search phrasing people type, but write it as proper language: names "
        "capitalised (Harshad Mehta, not harshad mehta), natural word order — never paste a "
        "raw lower-case query into a sentence.\n"
        "- Titles: make the options genuinely different — (1) the exact search phrase, plainly; "
        "(2) a specific question or tension the video resolves; (3) the stake or number that "
        "matters to an investor. No 'Understanding…', 'Everything about…', 'A guide to…' "
        "openers. If a winning competitor title uses a pattern, beat it, do not echo it.\n"
        "- Search suggestions show how people PHRASE a search; they are not facts about this "
        "video. Never adopt one that changes the meaning — if the video is about a rate hike, "
        "a suggestion about a rate cut is the wrong video. Write titles in proper title or "
        "sentence case, never all lower-case like a raw search query.\n"
        "- Each `why` must name the specific evidence used — the search suggestion quoted, or "
        "the competing title and its views per hour — not a general remark.\n"
        "- Thumbnail / cover text must not reuse the title's words: it carries the emotion or "
        "the stake, the title carries the subject.\n"
        "- Description: every sentence must tell the viewer something about the video. Cut any "
        "sentence that would be true of any video.\n\n"
        "HARD RULES:\n"
        "- Describe only what the creator says is in the video. Never promise content that is "
        "not there, and never invent numbers, quotes, timestamps or chapter times.\n"
        "- No guaranteed returns, no buy/sell advice, no 'sure shot' or 'multibagger guaranteed' "
        "claims — this is finance content.\n"
        "- No clickbait the video cannot pay off.\n\n"
        f"PLATFORM RULES for {spec['label']}:\n- " + "\n- ".join(spec.get("rules", []))
        + "\n\nPRODUCE:\n" + _field_brief(spec["fields"])
        + (f"\n\nLANGUAGE: {lang}" if lang else "")
        + memory.brand_block()
    )


# --- enforcing the limits in code ----------------------------------------------

def _clean_tag(t: str) -> str:
    return " ".join(str(t).replace("#", "").replace(",", " ").split())


def enforce(package: dict, fields: dict) -> tuple[dict, list[str]]:
    """Apply the platform's hard limits. Returns (package, notes on what was
    changed or is still wrong). Trimming is only done where it is safe (lists);
    an over-long title or description is reported, never silently cut."""
    notes: list[str] = []
    f = fields.get("titles")
    if f:
        for t in package.get("titles", []):
            n = len(t.get("text", ""))
            t["chars"] = n
            t["over_limit"] = n > f["max"]
            t["truncated_in_search"] = n > f.get("show", f["max"])
        if any(t["over_limit"] for t in package.get("titles", [])):
            notes.append(f"A title is over the {f['max']}-character limit and will be rejected — shorten it.")
    f = fields.get("description")
    if f:
        n = len(package.get("description", ""))
        package["description_chars"] = n
        if n > f["max"]:
            notes.append(f"Description is {n} characters; the limit is {f['max']} — cut it down.")
    f = fields.get("tags")
    if f:
        kept, total, seen = [], 0, set()
        for raw in package.get("tags", []):
            tag = _clean_tag(raw)
            # an over-long tag is dropped, not cut: "harshad mehta scam explained i"
            # is not something anyone searches
            if not tag or tag.lower() in seen or len(tag) > f.get("max_each", 30):
                continue
            cost = len(tag) + (1 if kept else 0)        # separators count toward the limit
            if total + cost > f["max_total"]:
                notes.append(f"Tags trimmed to fit the {f['max_total']}-character limit.")
                break
            kept.append(tag)
            seen.add(tag.lower())
            total += cost
        package["tags"], package["tags_chars"] = kept, total
    f = fields.get("hashtags")
    if f:
        tags, seen = [], set()
        for raw in package.get("hashtags", []):
            h = re.sub(r"[^\w]", "", str(raw), flags=re.UNICODE)
            if h and h.lower() not in seen:
                tags.append(h)
                seen.add(h.lower())
        if len(tags) > f["max"]:
            notes.append(f"Hashtags cut from {len(tags)} to the limit of {f['max']}.")
            tags = tags[: f["max"]]
        if len(tags) < f.get("min", 0):
            notes.append(f"Only {len(tags)} hashtag(s); aim for at least {f['min']}.")
        package["hashtags"] = tags
    for key in ("thumbnail_text", "cover_text"):
        f = fields.get(key)
        if f:
            package[key] = [" ".join(str(x).split()) for x in package.get(key, [])
                            if str(x).strip()][: f.get("n", 3)]
            if any(len(x.split()) > f.get("max_words", 4) for x in package[key]):
                notes.append(f"Some {key.replace('_', ' ')} options are longer than "
                             f"{f.get('max_words', 4)} words — they will be hard to read on the image.")
    f = fields.get("hooks")
    if f:
        hooks = [h for h in package.get("hooks", []) if str(h.get("text", "")).strip()][: f.get("n", 3)]
        for h in hooks:
            h["text"] = " ".join(str(h["text"]).split())
            h["chars"] = len(h["text"])
            h["over_limit"] = h["chars"] > f.get("max", 110)
        package["hooks"] = hooks
        if any(h["over_limit"] for h in hooks):
            notes.append(f"A hook is longer than {f.get('max', 110)} characters — it will be cut off before the reader gets it.")
    f = fields.get("pinned_comment")
    if f and len(package.get("pinned_comment", "")) > f["max"]:
        notes.append(f"Pinned comment is over {f['max']} characters.")
    return package, notes


# --- catching weak copy in code ------------------------------------------------

_FILLER = re.compile(
    r"\b(discover everything|everything (you need to know )?about|stay (updated|tuned)|in this video,? we"
    r"|delve|dive (deep )?into|unpack|landmark|monumental|game[- ]chang\w+|essential insights?"
    r"|vital insights?|critical insights?|well[- ]informed|gaining momentum|is trending|in recent news"
    r"|don'?t miss|must[- ]watch|let us know|share your (thoughts|insights)|what are your thoughts)\b", re.I)
_WEAK_OPENER = re.compile(r"^\s*(understanding|everything about|a guide to|all about|introduction to)\b", re.I)
_FIGURE = re.compile(r"(?:₹|rs\.?|\$|usd|inr)\s*[\d,.]+\s*(?:lakh crore|crore|lakh|billion|million|bn|mn|k)?"
                     r"|\b\d[\d,.]*\s*(?:%|percent|lakh crore|crore|lakh|billion|million|bn)", re.I)
_SMALL = {"the", "a", "an", "of", "for", "to", "in", "on", "and", "is", "vs", "ipo"}
# Words in a topic line that describe it rather than name it.
_GENERIC_SUBJECT = {"scam", "explained", "explain", "story", "news", "update", "analysis", "review", "price",
                    "share", "stock", "market", "today", "latest", "india", "indian", "the", "and", "for",
                    "what", "why", "how", "impact", "crash", "rally", "result", "results", "case"}


def _digits(s: str) -> str:
    return re.sub(r"[^\d]", "", s)


def _research_numbers(evidence: dict | None) -> set[str]:
    """Figures that describe the RESEARCH (readers a day, a rival's view count) —
    they are not facts about the video and must never appear in the copy."""
    look = (evidence or {}).get("lookup") or {}
    nums: list = []
    interest = look.get("interest") or {}
    nums += [interest.get(k) for k in ("last7", "prev7", "avg_per_day", "peak")]
    nums += [v.get("views") for v in (look.get("youtube") or {}).get("videos", [])]
    nums += [v.get("views") for v in (evidence or {}).get("videos", [])]
    return {str(int(n)) for n in nums if isinstance(n, (int, float)) and n >= 100}


def _proper_names(evidence: dict | None, topic: str) -> dict[str, str]:
    """lower-case word -> its proper spelling, for the subject's own name
    ("harshad" -> "Harshad"), taken from the matched Wikipedia article title."""
    title = (((evidence or {}).get("lookup") or {}).get("interest") or {}).get("article") or ""
    wanted = {w for w in re.findall(r"\w+", topic.lower()) if len(w) > 2}
    words = re.findall(r"\w+", title)
    # "Harshad Mehta" is a name. "Technical analysis" and "Price of oil" are
    # sentence case: the first capital says nothing, and "technical" in a
    # sentence is correct. All-capitals ("ITC") is always a name.
    named = len(words) >= 2 and all(w[0].isupper() for w in words)
    return {w.lower(): w for w in words if w.lower() in wanted and w != w.lower() and (named or w.isupper())}


# Talk about the research itself — reader counts, view rates, what creators are doing.
_RESEARCH_TALK = re.compile(
    r"\bviews? (per|an|a) (hour|day)\b|\b(readers?|searches) (a|per) day\b|\bdaily (readers?|searches|views)\b"
    r"|\bcreator videos?\b|\bsearch (volume|suggestions?|data)\b|\btop[- ]ranking quer\w+\b"
    r"|\b(people|viewers|traders|investors) are (searching|googling)\b", re.I)
# "Tag a friend" and friends: Meta and LinkedIn detect it and cut reach. Hinglish too.
_HINGLISH_BAIT = re.compile(r"\b(tag|share|follow|like)\s+(karo|kro|kariye|kijiye|karein|kardo|kar\s+do)\b", re.I)
_PRODUCTION_TALK = re.compile(
    r"\b(footage|atmospheric|on-?screen|voice-?over|b-?roll|handle logo|night[- ]drive"
    r"|this (reel|short|clip) (captures|shows|features|displays))\b", re.I)
_OTHER_LANGUAGE = {"english": re.compile(r"\bin hindi\b|\bhindi me(in)?\b|\bkya (tha|hai)\b|\bkab hua\b", re.I)}


def lint(package: dict, topic: str, notes: str, evidence: dict | None = None,
         language: str = "") -> list[str]:
    """Problems a reader would notice, found without a model: padding, a
    thumbnail that repeats the title, and figures the creator never gave."""
    issues: list[str] = []
    titles = [t.get("text", "") for t in package.get("titles", [])]
    hooks = [h.get("text", "") for h in package.get("hooks", [])]
    text_fields = titles + hooks + [package.get("description", ""), package.get("pinned_comment", "")]

    for t in titles:
        if _WEAK_OPENER.search(t):
            issues.append(f"Title “{t}” opens with a filler word — start with the subject or the question.")
        # (Devanagari has no capitals, so a Hindi title with an English word in it is fine.)
        if t and t == t.lower() and t.isascii() and any(c.isalpha() for c in t):
            issues.append(f"Title “{t}” is all lower-case like a raw search query — write it as a proper title.")
    seen = set()
    for m in _FILLER.finditer("\n".join(text_fields)):
        phrase = m.group(0).lower()
        if phrase not in seen:
            seen.add(phrase)
            issues.append(f"Remove the filler phrase “{m.group(0)}” — say something specific about the video instead.")

    title_words = [{w for w in re.findall(r"\w+", t.lower()) if w not in _SMALL} for t in titles]
    for key in ("thumbnail_text", "cover_text"):
        for option in package.get(key, []):
            words = {w for w in re.findall(r"\w+", option.lower()) if w not in _SMALL}
            if any(len(words & tw) >= 2 for tw in title_words):
                issues.append(f"{key.replace('_', ' ').capitalize()} “{option}” repeats the title's words — "
                              "give the stake or the emotion instead.")

    bait_text = "\n".join(text_fields)
    bait = algo._BAIT.search(bait_text) or _HINGLISH_BAIT.search(bait_text)
    if bait:
        issues.append(f"“{bait.group(0)}” is engagement bait, which platforms detect and cut reach for. Ask a real "
                      "question instead.")

    # Describing how it was made instead of what it gives the viewer.
    m = _PRODUCTION_TALK.search(joined_all := "\n".join(text_fields))
    if m:
        issues.append(f"“{m.group(0)}” describes how the post was made. Say what it means to the viewer instead.")
    # The same search phrase twice in one line reads like spam, not like a person.
    for line in titles + hooks:
        words = [w for w in re.findall(r"\w+", line.lower()) if w not in _SMALL]
        twice = [f"{a} {b}" for a, b in zip(words, words[1:]) if sum(1 for x in zip(words, words[1:]) if x == (a, b)) > 1]
        if twice:
            issues.append(f"“{line[:70]}” repeats “{twice[0]}” — write it the way a person would say it.")
            break
    pinned = package.get("pinned_comment") or ""
    if pinned and "?" not in pinned:
        issues.append("The pinned comment should ask one real question; a statement gives nobody a reason to reply.")

    # Research figures leaking into the copy ("1,401 daily readers are searching…").
    joined = "\n".join(text_fields)
    given_digits = _digits(f"{topic}\n{notes}")
    for num in sorted(_research_numbers(evidence), key=len, reverse=True):
        pretty = re.compile(r"(?<!\d)" + r"[,.]?".join(num) + r"(?!\d)")
        if num not in given_digits and pretty.search(joined):
            issues.append(f"The figure {int(num):,} comes from the research (reader or view counts), not from "
                          "the video — take it out of the copy.")
            break

    m = _RESEARCH_TALK.search(joined)
    if m:
        issues.append(f"“{m.group(0)}” talks about the research, not the video — the viewer does not care how "
                      "many people searched or watched. Say what the video shows.")

    # A name pasted in lower case straight from a search phrase ("harshad mehta
    # scam 1992"). Any lower-case use counts, even beside a correct one.
    for low, proper in _proper_names(evidence, topic).items():
        if re.search(rf"(?<![#@\w]){re.escape(low)}\b", joined):
            issues.append(f"Write the name properly everywhere — “{proper}”, not “{low}” — even inside a search phrase.")
            break

    # A search phrase in another language promises the wrong video
    # ("…explained in Hindi" on an English video).
    other = _OTHER_LANGUAGE.get(language)
    m = other.search(joined) if other else None
    if m:
        issues.append(f"“{m.group(0)}” is a Hindi-language search phrase, but this package is for an "
                      f"{language.capitalize()} video — remove it; it promises a different video.")

    # Search phrases pasted in whole. One, worked into a sentence, is good
    # SEO; three or more is keyword stuffing and reads like spam.
    phrases = [q for q in (evidence or {}).get("searches", []) if len(q.split()) >= 3]
    body = (package.get("description") or "").lower()
    pasted = [q for q in phrases if q.lower() in body]
    if len(pasted) >= 3:
        issues.append(f"The text pastes {len(pasted)} search phrases in whole (e.g. “{pasted[0]}”) — that is "
                      "keyword stuffing. Keep the main one, write the rest as normal sentences.")

    # The copy has to NAME the subject. A post about "the 1992 market
    # manipulation" that never says "Harshad Mehta" cannot be found by anyone
    # searching for him — and search is where an evergreen video lives.
    named = {w for w in re.findall(r"\w+", topic.lower()) if len(w) > 2 and w not in _GENERIC_SUBJECT}
    # (A feeling has no name to search for; its copy lives on the line, not the keyword.)
    if named and (evidence or {}).get("content_type") not in _SHARE_TYPES:
        def names_it(text: str) -> bool:
            low = text.lower()
            return any(w in low for w in named)
        lead = [t for t in titles + hooks if t]
        missing = [t for t in lead if not names_it(t)]
        if lead and len(missing) == len(lead):
            issues.append("None of the title / opening-line options names the subject — at least the "
                          f"search-led one must contain it ({', '.join(sorted(named)[:4])}).")
        body = package.get("description", "")
        if body and not names_it(body[:400]):
            issues.append("The text never names the subject in its opening — say it "
                          f"({', '.join(sorted(named)[:4])}) so search can find the post.")

    # Hashtags that name nothing: on every platform a tag is a topic label,
    # and "#StockMarket #Investing" labels ten million other posts.
    tags = [re.sub(r"[^\w]", "", str(h)).lower() for h in package.get("hashtags", [])]
    subject = {w for w in re.findall(r"\w+", topic.lower()) if len(w) > 2}
    if tags and not any(any(w in t for w in subject) for t in tags):
        issues.append("None of the hashtags names the subject — include at least one specific to it "
                      f"(built from: {', '.join(sorted(subject)[:5])}), not only broad ones like "
                      + ", ".join("#" + str(h) for h in package.get("hashtags", [])[:3]) + ".")

    # A figure is only safe if the creator supplied it: the package must
    # describe the video, and headlines are not the video.
    given = {_digits(m.group(0)) for m in _FIGURE.finditer(f"{topic}\n{notes}")}
    flagged = set()
    for m in _FIGURE.finditer("\n".join(text_fields)):
        d = _digits(m.group(0))
        if d and d not in given and d not in flagged:
            flagged.add(d)
            issues.append(f"The figure “{m.group(0).strip()}” is not in the creator's notes — remove it "
                          "unless the video states it.")
    return issues


# --- public --------------------------------------------------------------------

def _research(topic: str, terms: list[str], kwargs: dict) -> dict:
    """Look the subject up under what people really search. A second term is
    only spent (a YouTube search costs quota) when the first finds little."""
    best, score = None, -1
    for term in (terms[:2] or [topic]):
        look = lookup.research(term, **kwargs)
        found = ((look.get("youtube") or {}).get("found") or 0) + ((look.get("news") or {}).get("articles") or 0)
        if found > score:
            best, score = {**look, "term": term}, found
        if score >= 5:
            break
    return best


def analyse(platform: str, topic: str, board: dict | None = None, fetch=None, media: dict | None = None) -> dict:
    """The free half: evidence + the trend read, no LLM."""
    spec = formats().get(platform)
    if not isinstance(spec, dict) or "fields" not in spec:
        raise ValueError(f"unknown platform {platform!r}")
    kwargs = {"fetch": fetch} if fetch else {}
    seen = (media or {}).get("seen") or {}
    terms = [t for t in seen.get("search_terms", []) if _tokens(t)]
    evidence = insight.analyse(topic, board=board, suggest_feed=spec.get("suggest"), **kwargs)
    evidence["content_type"] = seen.get("content_type")
    # The tracker only knows its own beat. Look this one subject up live as
    # well — news, reader interest and YouTube — so any topic, however old or
    # narrow, gets its own trend read.
    if evidence.get("searchable") is not False:
        try:
            evidence["lookup"] = _research(topic, terms, kwargs)
        except Exception as exc:  # noqa: BLE001 — never lose the rest of the analysis over it
            evidence["lookup"] = {"errors": [f"lookup: {type(exc).__name__}"]}
        # What people type, for every phrase the subject goes by.
        merged = list(evidence.get("searches") or [])
        for term in terms[:3]:
            for q in insight.suggest(term, spec.get("suggest") or "web", **kwargs):
                if q not in merged:
                    merged.append(q)
        if merged:
            evidence["searches"] = merged[:15]
            evidence.setdefault("suggest_feed", "YouTube search" if spec.get("suggest") == "yt" else "Google search")
    return {"platform": platform, "label": spec["label"], "evidence": evidence,
            "strategy": strategy(evidence)}


def generate(platform: str, topic: str, notes: str = "", language: str = "english",
             board: dict | None = None, fetch=None, media_id: str | None = None, owner: str = "mine") -> dict:
    """Evidence + the written package for `platform`."""
    # A linked or uploaded post that was actually looked at (engine/media.py).
    from . import media as media_mod
    seen_media = media_mod.load(media_id) if media_id else None
    seen = (seen_media or {}).get("seen") or {}
    out = analyse(platform, topic, board, fetch, media=seen_media)
    spec = formats()[platform]
    user = (
        f"THE VIDEO IS ABOUT: {topic.strip()}\n"
        + (f"WHAT IT COVERS (from the creator):\n{notes.strip()}\n" if notes.strip() else
           "The creator gave no further detail — stay general about the contents; do not guess specifics.\n")
        + ("\nTHE POST ITSELF:\n" + media_mod.brief_for_writer(seen_media, owner, platform) + "\n" if seen_media else "")
        + (f"\nHOW TO PACKAGE THIS KIND OF CONTENT: {_PLAYBOOK[seen['content_type']]}\n"
           if seen.get("content_type") in _PLAYBOOK else "")
        + "\nEVIDENCE:\n" + _evidence_block(out["evidence"], out["strategy"])
        + f"\n\nWrite the {spec['label']} package now."
    )
    # What people are talking about right now. Connecting to it is the one thing
    # that can turn an evergreen post timely, when the connection is honest.
    rising = [e["label"] for e in ((board or {}).get("states") or {}).get("RISING", []) if e.get("niche")][:6]
    if rising:
        user += ("\n\nRISING IN THIS NICHE TODAY — use one only if it connects honestly to this content, to make it "
                 "feel timely, and say so in `why`. Never attach it to something unrelated, never to tragedy, and "
                 "never state a detail of it the video does not cover:\n- " + "\n- ".join(rising))

    # Memory: what was already written on this subject, and what the watched
    # creators' own results say about titles. Both go to the planner.
    past = past_work(topic)
    try:
        from . import recall
        learned = recall.lessons("creator")
    except Exception:  # noqa: BLE001
        learned = []
    if past:
        used = [f"- ({time.strftime('%d %b', time.localtime(p['created_at']))}, {p['platform']}) {line}"
                for p in past for line in p["lines"]][:14]
        user += ("\n\nALREADY WRITTEN FOR THIS SUBJECT — do not repeat or lightly reword any of these. "
                 "Find a different angle, or a follow-up to them:\n" + "\n".join(used))
    if learned:
        user += ("\n\nLEARNED FROM THE WATCHED CREATORS' OWN RESULTS (association, not a rule — use it "
                 "where it fits the video):\n- " + "\n- ".join(learned))

    # Step 1 — think before writing: who is searching for this, what do they
    # want, which angle is open. The writer then executes that plan instead
    # of improvising one while also counting characters.
    plan = None
    try:
        plan = llm.structured(_PLAN_SYSTEM.format(platform=spec["label"]) + memory.brand_block(),
                              user.replace(f"Write the {spec['label']} package now.", "Write the plan."),
                              _PLAN_SCHEMA, max_tokens=1200)
        user += "\n\nSTRATEGY TO EXECUTE (decided from the evidence — follow it):\n" + json.dumps(
            plan, ensure_ascii=False, indent=1)
    except Exception:  # noqa: BLE001 — a failed plan must not stop the package
        plan = None
    system, schema = _system(spec, language), _schema(spec["fields"])
    package = llm.structured(system, user, schema, max_tokens=3000)
    # One rewrite if the draft has problems code can see. Capped at one: a
    # second failure is shown to the creator rather than looped on.
    # Figures that were really seen in the creator's own post are safe; figures
    # seen in someone else's are not this creator's to state.
    known = notes
    if seen_media and owner == "mine":
        known += "\n" + "\n".join(seen.get("key_points", []) + seen.get("on_screen_text", []))
    real_stamps = {r["t"] for r in media_mod.real_chapters(seen_media, owner, platform)}
    any_stamp = {r["t"] for r in seen.get("timeline", [])}
    source_title = _tokens((seen_media or {}).get("facts", {}).get("title", "")) if owner == "theirs" else set()
    current_title = _norm((seen_media or {}).get("facts", {}).get("title", "")) if owner == "mine" else ""
    their_names = _their_specifics(seen, f"{topic}\n{notes}") if seen_media and owner == "theirs" else set()

    def check(pkg):
        found = lint(pkg, topic, known, out["evidence"], language)
        # A chapter time must come from the video: only the watched timeline counts.
        desc = pkg.get("description") or ""
        made_up = [t for t in re.findall(r"(?m)^\s*[-•]?\s*(\d{1,2}:\d{2}(?::\d{2})?)\b", desc)
                   if t not in real_stamps]
        # ...and written mid-sentence, a watched time is still a chapter time
        made_up += [t for t in any_stamp - real_stamps if re.search(rf"(?<![\d:]){re.escape(t)}\b", desc)]
        if made_up:
            found.append(f"Chapter time {made_up[0]} is not from the video — use only the real chapter times, or none.")
        found += _limit_problems(pkg, spec["fields"])
        if current_title and any(_norm(line) == current_title for line in _lines(pkg)):
            found.append("One title option just repeats the video's current title — every option must be a new attempt.")
        if their_names:
            copy = " ".join(_lines(pkg) + [pkg.get("description") or ""])
            used = sorted(n for n in their_names if re.search(rf"\b{re.escape(n)}\b", copy))
            if used:
                found.append(f"{', '.join(used[:4])} come from the other creator's video, not this creator's notes — "
                             "do not describe their specifics as being in this video.")
        if source_title:
            for line in _lines(pkg):
                toks = _tokens(line)
                if toks and len(toks & source_title) / len(toks | source_title) >= 0.6:
                    found.append(f"“{line}” is too close to the other creator's own title — write something original.")
                    break
        old = {_norm(line) for p in past for line in p["lines"]}
        repeat = next((line for line in _lines(pkg) if _norm(line) in old), None)
        if repeat:
            found.append(f"“{repeat}” was already used for this subject — write a different one.")
        return found

    issues = check(package)
    revised = False
    if issues:
        package = llm.structured(
            system,
            user + "\n\nYOUR PREVIOUS DRAFT:\n" + json.dumps(package, ensure_ascii=False)
            + "\n\nIt has these problems. Fix exactly these and keep everything else that was good:\n- "
            + "\n- ".join(issues),
            schema, max_tokens=3000)
        issues, revised = check(package), True
    for tag in reversed(spec.get("always_hashtags") or []):
        have = [h for h in package.get("hashtags", []) if str(h).lstrip("#").lower() != tag.lower()]
        package["hashtags"] = [tag] + have
    package, limit_notes = enforce(package, spec["fields"])
    out.update({"package": package, "limit_notes": limit_notes, "quality_notes": issues, "plan": plan,
                "past": [{k: p[k] for k in ("created_at", "platform", "topic", "lines")} for p in past],
                "media_used": bool(seen_media), "owner": owner if seen_media else None,
                "learned": learned,
                "revised": revised, "language": language, "fields": spec["fields"]})
    try:
        _save(platform, topic, out["strategy"].get("state"), package, plan, language)
    except Exception:  # noqa: BLE001 — history is a convenience; never fail the result over it
        pass
    return out


def _db():
    conn = analytics.connect()
    conn.executescript(_SCHEMA_SQL)
    have = {r["name"] for r in conn.execute("PRAGMA table_info(content_packages)")}
    for col in ("plan", "language"):               # added after the first release
        if col not in have:
            conn.execute(f"ALTER TABLE content_packages ADD COLUMN {col} TEXT")  # noqa: S608 — fixed names
    return conn


def _save(platform: str, topic: str, trend_state: str | None, package: dict,
          plan: dict | None = None, language: str = "") -> None:
    with closing(_db()) as conn:
        conn.execute("INSERT INTO content_packages (created_at, platform, topic, trend_state, package, plan, language)"
                     " VALUES (?,?,?,?,?,?,?)",
                     (time.time(), platform, topic[:300], trend_state, json.dumps(package, ensure_ascii=False),
                      json.dumps(plan, ensure_ascii=False) if plan else None, language))
        conn.commit()


def _limit_problems(package: dict, fields: dict) -> list[str]:
    """Lines over the platform's limit go back for a rewrite, not just a warning:
    a hook cut off mid-sentence is no hook."""
    out = []
    for key in ("titles", "hooks"):
        limit = (fields.get(key) or {}).get("max")
        for item in (package.get(key) or []) if limit else []:
            if len(item.get("text", "")) > limit:
                out.append(f"“{item['text'][:60]}…” is {len(item['text'])} characters; the limit is {limit}. Shorten it.")
    for key in ("thumbnail_text", "cover_text"):
        words = (fields.get(key) or {}).get("max_words")
        for option in (package.get(key) or []) if words else []:
            if len(str(option).split()) > words:
                out.append(f"“{option}” is too long for an image; at most {words} words.")
    limit = (fields.get("description") or {}).get("max")
    if limit and len(package.get("description") or "") > limit:
        out.append(f"The text is over the {limit}-character limit. Shorten it.")
    return out


def _their_specifics(seen: dict, ours: str) -> set[str]:
    """Names in someone else's video that this creator never mentioned: runs of
    capitalised words in its key points."""
    low = ours.lower()
    names: set[str] = set()
    for point in seen.get("key_points", []):
        for run in re.findall(r"\b[A-Z][\w&-]+(?:\s+[A-Z][\w&-]+)+|\b[A-Z]{3,}\b", point):
            if run.split()[0].lower() not in low:
                names.add(run)
    return names


def _lines(package: dict) -> list[str]:
    """The lines a viewer would see first: title options and opening hooks."""
    return [x.get("text", "") for x in (package.get("titles") or []) + (package.get("hooks") or [])
            if x.get("text")]


def _norm(line: str) -> str:
    return re.sub(r"[^\w]+", " ", line.lower()).strip()


def past_work(topic: str, limit: int = 6) -> list[dict]:
    """Earlier packages on the same subject — matched on the subject's words,
    so "Jio IPO" and "Jio IPO shareholder quota" find each other."""
    q = _tokens(topic)
    if not q:
        return []
    out = []
    with closing(_db()) as conn:
        for r in conn.execute("SELECT created_at, platform, topic, trend_state, package FROM content_packages"
                              " ORDER BY created_at DESC LIMIT 400"):
            t = _tokens(r["topic"])
            if not t or len(q & t) / len(q | t) < 0.5:
                continue
            lines = _lines(json.loads(r["package"]))
            if lines:
                out.append({"created_at": r["created_at"], "platform": r["platform"], "topic": r["topic"],
                            "trend_state": r["trend_state"], "lines": lines})
            if len(out) >= limit:
                break
    return out


def made_summary(limit: int = 40) -> list[dict]:
    """What has been packaged, by subject: how often, where, and when last."""
    with closing(_db()) as conn:
        return [{"topic": r["topic"], "packages": r["n"], "platforms": sorted(set((r["plats"] or "").split(","))),
                 "first": r["first"], "last": r["last"]}
                for r in conn.execute(
                    "SELECT topic, COUNT(*) AS n, GROUP_CONCAT(platform) AS plats, MIN(created_at) AS first,"
                    " MAX(created_at) AS last FROM content_packages GROUP BY lower(topic)"
                    " ORDER BY last DESC LIMIT ?", (limit,))]


def recent(limit: int = 15) -> list[dict]:
    with closing(_db()) as conn:
        return [{"id": r["id"], "created_at": r["created_at"], "platform": r["platform"],
                 "topic": r["topic"], "trend_state": r["trend_state"],
                 "package": json.loads(r["package"])}
                for r in conn.execute(
                    "SELECT * FROM content_packages ORDER BY created_at DESC LIMIT ?", (limit,))]
