"""Source discovery agent: finds secondary pages from a homepage.

Discovers pricing, products, careers, partners, promotions, about, impact/CSR,
and blog pages. High-value kinds are auto-attached to extra_sources so the next
(and current, if caller reloads extras) deep scrape covers them. Still records
pending rows for transparency on the competitor page.
"""

from __future__ import annotations

import logging
from typing import Dict, List
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import db
from agents.deep_scan_agent import PAGE_KINDS
from agents.http_text import decode_response_text, safe_accept_encoding

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Encoding": safe_accept_encoding(),
}

# Auto-track these so deep scans run without waiting for manual accept.
_AUTO_TRACK_KINDS = {
    "pricing", "products", "careers", "partners", "promotions", "about", "impact", "blog",
}
_MAX_AUTO_EXTRAS = 8


class SourceDiscoveryAgent:
    """Discover secondary pages; auto-attach high-value ones for deep scraping."""

    def discover(self, competitor_id: int, homepage_url: str) -> List[Dict]:
        found: List[Dict] = []
        parsed = urlparse(homepage_url)
        root = f"{parsed.scheme}://{parsed.netloc}"

        for kind, (paths, _keywords) in PAGE_KINDS.items():
            for path in paths:
                candidate = root + path
                if self._looks_alive(candidate):
                    found.append({"url": candidate, "kind": kind})
                    break

        link_map = self._scan_homepage_links(homepage_url)
        for kind, (_paths, keywords) in PAGE_KINDS.items():
            if any(f["kind"] == kind for f in found):
                continue
            for url, text in link_map.items():
                blob = f"{url} {text}".lower()
                if any(kw in blob for kw in keywords):
                    found.append({"url": url, "kind": kind})
                    break

        already = set(u.rstrip("/") for u in db.get_extra_sources(competitor_id))
        already.add(homepage_url.rstrip("/"))
        auto_budget = max(0, _MAX_AUTO_EXTRAS - len(already) + 1)

        offered: List[Dict] = []
        for item in found:
            url = item["url"].rstrip("/")
            if url in already or any(url == a for a in already):
                continue
            source_id = db.add_discovered_source(competitor_id, item["url"], item["kind"])
            auto = item["kind"] in _AUTO_TRACK_KINDS and auto_budget > 0
            if auto:
                db.add_extra_source(competitor_id, item["url"])
                if source_id:
                    try:
                        db.resolve_discovered_source(source_id, "accepted")
                    except Exception:
                        pass
                auto_budget -= 1
                already.add(url)
                logger.info(
                    "Auto-tracking %s page for competitor %s: %s",
                    item["kind"], competitor_id, item["url"],
                )
            if source_id:
                offered.append({**item, "id": source_id, "auto_tracked": auto})
        return offered

    def _looks_alive(self, url: str) -> bool:
        try:
            response = requests.get(
                url, headers=_HEADERS, timeout=8, allow_redirects=True, stream=True
            )
            content_type = response.headers.get("Content-Type", "")
            final = response.url
            response.close()
            if response.status_code != 200 or "text/html" not in content_type:
                return False
            path = urlparse(final).path.rstrip("/")
            return path not in ("", "/")
        except requests.exceptions.RequestException:
            return False

    def _scan_homepage_links(self, url: str) -> Dict[str, str]:
        html = ""
        try:
            response = requests.get(url, headers=_HEADERS, timeout=10)
            if response.status_code == 200:
                html = decode_response_text(response)
        except requests.exceptions.RequestException:
            html = ""
        if not html or len(html) < 400:
            from agents.browser_fetch import fetch_impersonated
            html = fetch_impersonated(url) or html
        if not html:
            return {}
        soup = BeautifulSoup(html, "html.parser")
        host = urlparse(url).netloc
        links: Dict[str, str] = {}
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.startswith("#") or href.startswith("mailto:"):
                continue
            absolute = urljoin(url, href)
            if urlparse(absolute).netloc != host:
                continue
            text = a.get_text(" ", strip=True)[:80]
            links[absolute.split("?")[0].split("#")[0]] = text
        return links
