"""News Agent: pulls recent public news about a competitor for PMs.

Fetches Google News RSS (no API key) and classifies each headline into
funding / marketing campaign / partnership / product launch / engagement.
Does not call the LLM — keeps Groq quota for summarize/classify/sections.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional
from urllib.parse import quote_plus, urlparse

import requests

from agents.categories import SIGNAL_LABELS

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

_SIGNAL_KEYWORDS = {
    "funding": (
        "funding", "raises", "raised", "series a", "series b", "series c",
        "valuation", "ipo", "investment", "invests", "venture", "seed round",
    ),
    "marketing_campaign": (
        "campaign", "ad campaign", "advertising", "marketing", "commercial",
        "brand campaign", "super bowl", "sponsorship", "ambassador",
    ),
    "partnership": (
        "partner", "partnership", "collab", "collaboration", "ties up",
        "joins forces", "acquisition", "acquires", "merger",
    ),
    "product_launch": (
        "launch", "launches", "unveils", "introduces", "releases", "debut",
        "new product", "new collection",
    ),
    "engagement": (
        "users", "members", "community", "engagement", "downloads",
        "subscribers", "followers", "app users", "loyalty",
    ),
}


class NewsAgent:
    """Collect and classify recent news headlines for a competitor."""

    def __init__(self, max_items: int = 8) -> None:
        self.max_items = max_items
        self.last_error: Optional[str] = None

    @staticmethod
    def _brand_query(name: str, url: str) -> str:
        brand = (name or "").strip()
        if not brand or brand.lower().startswith("http"):
            host = urlparse(url or "").netloc.replace("www.", "")
            brand = host.split(".")[0] if host else "company"
        # Drop trailing junk like long ad titles
        brand = brand.split("?")[0][:60]
        return brand

    @staticmethod
    def classify_headline(title: str) -> str:
        lower = (title or "").lower()
        for signal, keywords in _SIGNAL_KEYWORDS.items():
            if any(kw in lower for kw in keywords):
                return signal
        return "other"

    def _fetch_rss(self, query: str) -> List[Dict]:
        # when:14d keeps results recent; hl/gl bias to English.
        rss_url = (
            "https://news.google.com/rss/search?"
            f"q={quote_plus(query + ' when:14d')}&hl=en-US&gl=US&ceid=US:en"
        )
        try:
            response = requests.get(rss_url, headers=_HEADERS, timeout=15)
            response.raise_for_status()
        except requests.exceptions.RequestException as exc:
            self.last_error = f"news fetch failed ({exc.__class__.__name__})"
            logger.warning("News RSS failed for %s: %s", query, exc.__class__.__name__)
            return []

        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as exc:
            self.last_error = f"news XML parse error ({exc})"
            return []

        items: List[Dict] = []
        for item in root.findall("./channel/item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            source_el = item.find("source")
            source = (source_el.text or "").strip() if source_el is not None else ""
            if not title:
                continue
            # Google titles often look like "Headline - Publisher"
            clean_title = re.sub(r"\s+-\s+[^-]+$", "", title).strip() or title
            signal = self.classify_headline(clean_title + " " + title)
            items.append({
                "signal_type": signal,
                "signal_label": SIGNAL_LABELS.get(signal, "News"),
                "title": clean_title[:240],
                "url": link,
                "summary": f"{source} · {pub}".strip(" ·") if source or pub else "",
                "source": source,
            })
            if len(items) >= self.max_items:
                break
        return items

    def fetch(self, competitor_name: str = "", url: str = "") -> List[Dict]:
        """Return up to max_items news signals about the competitor."""
        self.last_error = None
        brand = self._brand_query(competitor_name, url)
        # Bias toward PM-relevant news without excluding general coverage.
        query = f'{brand} (funding OR campaign OR partnership OR launch OR users OR marketing)'
        items = self._fetch_rss(query)
        if not items:
            # Broader fallback
            items = self._fetch_rss(brand)
        if not items and not self.last_error:
            self.last_error = "no recent news found"
        return items
