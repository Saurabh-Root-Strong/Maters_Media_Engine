"""Collectors for the free sources.

Each source is split into a pure `parse_*` (text in, records out — unit-tested
against saved payloads) and a thin `fetch_*` that does the HTTP. A record is:

    {source, ext_id, title, url, publisher, published_at, value, extra_text}

`value` is the source's own measured number at collection time (search volume,
video views, daily page views) or None for news, where the signal is the count
of articles. `extra_text` is additional wording used only for topic matching.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from .text import strip_publisher

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) MediaEngine/1.0 (personal trend dashboard)"
_HT = "{https://trends.google.com/trending/rss}"
_ATOM = "{http://www.w3.org/2005/Atom}"
_YT = "{http://www.youtube.com/xml/schemas/2015}"
_MEDIA = "{http://search.yahoo.com/mrss/}"


def http_get(url: str, timeout: float = 25.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                               "Accept-Language": "en-IN,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 — fixed https hosts
        return r.read().decode("utf-8", "replace")


def _rfc822(s: str | None) -> float | None:
    try:
        return parsedate_to_datetime(s).timestamp() if s else None
    except (TypeError, ValueError):
        return None


def _iso(s: str | None) -> float | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() if s else None
    except (TypeError, ValueError):
        return None


def parse_traffic(s: str | None) -> float:
    """'200+' -> 200, '2K+' -> 2000, '1M+' -> 1e6, '20,000+' -> 20000."""
    m = re.match(r"\s*([\d.,]+)\s*([KkMmBb]?)", s or "")
    if not m:
        return 0.0
    try:
        n = float(m.group(1).replace(",", ""))
    except ValueError:
        return 0.0
    return n * {"": 1, "k": 1e3, "m": 1e6, "b": 1e9}[m.group(2).lower()]


# --- Google Trends "trending now" ------------------------------------------------

def gtrends_url(geo: str) -> str:
    return f"https://trends.google.com/trending/rss?geo={urllib.parse.quote(geo)}"


def parse_gtrends(xml_text: str) -> list[dict]:
    out = []
    for it in ET.fromstring(xml_text).iter("item"):
        query = (it.findtext("title") or "").strip()
        if not query:
            continue
        news = [n.findtext(f"{_HT}news_item_title") or "" for n in it.iter(f"{_HT}news_item")]
        first_url = next((n.findtext(f"{_HT}news_item_url") for n in it.iter(f"{_HT}news_item")), None)
        out.append({
            "source": "gtrends", "ext_id": query.lower(), "title": query,
            "url": first_url or "https://trends.google.com/trending?geo=IN",
            "publisher": "Google Trends",
            "published_at": _rfc822(it.findtext("pubDate")),
            "value": parse_traffic(it.findtext(f"{_HT}approx_traffic")),
            # The attached headlines say what the bare query is about.
            "extra_text": " . ".join(news),
        })
    return out


# --- Google News -----------------------------------------------------------------

def news_url(feed: dict, geo: str = "IN") -> str:
    tail = f"hl=en-{geo}&gl={geo}&ceid={geo}:en"
    if feed.get("q"):
        return f"https://news.google.com/rss/search?q={urllib.parse.quote_plus(str(feed['q']))}&{tail}"
    return f"https://news.google.com/rss/headlines/section/topic/{urllib.parse.quote(str(feed['topic']))}?{tail}"


def parse_news(xml_text: str) -> list[dict]:
    out = []
    for it in ET.fromstring(xml_text).iter("item"):
        raw = (it.findtext("title") or "").strip()
        if not raw:
            continue
        publisher = (it.findtext("source") or "").strip() or raw.rpartition(" - ")[2].strip()
        title = strip_publisher(raw)
        # The same article arrives through several feeds with different guids;
        # publisher + headline is the stable identity.
        ext = hashlib.sha1(f"{publisher.lower()}|{title.lower()}".encode()).hexdigest()[:20]
        out.append({
            "source": "news", "ext_id": ext, "title": title,
            "url": (it.findtext("link") or "").strip(), "publisher": publisher,
            "published_at": _rfc822(it.findtext("pubDate")),
            "value": None, "extra_text": "",
        })
    return out


# --- YouTube channel feed --------------------------------------------------------

def youtube_url(channel_id: str) -> str:
    return f"https://www.youtube.com/feeds/videos.xml?channel_id={urllib.parse.quote(channel_id)}"


def parse_youtube(xml_text: str, channel_name: str = "") -> list[dict]:
    out = []
    for e in ET.fromstring(xml_text).iter(f"{_ATOM}entry"):
        vid = e.findtext(f"{_YT}videoId")
        title = (e.findtext(f"{_ATOM}title") or "").strip()
        if not vid or not title:
            continue
        stats = e.find(f"{_MEDIA}group/{_MEDIA}community/{_MEDIA}statistics")
        try:
            views = float(stats.get("views")) if stats is not None else None
        except (TypeError, ValueError):
            views = None
        out.append({
            "source": "youtube", "ext_id": vid, "title": title,
            "url": f"https://www.youtube.com/watch?v={vid}",
            "publisher": channel_name or (e.findtext(f"{_ATOM}author/{_ATOM}name") or ""),
            "published_at": _iso(e.findtext(f"{_ATOM}published")),
            "value": views, "extra_text": "",
        })
    return out


def fetch_youtube_api(channel_id: str, key: str, channel_name: str = "", get=None) -> list[dict]:
    """A channel's latest uploads with exact view counts via the YouTube Data
    API (free key, 10,000 quota units/day; this costs 2 per call). Same record
    shape as parse_youtube. Used when YOUTUBE_API_KEY is set — the free feed
    above is unreliable on some days."""
    get = get or http_get
    api = "https://www.googleapis.com/youtube/v3"
    uploads = "UU" + channel_id[2:]          # every channel's uploads playlist id
    q = urllib.parse.urlencode
    items = json.loads(get(f"{api}/playlistItems?" + q(
        {"part": "contentDetails", "maxResults": 15, "playlistId": uploads, "key": key}))).get("items", [])
    ids = [i["contentDetails"]["videoId"] for i in items if i.get("contentDetails", {}).get("videoId")]
    if not ids:
        return []
    videos = json.loads(get(f"{api}/videos?" + q(
        {"part": "snippet,statistics,contentDetails,liveStreamingDetails", "id": ",".join(ids),
         "key": key}))).get("items", [])
    out = []
    for v in videos:
        sn, stats = v.get("snippet", {}), v.get("statistics", {})
        if not sn.get("title"):
            continue
        try:
            views = float(stats["viewCount"]) if "viewCount" in stats else None
        except (TypeError, ValueError):
            views = None
        out.append({"source": "youtube", "ext_id": v["id"], "title": sn["title"].strip(),
                    "url": f"https://www.youtube.com/watch?v={v['id']}",
                    "publisher": channel_name or sn.get("channelTitle", ""),
                    "published_at": _iso(sn.get("publishedAt")), "value": views, "extra_text": "",
                    "kind": video_kind(v)})
    return out


def _seconds(iso: str | None) -> int | None:
    """ISO-8601 duration ('PT1H2M3S') -> seconds."""
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m or not any(m.groups()):
        return None
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + se


def video_kind(v: dict) -> str:
    """'live' (stream, running or recorded), 'short' (<= 3 minutes) or 'long'.
    Their view rates differ by orders of magnitude, so they are only ever
    compared like with like."""
    if v.get("liveStreamingDetails") or v.get("snippet", {}).get("liveBroadcastContent") in ("live", "upcoming"):
        return "live"
    secs = _seconds((v.get("contentDetails") or {}).get("duration"))
    return "short" if secs is not None and secs <= 180 else "long"


# --- Wikipedia top pages per country ---------------------------------------------

_WIKI_SKIP = ("Main_Page", "Special:", "Wikipedia:", "Portal:", "File:", "Help:", "Category:",
              "Template:", "User:", "Talk:", "Draft:")


def wiki_url(geo: str, day) -> str:
    return ("https://wikimedia.org/api/rest_v1/metrics/pageviews/top-per-country/"
            f"{urllib.parse.quote(geo)}/all-access/{day:%Y/%m/%d}")


def parse_wiki(json_text: str) -> list[dict]:
    data = json.loads(json_text)
    out = []
    for block in data.get("items", []):
        try:
            day = datetime(int(block["year"]), int(block["month"]), int(block["day"]),
                           tzinfo=timezone.utc)
        except (KeyError, ValueError):
            continue
        for a in block.get("articles", []):
            name = a.get("article", "")
            if a.get("project") != "en.wikipedia" or name.startswith(_WIKI_SKIP) or name.startswith("."):
                continue
            out.append({
                "source": "wiki", "ext_id": name, "title": name.replace("_", " "),
                "url": f"https://en.wikipedia.org/wiki/{urllib.parse.quote(name)}",
                "publisher": "Wikipedia",
                # A day's total is only known once the day is over.
                "published_at": (day + timedelta(days=1)).timestamp(),
                "value": float(a.get("views_ceil") or 0), "extra_text": "",
            })
    return out
