"""
app.py — Zim RSS Scraper API
Deployable on Render as a web service.

Endpoints:
  GET  /              — health check
  GET  /scrape        — scrape all feeds (returns JSON)
  GET  /scrape?feed=herald   — scrape a single feed by name (case-insensitive)
  GET  /scrape?limit=3       — override articles per feed (default 5, max 10)
"""

import os
import re
import time
import random
import logging
import requests
import urllib3
import feedparser
from bs4 import BeautifulSoup
from urllib.parse import urlparse
from flask import Flask, jsonify, request

try:
    import cloudscraper
    _cloudscraper = cloudscraper.create_scraper()
except ImportError:
    _cloudscraper = None

urllib3.disable_warnings()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

RSS_FEEDS = [
    {"name": "New Zimbabwe", "url": "https://www.newzimbabwe.com/feed/"},
    {"name": "Herald",       "url": "https://www.herald.co.zw/feed/"},
    {"name": "Pindula News", "url": "https://news.pindula.co.zw/feed/"},
    {"name": "ZimLive",      "url": "https://www.zimlive.com/feed/"},
]

DEFAULT_ARTICLES_PER_FEED = int(os.environ.get("ARTICLES_PER_FEED", 2))
MAX_ARTICLES_PER_FEED = 3

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

SITE_CONFIGS = {
    "herald.co.zw": {
        "selectors": ["div.jeg_post_content", "div.entry-content", "div.td-post-content"],
        "remove":    ["div.jeg_ad", ".sharedaddy", ".jp-relatedposts", ".related-posts", ".wp-caption-text"],
    },
    "newzimbabwe.com": {
        "selectors": ["div.post-body", "div#mvp-content-main", "div.entry-content", "div.article-body"],
        "remove":    [".mvp-related-posts", ".mvpfd_below", "div[class*='ad']", ".share-container"],
    },
    "news.pindula.co.zw": {
        "selectors": ["div.entry-content", "div.post-content", "div.td-post-content"],
        "remove":    [".pindula-related", ".sharedaddy", ".jp-relatedposts", "div[id*='ad']"],
    },
    "zimlive.com": {
        "selectors": ["div.entry-content", "div.jeg_post_content", "div.post-content"],
        "remove":    [".jeg_ad", ".sharedaddy", ".jp-relatedposts", "div[class*='ad']", ".author-box"],
    },
}

DEFAULT_SELECTORS = ["article", "div.entry-content", "div.post-content", "div.article-body", "main"]
DEFAULT_REMOVE    = ["nav", "header", "footer", "aside", "div[class*='ad']", ".related", ".share"]

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def get_headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

def rss_headers(url=""):
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else ""
    h = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Cache-Control": "max-age=0",
    }
    if origin:
        h["Referer"] = origin
    return h

def get_site_config(url):
    host = urlparse(url).netloc.lower().lstrip("www.")
    for domain, cfg in SITE_CONFIGS.items():
        if domain in host:
            return cfg
    return {"selectors": DEFAULT_SELECTORS, "remove": DEFAULT_REMOVE}

def clean_text(text):
    replacements = {
        "\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"',
        "\u2026": "...", "\u00a0": " ", "\u2013": "-", "\u2014": "--"
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return " ".join(text.split()).strip()

def is_valid_paragraph(text):
    if not text or len(text) < 30:
        return False
    skip = [
        "advertisement", "sponsored", "click here", "read also", "share this",
        "subscribe", "newsletter", "sign up", "follow us", "related posts",
        "you may also like", "leave a comment", "your email", "required fields",
        "previous article", "next article", "tags:", "category:"
    ]
    lower = text.lower()
    if any(s in lower for s in skip):
        return False
    if text.isupper() and len(text) < 120:
        return False
    if re.match(r"^[\d\W]+$", text):
        return False
    return True

def extract_thumbnail(soup):
    def valid(src):
        if not src or src.startswith("data:"):
            return False
        low = src.lower()
        return not any(x in low for x in [
            ".gif", "logo", "icon", "avatar", "spinner",
            "pixel", "track", "blank", "placeholder"
        ])

    og = soup.find("meta", property="og:image")
    if og and valid(og.get("content", "")):
        return og["content"].strip()

    tw = soup.find("meta", {"name": "twitter:image"})
    if tw and valid(tw.get("content", "")):
        return tw["content"].strip()

    wp = soup.find("img", class_="wp-post-image")
    if wp and valid(wp.get("src", "")):
        return wp["src"].strip()

    for style_tag in soup.find_all("style"):
        txt = style_tag.string or ""
        m = re.search(r"background(?:-image)?:\s*url\(([^)]+)\)", txt, re.I)
        if m:
            raw = m.group(1).strip().strip("'\"")
            if raw.startswith("http") and valid(raw):
                return raw

    for img in soup.find_all("img", src=True):
        src = img["src"].strip()
        if valid(src) and re.search(r"/(uploads|wp-content)/", src, re.I):
            return src

    return ""

def fetch_with_fallback(url, use_rss_headers=False, timeout=15):
    h = rss_headers(url) if use_rss_headers else get_headers()
    if _cloudscraper:
        try:
            resp = _cloudscraper.get(url, headers=h, timeout=timeout)
            resp.raise_for_status()
            return resp
        except Exception as e:
            log.warning(f"cloudscraper failed for {url}: {e} — falling back to requests")
    session = requests.Session()
    session.verify = False
    resp = session.get(url, headers=h, timeout=timeout)
    resp.raise_for_status()
    return resp

# ---------------------------------------------------------------------------
# CORE LOGIC
# ---------------------------------------------------------------------------

def fetch_rss(feed_url, limit=5):
    parsed = urlparse(feed_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    urls_to_try = [
        feed_url,
        f"{base}/?feed=rss2",
        f"{base}/rss",
        f"{base}/rss.xml",
    ]

    for url in urls_to_try:
        try:
            resp = fetch_with_fallback(url, use_rss_headers=True, timeout=15)
            feed = feedparser.parse(resp.text)
            if not feed.entries:
                continue
            articles = []
            for entry in feed.entries[:limit]:
                thumbnail = ""
                if hasattr(entry, "media_thumbnail") and entry.media_thumbnail:
                    thumbnail = entry.media_thumbnail[0].get("url", "")
                elif hasattr(entry, "enclosures") and entry.enclosures:
                    for enc in entry.enclosures:
                        if enc.get("type", "").startswith("image/"):
                            thumbnail = enc.get("href", "")
                            break
                if not thumbnail:
                    for c in entry.get("content", [{}]):
                        m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', c.get("value", ""))
                        if m:
                            src = m.group(1)
                            if re.search(r"\.(jpg|jpeg|png|webp)", src, re.I):
                                thumbnail = src
                                break
                articles.append({
                    "title":     entry.get("title", "").strip(),
                    "url":       entry.get("link", ""),
                    "published": getattr(entry, "published", "") or getattr(entry, "updated", ""),
                    "thumbnail": thumbnail,
                })
            if articles:
                return articles
        except Exception as e:
            log.warning(f"RSS attempt failed ({url}): {e}")

    return []

def scrape_article(url):
    cfg = get_site_config(url)
    try:
        resp = fetch_with_fallback(url, use_rss_headers=False, timeout=20)
        html = resp.text
    except Exception as e:
        return {"url": url, "success": False, "content": "", "error": str(e)}

    soup = BeautifulSoup(html, "lxml")

    for sel in cfg["remove"]:
        for el in soup.select(sel):
            el.decompose()
    for tag in soup(["script", "style", "noscript", "iframe", "form", "button"]):
        tag.decompose()

    content = ""
    for selector in cfg["selectors"]:
        container = soup.select_one(selector)
        if not container:
            continue
        paragraphs = [clean_text(p.get_text()) for p in container.find_all(["p", "h2", "h3", "blockquote"])]
        paragraphs = [p for p in paragraphs if is_valid_paragraph(p)]
        if len(paragraphs) >= 3:
            content = "\n\n".join(paragraphs)
            break

    if not content:
        paragraphs = [clean_text(p.get_text()) for p in soup.find_all("p")]
        paragraphs = [p for p in paragraphs if is_valid_paragraph(p)]
        content = "\n\n".join(paragraphs)

    thumbnail = extract_thumbnail(soup)

    title = ""
    og = soup.find("meta", property="og:title")
    if og:
        title = clean_text(og.get("content", ""))
    if not title:
        h1 = soup.find("h1")
        if h1:
            title = clean_text(h1.get_text())

    return {
        "url":        url,
        "success":    bool(content and len(content.split()) > 30),
        "title":      title,
        "content":    content,
        "thumbnail":  thumbnail,
        "word_count": len(content.split()) if content else 0,
    }

def run_scrape(feeds, limit):
    results = []
    for feed_cfg in feeds:
        name = feed_cfg["name"]
        log.info(f"Fetching RSS: {name}")
        articles = fetch_rss(feed_cfg["url"], limit=limit)
        if not articles:
            log.warning(f"No articles found for {name}")
            continue
        for article in articles:
            url = article["url"]
            log.info(f"Scraping: {url}")
            time.sleep(random.uniform(1.0, 2.5))
            result = scrape_article(url)
            result["source"]    = name
            result["published"] = article["published"]
            if not result.get("thumbnail"):
                result["thumbnail"] = article.get("thumbnail", "")
            results.append(result)
    return results

# ---------------------------------------------------------------------------
# ROUTES
# ---------------------------------------------------------------------------

@app.route("/")
def health():
    return jsonify({
        "status": "ok",
        "cloudscraper": _cloudscraper is not None,
        "feeds": [f["name"] for f in RSS_FEEDS],
        "usage": {
            "scrape_all":        "/scrape",
            "scrape_one_feed":   "/scrape?feed=herald",
            "custom_limit":      "/scrape?limit=3",
        }
    })

@app.route("/scrape")
def scrape():
    # ?feed= filter
    feed_filter = request.args.get("feed", "").strip().lower()
    if feed_filter:
        feeds = [f for f in RSS_FEEDS if feed_filter in f["name"].lower()]
        if not feeds:
            return jsonify({"error": f"No feed matched '{feed_filter}'", "available": [f["name"] for f in RSS_FEEDS]}), 404
    else:
        feeds = RSS_FEEDS

    # ?limit= override
    try:
        limit = min(int(request.args.get("limit", DEFAULT_ARTICLES_PER_FEED)), MAX_ARTICLES_PER_FEED)
    except ValueError:
        limit = DEFAULT_ARTICLES_PER_FEED

    results = run_scrape(feeds, limit)
    success_count = sum(1 for r in results if r.get("success"))

    return jsonify({
        "total":   len(results),
        "success": success_count,
        "failed":  len(results) - success_count,
        "articles": results,
    })

# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
