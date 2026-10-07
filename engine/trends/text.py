"""Text handling for topic grouping: tokenising headlines, and deciding whether
a search trend / Wikipedia page belongs to the finance niche.

Deliberately simple and deterministic — every grouping decision can be traced
to shared words, so a wrong merge is debuggable.
"""

from __future__ import annotations

import re
import unicodedata

# Words that carry no topic information in a headline.
_STOP = set("""
a an the and or but if of in on at to for from by with without into over under as is are was were be been
being it its this that these those he she they we you i his her their our your not no yes do does did done
has have had will would can could should may might must than then so such very more most much many few
what when where which who whom why how all any both each other some own same too just only also about
after before again against between during through up down out off above below here there now new latest
says said say tells told amid ahead vs versus via per live update updates news today tomorrow yesterday
check know details explained explainer full list top key big watch watchlist video photos pics here's
heres set sets get gets got make makes made take takes see sees seen look looks likely expected expect
due day week month year years days weeks months time times first second third one two three four five
six seven eight nine ten mon tue wed thu fri sat sun jan feb mar apr jun jul aug sep sept oct nov dec
october november december january february march april june july august september monday tuesday
wednesday thursday friday saturday sunday amp nbsp com www http https
""".split())

# Hindi (Devanagari) and Hinglish (Roman) filler. Half of India's finance
# video titles are Hinglish; without these, "kaise", "hai" or "में" would count
# as shared NAMES and glue unrelated videos into one topic. Measured
# 2026-10-07: में was the single most frequent word in tracked video titles.
_STOP |= set("""
hai hain ho hoga hogi honge tha thi the ka ki ke ko me mein se par pe aur ya bhi ab abhi kab kya kyu kyun kaise
kaun kahan jab tab yeh ye wo woh voh is us iska uska apna apne apni sab ek do teen kar karo kare karna karein
kiya kiye raha rahi rahe gaya gayi gaye diya diye liya liye lo le de dekho dekhiye dekhe janiye jaane samjhe
samjhiye sach bada badi bade naya nayi naye wala wali wale log logon bhai dosto bas phir fir agar lekin magar
toh to na nahi nahin mat hum aap tum unka unke inke
है हैं था थी थे का की के को में से पर और या भी अब क्या क्यों कैसे कब यह ये वो वह इस उस एक दो कर करें किया
रहा रही रहे गया गई गए दिया लिया हो होगा होगी नहीं ना तो लेकिन अगर सब आप हम जानिए देखिए समझिए बड़ा बड़ी बड़े
""".split())

# Channel and programme tags that appear in video titles ("| N18G", "#sanjaykathuria").
# Extended at runtime with the configured channels' own names (set_channel_noise).
_STOP |= {"n18g", "n18v", "n18s", "n18l", "n18m", "n18p", "cnbc", "awaaz", "cnbctv18", "zeebiz", "zee", "etnow",
          "ndtv", "moneycontrol", "shorts", "short", "live", "breaking", "latest"}


def set_channel_noise(names) -> None:
    """Treat the watched channels' run-together hashtags as filler ("#sanjaykathuria",
    "#marketgabru"): a channel tagging itself on every video must not make all
    its videos look like one story. Only the joined-up form is added — the
    separate words ("market", "trader", "groww") are real subjects."""
    for name in names or []:
        words = [w for w in re.split(r"[^\w]+", str(name).lower()) if w]
        if len(words) > 1:
            _STOP.add("".join(words))


# Real words, but present in most market headlines — on their own they do not
# identify a story, so they count for less when matching.
GENERIC = set("""
share stock market price india indian nifty sensex bse nse target buy sell hold rs crore lakh pct percent
investor trader trade trading rally fall rise gain loss drop surge jump slip slide crash point high low
record close open end session index equity company firm business sector result earning profit revenue
quarter q1 q2 q3 q4 fy25 fy26 fy27 growth outlook analyst brokerage call pick money rate bank fund mutual
plan deal report data global world govt government minister ministry policy
ipo allotment status online gmp subscription subscribed listing list debut premium discount issue lot size
band launch date schedule mean upcoming focus impact decoded opening closing bell wrap update early late
grey registrar apply review should much per third-quarter second-quarter first-quarter fourth-quarter
higher lower reason extend lead recovery positive negative momentum settle mid-day snap streak straight
consecutive maintain drive driving weigh edge flat mixed cue signal start trend
""".split())

# Marks a Google-Trends query / Wikipedia page as finance-relevant.
_NICHE = set("""
nifty sensex bse nse stock share ipo sebi rbi repo inflation gdp rupee dollar forex crude oil gold silver
bullion fii dii mutual fund etf dividend bonus split demerger merger acquisition buyback results
earnings quarter profit revenue budget tax gst income itr bank banking nbfc loan emi insurance lic adani
ambani reliance tata infosys tcs wipro hdfc icici sbi kotak axis bajaj maruti mahindra itc hindustan
unilever larsen airtel jio paytm zomato swiggy nykaa ola vedanta ongc ntpc coal steel jsw hindalco
bitcoin crypto ethereum fed fomc powell nasdaq dow recession tariff sanctions export import trade
economy economic fiscal monetary bond yield treasury market markets investor investing broker zerodha
groww upstox smallcap midcap largecap futures options derivatives expiry hindenburg scam fraud
stake promoter brokerage valuation listing allotment gmp fpi qip npa
shareholder equity equities portfolio selloff ebitda capex
q1 q2 q3 q4 fy25 fy26 fy27 fy28 psu disinvestment nav aum hni investor bourse
""".split())
# Deliberately NOT in the list, though they look financial: "lakh"/"crore" (every
# car-launch price), "rally" (political rallies), "sip" (a drink), "bull"/"bear",
# "retail", "credit", "margin", "circuit" — each pulled non-market stories onto
# the niche board when tried against live data (2026-10-06).

def _words(text: str) -> list[str]:
    """Split into words, Unicode-aware. Combining marks (category M) are kept:
    Devanagari vowel signs are marks, and a plain \\w regex cuts Hindi words
    apart at every one of them."""
    kept = "".join(c if c.isalnum() or c in "&'-" or unicodedata.category(c)[0] == "M" else " "
                   for c in text)
    return kept.split()


def _stem(w: str) -> str:
    """Very light plural folding: shares->share, companies->company."""
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


# Tokens are stemmed, so the word lists must be too or "results" never matches.
GENERIC = {_stem(w) for w in GENERIC}
_NICHE = {_stem(w) for w in _NICHE}


def niche_extra(words) -> set[str]:
    """Config-supplied niche keywords, normalised the same way as tokens."""
    out: set[str] = set()
    for w in words or []:
        out |= tokens(str(w))
    return out


def strip_publisher(title: str) -> str:
    """Google News titles end with ' - Publisher'."""
    head, sep, tail = title.rpartition(" - ")
    return head.strip() if sep and len(tail) <= 40 and head else title.strip()


def tokens(text: str) -> set[str]:
    out = set()
    for raw in _words((text or "").lower().replace("’", "'")):
        w = raw.strip("'-&").replace("'s", "")
        if len(w) < 2 or w.isdigit() or w in _STOP:
            continue
        w = _stem(w)
        if w and w not in _STOP:
            out.add(w)
    return out


def weight(toks) -> float:
    """Matching weight of a token set — generic market words count half."""
    return sum(0.5 if t in GENERIC else 1.0 for t in toks)


def is_niche(toks: set[str], extra: set[str] | None = None) -> bool:
    return bool(toks & _NICHE) or bool(extra and toks & extra)
