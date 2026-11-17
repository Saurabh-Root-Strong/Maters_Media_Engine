"""Tags and hashtags that recent winning videos on a subject actually use.

The YouTube lookup (trends/lookup.py) already fetches the last 30 days of
videos on the subject with their full snippet, so this costs no extra quota.
A label counts as a trend only when SEVERAL channels use it: one channel's
template ("#PushkarRajThakur #Shorts #Viral") is not a trend.

Three guards keep it honest, because a tag that promises what the video is not
about is misleading metadata under YouTube's spam policy:
- off-subject videos are left out before counting (same filter as titles);
- junk ("#shorts", "#viral", "#fyp") and channel names never count;
- a trend label goes into a package only if every word in it is something the
  video or its notes actually say (relevant()).
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

MIN_CHANNELS = 2            # used by at least this many different channels
MAX_HASHTAG = 22            # a sentence-long hashtag has nobody following it
MAX_TAG = 30
TOP = 12

# \w misses Devanagari vowel signs (U+093E..), so "#शेयरबाजार" would stop at "श".
_L = r"[^\W_]|[ऀ-ॿ]"
_HASH = re.compile(rf"#((?:{_L})(?:{_L}|_){{1,60}})")
_WORDS = re.compile(rf"(?:{_L})+")
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
# Labels that say nothing about the subject: format, platform, engagement bait.
JUNK = frozenset("""
shorts short ytshorts youtubeshorts youtubeshort shortsfeed shortsvideo shortvideo viral viralvideo viralshorts
trending trend trendingshorts fyp foryou foryoupage explore explorepage reels reel reelsinstagram instagram
instareels tiktok youtube subscribe like likes share comment follow new video videos status whatsappstatus
1m 1k shortsviral feed ytviral
""".split())


# The niche's own evergreen labels: always popular, never "the trend". Listed,
# but ranked after specific ones, and a writer using them has not used a trend.
BROAD = frozenset("""
stockmarket sharemarket stockmarketindia sharemarketindia indianstockmarket stockmarketnews sharemarketnews
nifty nifty50 sensex banknifty trading trader investing investment investor stocks stock finance money
business businessnews economy indianeconomy financialnews news india trading101 stockmarketforbeginners
""".split())


# Words that name the niche or pad a phrase; sharing only these with the subject
# is not sharing the subject ("#StockMarketCrash" is not about a loss reel).
FILLER = frozenset("""
stock stocks share shares market markets stockmarket sharemarket india indian the a an of in on for and to is
today aaj live news latest update video videos short shorts reel reels kya hai ka ki ke ko se mein me ho
trading trader traders investing investor investors finance money nifty sensex status
""".split())


def _stem(w: str) -> str:
    if w.endswith("sses") or (w.endswith("es") and w[-3:-2] in ("s", "x", "h")) and len(w) > 4:
        return w[:-2]                       # losses -> loss, crashes -> crash
    return w[:-1] if w.endswith("s") and not w.endswith("ss") and len(w) > 3 else w   # trades -> trade; loss stays


def subject_words(*texts: str) -> set[str]:
    """The words that make a subject this subject: "share market loss" -> {"loss"}."""
    return {_stem(w) for t in texts for w in words(t or "") if w not in FILLER and len(w) > 2}


def key(label: str) -> str:
    """One key for "#StockMarket", "stock market" and "stockmarket"."""
    return "".join(_WORDS.findall(str(label).lower()))


def words(label: str) -> list[str]:
    """"StockMarketIndia" -> ["stock", "market", "india"]; "stock market" -> same."""
    text = _CAMEL.sub(" ", str(label).lstrip("#"))
    return _WORDS.findall(text.lower())


def harvest(snippet: dict) -> tuple[list[str], list[str]]:
    """(hashtags, tags) from one video's snippet: hashtags as written in its
    title and description, tags from its tag box."""
    text = f"{snippet.get('title') or ''}\n{snippet.get('description') or ''}"
    hashtags = list(dict.fromkeys(m.group(1) for m in _HASH.finditer(text)))[:30]
    tags = [str(t).strip() for t in (snippet.get("tags") or []) if str(t).strip()][:40]
    return hashtags, tags


def aggregate(sources: list[dict], noise: set[str] | None = None, exclude: set[str] | None = None,
              subject: set[str] | None = None) -> list[dict]:
    """Rank labels by how many channels use them, then by how fast those videos
    are gathering views. Returns [{text, kind, channels, videos, vph, fresh}]."""
    noise = {w.lower() for w in noise or ()}
    channels = {key(s.get("channel") or "") for s in sources} - {""}
    skip = JUNK | channels | {key(x) for x in exclude or ()}
    seen_by: dict[str, set] = defaultdict(set)
    vids: Counter = Counter()
    vph: Counter = Counter()
    fresh: dict[str, bool] = {}
    spell: dict[str, Counter] = defaultdict(Counter)
    kind: dict[str, Counter] = defaultdict(Counter)
    for s in sources:
        labels = [(h, "hashtag") for h in s.get("hashtags") or []] + [(t, "tag") for t in s.get("tags") or []]
        done = set()
        for text, k in labels:
            kk = key(text)
            if (len(kk) < 3 or kk.isdigit() or kk in skip or kk in done
                    or any(kk.startswith(j) and kk[len(j):].isdigit() for j in ("shorts", "trending"))):
                continue
            if noise & set(words(text)):
                continue
            limit = MAX_HASHTAG if k == "hashtag" else MAX_TAG
            if len(text) > limit:
                continue
            done.add(kk)
            seen_by[kk].add(key(s.get("channel") or "") or id(s))
            vids[kk] += 1
            vph[kk] += s.get("views_per_hour") or 0
            fresh[kk] = fresh.get(kk, False) or (s.get("age_days") or 99) <= 7
            spell[kk][text] += 1
            kind[kk][k] += 1
    out = []
    for kk, who in seen_by.items():
        if len(who) < MIN_CHANNELS:
            continue
        # A search for "share market loss" also returns this week's crash news:
        # "#Nvidia" or "#Politics" are popular there, but not about this subject.
        if subject and kk not in BROAD and not subject & {_stem(w) for w in words(next(iter(spell[kk])))}:
            continue
        best = kind[kk].most_common(1)[0][0]
        # hashtags keep their CamelCase; a tag-only label is shown as a hashtag-ready word too
        text = max(spell[kk].items(), key=lambda x: (x[1], any(c.isupper() for c in x[0])))[0]
        out.append({"text": text, "key": kk, "kind": best, "channels": len(who), "videos": vids[kk],
                    "vph": round(vph[kk]), "fresh": fresh[kk]})
    for t in out:
        t["broad"] = t["key"] in BROAD
    # specific labels first: "#ShareMarketLoss" says more than "#ShareMarket", which every video carries
    out.sort(key=lambda x: (x["broad"], -x["channels"], -x["vph"]))
    return out[:TOP]


def relevant(label: str, vocab: set[str]) -> bool:
    """Every word of the label is something this video or its notes say. Works
    for "#StockMarketIndia", "stock market india" and "stockmarketindia" (split
    by dictionary when there are no capitals or spaces)."""
    ws = words(label)
    if len(ws) == 1 and len(ws[0]) > 6 and ws[0] not in vocab:
        return _breaks(ws[0], vocab)
    return bool(ws) and all(w in vocab for w in ws)


def _breaks(s: str, vocab: set[str]) -> bool:
    ok = [True] + [False] * len(s)
    for i in range(1, len(s) + 1):
        for j in range(max(0, i - 20), i):
            if ok[j] and i - j >= 2 and s[j:i] in vocab:
                ok[i] = True
                break
    return ok[-1]


def as_hashtag(label: str) -> str:
    """"stock market reality" -> "StockMarketReality"; a hashtag is kept as written."""
    raw = str(label).lstrip("#")
    if " " not in raw.strip():
        return "".join(_WORDS.findall(raw))
    return "".join(w[:1].upper() + w[1:] for w in _WORDS.findall(raw))


def add(package: dict, trends: list[dict], vocab: set[str], fields: dict, language: str,
        most_hashtags: int = 2, most_tags: int = 3) -> list[str]:
    """Put up to two trend hashtags and three trend tags into the package when the
    writer used none, only ones relevant() to this video. Returns what was added."""
    added: list[str] = []
    specific = [t for t in trends if not t.get("broad")]
    keys = {t["key"] for t in specific}
    fit = [t for t in specific if relevant(t["text"], vocab)
           and (language == "hindi" or not _DEVANAGARI.search(t["text"]))]
    if "hashtags" in fields and isinstance(package.get("hashtags"), list):
        have = {key(h) for h in package["hashtags"]}
        if not have & keys:
            new = [as_hashtag(t["text"]) for t in fit if t["key"] not in have
                   and len(as_hashtag(t["text"])) <= MAX_HASHTAG][:most_hashtags]
            # after the subject's own first hashtag, before the rest: the first ones show above the title
            package["hashtags"] = package["hashtags"][:1] + new + package["hashtags"][1:]
            added += ["#" + h for h in new]
    if "tags" in fields and isinstance(package.get("tags"), list):
        have = {key(t) for t in package["tags"]}
        if not have & keys:
            new = [" ".join(words(t["text"])) for t in fit if t["key"] not in have][:most_tags]
            package["tags"] = package["tags"][:3] + new + package["tags"][3:]
            added += new
    return added


def used_in(ready: list[dict], trends: list[dict]) -> list[dict]:
    """Which trend labels made it into what the creator will paste (after the
    reviewer's pick): hashtags anywhere in the text, tags from the tag box."""
    have: set[str] = set()
    for x in ready or []:
        text = str(x.get("text") or "")
        have |= {key(m.group(1)) for m in _HASH.finditer(text)}
        if x.get("label") == "Tags":
            have |= {key(t) for t in text.split(",")}
    return [t for t in trends if t["key"] in have]
