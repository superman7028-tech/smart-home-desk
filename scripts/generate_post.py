#!/usr/bin/env python3
"""Draft one smart-home post from a live niche trend and update MkDocs nav.

Scheduled every 6 hours (00:00, 06:00, 12:00, 18:00 local time).
Uses Ollama on this Mac. Set OPENAI_API_KEY to use OpenAI instead.
"""

from __future__ import annotations

import argparse
import fcntl
import html
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
POSTS = DOCS / "posts"
DATA = ROOT / "data"
LOGS = ROOT / "logs"
SEEN_PATH = DATA / "seen.json"
STATE_PATH = DATA / "state.json"
LOCK_PATH = DATA / "generate.lock"
MIN_GAP_SECONDS = 5 * 60 * 60

ATOM = "{http://www.w3.org/2005/Atom}"
TRENDS_NS = {"ht": "https://trends.google.com/trending/rss"}
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
SUBREDDITS = ("smarthome", "homeassistant", "homeautomation", "MatterProtocol")
NICHE_RE = re.compile(
    r"smart home|smarthome|home assistant|homeassistant|zigbee|zwave|z-wave|"
    r"matter|thread|homekit|alexa|google home|smart plug|smart bulb|thermostat|"
    r"doorbell|robot vacuum|esphome|tasmota|iot|sensor|hue|home automation",
    re.I,
)
BLOCK_RE = re.compile(
    r"\b(killed|shooting|murder|amber alert|suicide|war|election|measles|"
    r"vaccine|kratom|overdose|porn|obituary)\b",
    re.I,
)
SKIP_TITLE_RE = re.compile(
    r"\b(megathread|weekly thread|daily thread|free talk|community day|"
    r"countdown|hiring|we are hiring)\b",
    re.I,
)
DEAL_RE = re.compile(
    r"(\$\d|\d+% off|\bdeal\b|stock up|discount code|coupon|at a steal|\boffloads?\b)",
    re.I,
)
OFF_TOPIC_RE = re.compile(
    r"without the smart home|wet-bulb|why does it matter|dark matter|stock market|"
    r"\b(illness|deadly|fda|menopause|bitcoin|crypto|gold|dishwasher|horoscope|obituary|cagr)\b|"
    r"market demonstrates|market size",
    re.I,
)
FRESH_HOURS = 48
VAGUE_RE = re.compile(
    r"\b(like this|what is this|look at this|anyone else|does anyone|is this worth)\b",
    re.I,
)
NEWS_SEARCH = (
    "https://news.google.com/rss/search?q=%22smart+home%22+OR+%22Home+Assistant%22"
    "+OR+Matter+bulb&hl=en-US&gl=US&ceid=US:en"
)
CACHE_PATH = DATA / "feed_cache.json"

TOPIC_BANK = (
    ("Do you still need a hub for Matter devices?", "Matter hub buying decision"),
    ("Zigbee vs Wi-Fi motion sensors for a rental", "sensor protocol choice"),
    ("What happens to cloud smart plugs when the company shuts the app down", "local control"),
    ("Home Assistant Green vs a used mini PC", "hub hardware"),
    ("Thread border routers and flaky Matter devices", "Thread networking"),
    ("Energy-monitoring plugs and space-heater circuits", "energy monitoring safety"),
    ("Apartment door locks that do not require a new deadbolt", "renter locks"),
    ("Why many smart bulbs only join 2.4 GHz Wi-Fi", "wifi setup"),
    ("Robot vacuum no-go zones that survive a remap", "robot vacuum maps"),
    ("Video doorbells that record without a monthly fee", "local video storage"),
    ("Smart thermostats in apartments that ban new wiring", "thermostat constraints"),
    ("Water-leak sensors under a washing machine", "leak sensors"),
    ("Presence detection without a dozen motion sensors", "presence detection"),
    ("ESPHome vs stock firmware on a cheap plug", "local firmware"),
    ("Guest access without handing over the Wi-Fi password", "guest access"),
    ("Grouping lights so one dead bulb does not kill the scene", "lighting scenes"),
)

DEFAULT_QUERIES = (
    ("Energy-monitoring smart plugs", "energy monitoring smart plug"),
    ("Zigbee motion sensors", "zigbee motion sensor"),
    ("Matter smart bulbs", "matter smart bulb"),
    ("Home Assistant hardware", "home assistant green"),
    ("Thread border routers", "thread border router"),
)
QUERY_RULES = (
    (r"bulb|light|hue|lamp", "Matter smart bulbs", "matter smart bulb"),
    (r"plug|energy|heater|outlet", "Energy-monitoring plugs", "energy monitoring smart plug"),
    (r"lock|deadbolt", "Smart locks for apartments", "smart lock keypad no wiring"),
    (r"thermostat|hvac", "Smart thermostats", "smart thermostat"),
    (r"doorbell|camera", "Video doorbells", "video doorbell local storage"),
    (r"vacuum|robot", "Robot vacuums", "robot vacuum"),
    (r"leak|water", "Water-leak sensors", "water leak sensor"),
    (r"motion|presence|sensor", "Motion and presence sensors", "zigbee motion sensor"),
    (r"hub|home assistant|mini pc|green", "Home Assistant hardware", "home assistant green"),
    (r"matter|thread|border", "Thread border routers", "thread border router"),
    (r"router|offline|wi-fi|wifi", "Routers for smart-home devices", "wifi router smart home 2.4 ghz"),
)


@dataclass
class Topic:
    title: str
    url: str
    excerpt: str
    source: str
    score: int
    published: str = ""


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def setup_logging() -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        handlers=[
            logging.FileHandler(LOGS / "generate.log"),
            logging.StreamHandler(),
        ],
    )


def log(message: str) -> None:
    logging.info(message)


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def save_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug[:60] or "post").strip("-")


def yaml_quote(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip().replace("\\", "\\\\").replace('"', '\\"')
    return f'"{cleaned}"'


def html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>", "\n", raw)
    raw = re.sub(r"(?i)</p>", "\n", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    raw = html.unescape(raw)
    raw = raw.replace("\xa0", " ")
    raw = re.sub(r"[ \t]+", " ", raw)
    raw = re.sub(r"\n{2,}", "\n", raw)
    return raw.strip()


def fetch_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        if exc.code != 429:
            raise
        time.sleep(3)
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()


def amazon_url(query: str, tag: str) -> str:
    params = {"k": query}
    if tag:
        params["tag"] = tag
    return "https://www.amazon.com/s?" + urllib.parse.urlencode(params)


def related_queries(title: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for pattern, label, query in QUERY_RULES:
        if re.search(pattern, title, re.I) and query not in seen:
            found.append((label, query))
            seen.add(query)
    for label, query in DEFAULT_QUERIES:
        if query not in seen:
            found.append((label, query))
            seen.add(query)
        if len(found) >= 3:
            break
    return found[:3]


def product_topic(title: str) -> bool:
    if OFF_TOPIC_RE.search(title):
        return False
    if re.search(r"without the (app,|bulb|smart home)|or the smart home", title, re.I):
        return False
    return (
        re.search(
            r"home assistant|homekit|zigbee|z-wave|zwave|alexa|google home|nanoleaf|"
            r"\bhue\b|tapo|govee|ikea|esphome|smart (home|light|bulb|plug|lock|switch)|"
            r"\b(bulb|lamp|plug|sensor|thermostat|doorbell|vacuum)s?\b|thread border|matter[- ]|"
            r"home hub|smartthings|switchbot|smart lights|router setting",
            title,
            re.I,
        )
        is not None
    )


def eligible_title(title: str) -> bool:
    title = title.strip()
    if len(title) < 18:
        return False
    if BLOCK_RE.search(title) or SKIP_TITLE_RE.search(title) or DEAL_RE.search(title) or VAGUE_RE.search(title):
        return False
    return True


def parse_date(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        if value[:4].isdigit():
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def is_fresh(value: str, hours: int = FRESH_HOURS) -> bool:
    parsed = parse_date(value)
    if parsed is None:
        return False
    return datetime.now(timezone.utc) - parsed <= timedelta(hours=hours)


def recency_score(value: str) -> float:
    parsed = parse_date(value)
    if parsed is None:
        return 0
    age_hours = max(0, (datetime.now(timezone.utc) - parsed).total_seconds() / 3600)
    return 1000 - age_hours


def news_search_url() -> str:
    query = (
        '("smart home" OR "Home Assistant" OR "smart lights" OR SmartThings OR '
        'Zigbee OR "Matter bulb" OR "Home Hub" OR SwitchBot) when:2d'
    )
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}
    )


def strip_publisher(title: str) -> str:
    if " - " not in title:
        return title.strip()
    head, tail = title.rsplit(" - ", 1)
    if head.strip() and len(tail.strip()) <= 40:
        return head.strip()
    return title.strip()


def remember_feed(name: str, topics: list[Topic]) -> None:
    cache = load_json(CACHE_PATH, {})
    cache[name] = {
        "saved_at": time.time(),
        "topics": [
            {
                "title": topic.title,
                "url": topic.url,
                "excerpt": topic.excerpt,
                "source": topic.source,
                "score": topic.score,
                "published": topic.published,
            }
            for topic in topics
        ],
    }
    save_json(CACHE_PATH, cache)


def recall_feed(name: str, max_age: int = 36 * 60 * 60) -> list[Topic]:
    entry = load_json(CACHE_PATH, {}).get(name) or {}
    saved = float(entry.get("saved_at") or 0)
    if not saved or time.time() - saved > max_age:
        return []
    return [Topic(**item) for item in entry.get("topics", [])]


def reddit_topics() -> list[Topic]:
    topics: list[Topic] = []
    for index, sub in enumerate(SUBREDDITS):
        url = f"https://www.reddit.com/r/{sub}/hot.rss"
        try:
            root = ET.fromstring(fetch_bytes(url))
        except Exception as exc:  # network and parse errors should not stop the run
            log(f"reddit skip r/{sub}: {exc}")
            continue
        for rank, entry in enumerate(root.findall(f"{ATOM}entry")):
            title = (entry.findtext(f"{ATOM}title") or "").strip()
            if not eligible_title(title):
                continue
            author_el = entry.find(f"{ATOM}author")
            author = ""
            if author_el is not None:
                author = (author_el.findtext(f"{ATOM}name") or "").lower()
            if author == "automoderator":
                continue
            link_el = entry.find(f"{ATOM}link")
            href = link_el.get("href") if link_el is not None else ""
            excerpt = html_to_text(entry.findtext(f"{ATOM}content") or "")[:900]
            published = entry.findtext(f"{ATOM}published") or ""
            if not product_topic(title) or not is_fresh(published):
                continue
            if len(excerpt) < 180:
                continue
            topics.append(
                Topic(
                    title=title,
                    url=href,
                    excerpt=excerpt,
                    source=f"r/{sub}",
                    score=int(recency_score(published) + 80),
                    published=published,
                )
            )
        time.sleep(0.4)
    return topics


def google_news_topics() -> list[Topic]:
    try:
        root = ET.fromstring(fetch_bytes(news_search_url()))
    except Exception as exc:
        log(f"google news skip: {exc}")
        cached = [topic for topic in recall_feed("google-news") if is_fresh(topic.published)]
        if cached:
            log(f"google news using {len(cached)} cached items from the last {FRESH_HOURS} hours")
        return cached
    topics: list[Topic] = []
    for item in root.findall("./channel/item"):
        raw_title = (item.findtext("title") or "").strip()
        title = re.sub(r"\s*\(review\)\s*$", "", strip_publisher(raw_title), flags=re.I).strip()
        published = item.findtext("pubDate") or ""
        if not eligible_title(title) or not product_topic(title) or OFF_TOPIC_RE.search(title):
            continue
        if not is_fresh(published):
            continue
        outlet = ""
        source_el = item.find("source")
        if source_el is not None and source_el.text:
            outlet = source_el.text.strip()
        excerpt = (
            f"Reported {published} by {outlet or 'a news outlet'}. "
            f"The headline is the available text: {title}."
        )
        topics.append(
            Topic(
                title=title,
                url=item.findtext("link") or "",
                excerpt=excerpt,
                source=f"Google News / {outlet}" if outlet else "Google News",
                score=int(recency_score(published)),
                published=published,
            )
        )
    topics.sort(key=lambda topic: topic.score, reverse=True)
    remember_feed("google-news", topics[:25])
    return topics[:25]


def google_topics() -> list[Topic]:
    url = "https://trends.google.com/trending/rss?geo=US"
    try:
        root = ET.fromstring(fetch_bytes(url))
    except Exception as exc:
        log(f"google trends skip: {exc}")
        return []
    topics: list[Topic] = []
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        if not eligible_title(title) or not NICHE_RE.search(title):
            continue
        traffic = item.findtext("ht:approx_traffic", default="0", namespaces=TRENDS_NS) or "0"
        digits = re.sub(r"[^0-9]", "", traffic) or "0"
        link = item.findtext("link") or ""
        news = item.find("ht:news_item", TRENDS_NS)
        excerpt = ""
        if news is not None:
            excerpt = html_to_text(news.findtext("ht:news_item_title", default="", namespaces=TRENDS_NS) or "")
        topics.append(
            Topic(
                title=title,
                url=link,
                excerpt=excerpt,
                source="Google Trends",
                score=1000 + int(digits),
            )
        )
    return topics


def bank_topics(seen: set[str]) -> list[Topic]:
    topics: list[Topic] = []
    for offset, (title, angle) in enumerate(TOPIC_BANK):
        if norm(title) in seen:
            continue
        topics.append(
            Topic(
                title=title,
                url="",
                excerpt=f"Evergreen smart-home question: {angle}.",
                source="topic bank",
                score=10 - offset,
            )
        )
    return topics


def choose_topic(seen: set[str]) -> Topic | None:
    news = google_news_topics()
    candidates = list(news)
    if len(news) < 3:
        candidates.extend(reddit_topics())
    fresh = [
        topic
        for topic in candidates
        if is_fresh(topic.published)
        and norm(topic.title) not in seen
        and (not topic.url or topic.url not in seen)
    ]
    fresh.sort(key=lambda topic: topic.score, reverse=True)
    if fresh:
        return fresh[0]
    log(f"no smart-home story from the last {FRESH_HOURS} hours")
    return None


def clean_model_text(text: str) -> str:
    text = re.sub(r"(?is)<think>.*?</think>", "", text).strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:markdown|md)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and lines[0].lstrip().startswith("#"):
        lines = lines[1:]
    headings = {
        "what was reported",
        "why it matters in a smart home",
        "what to check",
        "what not to assume",
    }
    repaired: list[str] = []
    for line in lines:
        label = line.strip().strip("*").strip().rstrip(":").lower()
        if label in headings:
            repaired.append(f"## {line.strip().strip('*').strip().rstrip(':')}")
        else:
            repaired.append(line)
    return "\n".join(repaired).strip()


def word_count(text: str) -> int:
    return len(re.findall(r"\b[\w']+\b", text))


def post_json(url: str, payload: dict, timeout: int) -> dict:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def draft_with_ollama(prompt: str) -> str:
    host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    model = os.environ.get("OLLAMA_MODEL", "qwen3-8b-hermes:latest")
    payload = {
        "model": model,
        "think": False,
        "stream": False,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You write practical smart-home buying and setup articles. "
                    "Never invent prices, test results, statistics, or model numbers "
                    "that were not supplied. If a fact is missing, say what the reader should verify."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "options": {"temperature": 0.4, "num_predict": 2200},
    }
    log(f"drafting with ollama model {model}")
    body = post_json(f"{host}/api/chat", payload, timeout=600)
    message = body.get("message") or {}
    return clean_model_text(message.get("content") or message.get("thinking") or "")


def draft_with_openai(prompt: str) -> str:
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    payload = {
        "model": model,
        "temperature": 0.4,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You write practical smart-home buying and setup articles. "
                    "Never invent prices, test results, statistics, or model numbers "
                    "that were not supplied. If a fact is missing, say what the reader should verify."
                ),
            },
            {"role": "user", "content": prompt},
        ],
    }
    log(f"drafting with openai model {model}")
    request = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
        },
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        body = json.loads(response.read().decode("utf-8"))
    return clean_model_text(body["choices"][0]["message"]["content"])


def title_keys(title: str) -> list[str]:
    stop = {
        "smart", "home", "about", "their", "which", "would", "there", "where",
        "these", "those", "light", "lights", "until", "after", "before", "whole",
    }
    words = [word.lower() for word in re.findall(r"[A-Za-z0-9']{5,}", title)]
    keys = [word for word in words if word not in stop]
    return keys or words


def stays_on_topic(title: str, body: str) -> bool:
    keys = title_keys(title)
    if not keys:
        return True
    lowered = body.lower()
    hits = sum(1 for word in keys if word in lowered)
    return hits >= min(2, len(keys))


def build_prompt(topic: Topic, expand: bool) -> str:
    correction = (
        "The previous draft drifted off the headline. Rewrite it so every section is about this story.\n"
        if expand
        else ""
    )
    return (
        f"{correction}"
        "Write 650-850 words of markdown about this specific story from the last 48 hours.\n"
        "Do not add a title heading.\n"
        f"Headline: {topic.title}\n"
        f"Published: {topic.published or 'within the last 48 hours'}\n"
        f"Source: {topic.source}\n"
        f"Known text:\n{topic.excerpt}\n\n"
        "Use these headings: What was reported, Why it matters in a smart home, What to check, What not to assume.\n"
        "The first paragraph may only state what the headline and known text support.\n"
        "After that, give the usual homeowner checks for this kind of problem, and label them as general checks, "
        "not as facts taken from the report.\n"
        "Do not invent the exact setting, firmware version, wattage, price, lumen number, or test result "
        "when it is not in the known text.\n"
        "Do not claim a hands-on test. Do not add affiliate links or a disclosure."
    )


def draft_article(topic: Topic) -> str:
    prompt = build_prompt(topic, expand=False)
    writer = draft_with_openai if os.environ.get("OPENAI_API_KEY") else draft_with_ollama
    text = writer(prompt)
    if word_count(text) < 400 or not stays_on_topic(topic.title, text):
        log("draft missed the headline; rewriting once")
        text = writer(build_prompt(topic, expand=True))
    if not stays_on_topic(topic.title, text):
        raise RuntimeError(f"draft is not about the trend: {topic.title}")
    return text


def render_article(topic: Topic, body: str, tag: str) -> tuple[str, str]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", body) if part.strip()]
    description_source = paragraphs[0] if paragraphs else topic.title
    description = re.sub(r"[#>*_`]", "", description_source)
    description = re.sub(r"\s+", " ", description).strip()[:155]
    if topic.url:
        safe_url = topic.url.replace("(", "%28").replace(")", "%29")
        source_line = f"[original discussion]({safe_url})"
    else:
        source_line = topic.source
    links = "\n".join(
        f"- [{label}]({amazon_url(query, tag)})" for label, query in related_queries(topic.title)
    )
    quote = ""
    if topic.excerpt and topic.source != "topic bank" and not topic.excerpt.startswith("Reported "):
        snippet = topic.excerpt[:500].strip()
        quote = "\n".join(f"> {line}" for line in snippet.splitlines() if line.strip())
        quote = f"\n{quote}\n"
    date = datetime.now().strftime("%Y-%m-%d")
    article = f"""---
title: {yaml_quote(topic.title)}
description: {yaml_quote(description)}
date: {date}
---

# {topic.title}

!!! note "Before you buy"
    This note was drafted with AI from a public trend ({topic.source}) and formatted for Smart Home Desk. It is not a hands-on lab test. Confirm compatibility, radio standard, and the return window on the manufacturer page. Product links may be affiliate links, which can pay for the time spent publishing. See the [disclosure](../disclosure.md).

**Source:** {source_line}
{quote}
{body}

## Gear to compare

Search current listings, then match the radio to the hub you already own:

{links}

Prices and stock move. The link is a search, not a promise that one brand is the winner.
"""
    return article, description


def post_files() -> list[tuple[str, str, str]]:
    found: list[tuple[str, str, str]] = []
    if not POSTS.exists():
        return found
    for path in POSTS.glob("*.md"):
        text = path.read_text(encoding="utf-8")
        title_match = re.search(r"(?m)^title:\s+\"(.*)\"\s*$", text)
        h1_match = re.search(r"(?m)^#\s+(.+)$", text)
        date_match = re.search(r"(?m)^date:\s+(\d{4}-\d{2}-\d{2})\s*$", text)
        title = (title_match.group(1) if title_match else h1_match.group(1) if h1_match else path.stem)
        title = title.replace('\\"', '"')
        date = date_match.group(1) if date_match else "1970-01-01"
        found.append((date, title, f"posts/{path.name}"))
    found.sort(key=lambda item: (item[0], item[2]), reverse=True)
    return found


def write_mkdocs(posts: list[tuple[str, str, str]]) -> None:
    post_lines = "\n".join(f"      - {yaml_quote(title)}: {rel}" for _date, title, rel in posts)
    if not post_lines:
        post_lines = "      - " + yaml_quote("Posts will appear here") + ": index.md"
    content = f"""site_name: Smart Home Desk
site_description: Practical smart-home buying and setup notes based on what people are asking about right now.
site_url: https://superman7028-tech.github.io/smart-home-desk/
docs_dir: docs
theme:
  name: material
  palette:
    - scheme: default
      primary: teal
      accent: teal
      toggle:
        icon: material/weather-night
        name: Switch to dark mode
    - scheme: slate
      primary: teal
      accent: teal
      toggle:
        icon: material/weather-sunny
        name: Switch to light mode
  features:
    - navigation.tracking
    - toc.follow
markdown_extensions:
  - admonition
  - tables
  - toc:
      permalink: true
plugins:
  - search
nav:
  - Home: index.md
  - Gear: gear.md
  - Disclosure: disclosure.md
  - Posts:
{post_lines}
"""
    (ROOT / "mkdocs.yml").write_text(content, encoding="utf-8")


def replace_block(path: Path, start: str, end: str, inner: str) -> None:
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.S)
    replacement = f"{start}\n{inner.rstrip()}\n{end}"
    updated, count = pattern.subn(replacement, text, count=1)
    if count != 1:
        raise RuntimeError(f"missing marker block in {path}")
    path.write_text(updated, encoding="utf-8")


def write_latest(posts: list[tuple[str, str, str]]) -> None:
    if posts:
        lines = "\n".join(f"- [{title}]({rel})" for _date, title, rel in posts[:8])
    else:
        lines = "- The first post is on its way."
    replace_block(DOCS / "index.md", "<!-- LATEST:START -->", "<!-- LATEST:END -->", lines)


def write_affiliate_links(tag: str) -> None:
    lines = "\n".join(f"- [{label}]({amazon_url(query, tag)})" for label, query in DEFAULT_QUERIES)
    replace_block(DOCS / "gear.md", "<!-- AFFILIATE:START -->", "<!-- AFFILIATE:END -->", lines)


def build_site() -> None:
    mkdocs = ROOT / ".venv" / "bin" / "mkdocs"
    if not mkdocs.exists():
        log("mkdocs venv not installed; skipped site build")
        return
    import subprocess

    completed = subprocess.run(
        [str(mkdocs), "build", "--strict"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if completed.returncode != 0:
        log(completed.stdout)
        log(completed.stderr)
        raise RuntimeError("mkdocs build failed")
    log("mkdocs build wrote site/")
    publish_site()


def publish_site() -> None:
    import subprocess

    if not (ROOT / ".git").exists():
        log("no git repo; skipped publish")
        return
    remote = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if remote.returncode != 0:
        log("no origin remote; skipped publish")
        return
    mkdocs = ROOT / ".venv" / "bin" / "mkdocs"
    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", "superman7028-tech")
    env.setdefault("GIT_COMMITTER_NAME", env["GIT_AUTHOR_NAME"])
    env.setdefault("GIT_AUTHOR_EMAIL", "superman7028-tech@users.noreply.github.com")
    env.setdefault("GIT_COMMITTER_EMAIL", env["GIT_AUTHOR_EMAIL"])
    completed = subprocess.run(
        [str(mkdocs), "gh-deploy", "--force"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )
    if completed.returncode != 0:
        log(completed.stdout)
        log(completed.stderr)
        log("publish failed; local site was still built")
        return
    log("published https://superman7028-tech.github.io/smart-home-desk/")


def acquire_lock():
    DATA.mkdir(parents=True, exist_ok=True)
    handle = LOCK_PATH.open("w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def too_soon(force: bool) -> bool:
    if force:
        return False
    state = load_json(STATE_PATH, {})
    last = float(state.get("last_post_epoch") or 0)
    elapsed = time.time() - last
    if last and elapsed < MIN_GAP_SECONDS:
        log(f"skip: last post was {int(elapsed / 60)} minutes ago")
        return True
    return False


def unique_path(title: str) -> Path:
    POSTS.mkdir(parents=True, exist_ok=True)
    date = datetime.now().strftime("%Y-%m-%d")
    base = POSTS / f"{date}-{slugify(title)}.md"
    path = base
    counter = 2
    while path.exists():
        path = POSTS / f"{date}-{slugify(title)}-{counter}.md"
        counter += 1
    return path


def generate(force: bool, dry_run: bool) -> int:
    load_env(ROOT / ".env")
    setup_logging()
    lock = acquire_lock()
    if lock is None:
        log("skip: another generate_post run is active")
        return 0
    try:
        if too_soon(force):
            return 0
        seen_payload = load_json(SEEN_PATH, {"titles": [], "urls": []})
        seen = set(seen_payload.get("titles", [])) | set(seen_payload.get("urls", []))
        topic = choose_topic(seen)
        if topic is None:
            log("no unused topics available")
            return 1
        log(f"topic [{topic.source}] {topic.title}")
        if dry_run:
            return 0
        body = draft_article(topic)
        if word_count(body) < 350:
            log(f"refusing short draft ({word_count(body)} words)")
            return 1
        tag = os.environ.get("AMAZON_ASSOCIATE_TAG", "").strip()
        article, _description = render_article(topic, body, tag)
        path = unique_path(topic.title)
        path.write_text(article, encoding="utf-8")
        titles = list(dict.fromkeys([*seen_payload.get("titles", []), norm(topic.title)]))
        urls = list(seen_payload.get("urls", []))
        if topic.url:
            urls = list(dict.fromkeys([*urls, topic.url]))
        save_json(SEEN_PATH, {"titles": titles, "urls": urls})
        posts = post_files()
        write_mkdocs(posts)
        write_latest(posts)
        write_affiliate_links(tag)
        save_json(
            STATE_PATH,
            {"last_post_epoch": time.time(), "last_file": str(path), "title": topic.title},
        )
        build_site()
        log(f"created {path}")
        print(f"created {path}")
        return 0
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Add one trending smart-home post")
    parser.add_argument("--force", action="store_true", help="ignore the 5-hour gap")
    parser.add_argument("--dry-run", action="store_true", help="choose a topic and exit")
    args = parser.parse_args()
    try:
        return generate(force=args.force, dry_run=args.dry_run)
    except Exception as exc:
        setup_logging()
        log(f"error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
