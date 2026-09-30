"""Source discovery agent: finds secondary pages from a homepage.

Discovers pricing, products, careers (incl. openings), partners, promotions,
about, impact/CSR, blog, press/newsroom, and (cautiously) public review
directory pages. High-value kinds are auto-attached to extra_sources so the
same run's deep scrape covers them.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Set
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import db
from agents.deep_scan_agent import PAGE_KINDS
from agents.http_text import decode_response_text, safe_accept_encoding
from agents.search_provider import discover_source

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
    "pricing", "products", "careers", "partners", "promotions",
    "about", "impact", "blog", "press", "reviews",
}
_MAX_AUTO_EXTRAS = 14
# LinkedIn company pages require login for useful content — skip rather than
# work around access restrictions (terms).
_SKIP_DISCOVERY_HOSTS = (
    "linkedin.com", "facebook.com", "twitter.com", "x.com", "instagram.com",
)


class SourceDiscoveryAgent:
    """Discover secondary pages; auto-attach high-value ones for deep scraping."""

    def discover(self, competitor_id: int, homepage_url: str) -> List[Dict]:
        found: List[Dict] = []
        parsed = urlparse(homepage_url)
        root = f"{parsed.scheme}://{parsed.netloc}"
        host = parsed.netloc.lower().replace("www.", "")

        # 1) Path probes on the competitor host (pricing / careers / press first).
        priority_kinds = (
            "pricing", "careers", "press", "blog", "products", "partners",
            "promotions", "about", "impact",
        )
        for kind in priority_kinds:
            paths, _keywords = PAGE_KINDS.get(kind, ((), ()))
            for path in paths:
                candidate = root + path
                if self._looks_alive(candidate):
                    found.append({"url": candidate, "kind": kind})
                    break

        # 2) Homepage link scan for kinds still missing.
        link_map = self._scan_homepage_links(homepage_url)
        for kind, (_paths, keywords) in PAGE_KINDS.items():
            if kind == "reviews" or any(f["kind"] == kind for f in found):
                continue
            for url, text in link_map.items():
                blob = f"{url} {text}".lower()
                if any(kw in blob for kw in keywords):
                    found.append({"url": url, "kind": kind})
                    break

        # 3) Explicit web search for thin / external kinds (pricing, careers, press, reviews).
        brand = self._brand_name(competitor_id, homepage_url)
        found.extend(self._search_supplement(brand, host, found))

        already = set(u.rstrip("/") for u in db.get_extra_sources(competitor_id))
        already.add(homepage_url.rstrip("/"))
        auto_budget = max(0, _MAX_AUTO_EXTRAS - len(already) + 1)

        offered: List[Dict] = []
        for item in found:
            url = item["url"].rstrip("/")
            if self._should_skip_host(url):
                continue
            if url in already or any(url == a for a in already):
                continue
            try:
                source_id = db.add_discovered_source(competitor_id, item["url"], item["kind"])
            except Exception:
                logger.exception(
                    "Failed to record discovered source %s for competitor %s",
                    item.get("url"), competitor_id,
                )
                continue
            auto = item["kind"] in _AUTO_TRACK_KINDS and auto_budget > 0
            if auto:
                try:
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
                except Exception:
                    logger.exception(
                        "Failed to auto-track %s for competitor %s",
                        item.get("url"), competitor_id,
                    )
            if source_id:
                offered.append({**item, "id": source_id, "auto_tracked": auto})
        return offered

    def _brand_name(self, competitor_id: int, homepage_url: str) -> str:
        try:
            row = db.get_competitor(competitor_id)
            if row and row["name"]:
                return str(row["name"]).strip()
        except Exception:
            pass
        host = urlparse(homepage_url).netloc.replace("www.", "")
        return host.split(".")[0] if host else "company"

    @staticmethod
    def _should_skip_host(url: str) -> bool:
        host = urlparse(url).netloc.lower()
        return any(skip in host for skip in _SKIP_DISCOVERY_HOSTS)

    def _search_supplement(
        self, brand: str, host: str, already: List[Dict]
    ) -> List[Dict]:
        """Extra discovery via the existing search provider — failures are skipped."""
        have: Set[str] = {f["kind"] for f in already}
        have_urls = {f["url"].rstrip("/") for f in already}
        extras: List[Dict] = []

        queries = []
        if "pricing" not in have:
            queries.append(("pricing", f"{brand} pricing site:{host}"))
            queries.append(("pricing", f"{brand} pricing plans"))
        if "careers" not in have:
            queries.append(("careers", f"{brand} careers jobs site:{host}"))
            queries.append(("careers", f"{brand} open roles hiring"))
        if "press" not in have:
            queries.append(("press", f"{brand} newsroom OR press site:{host}"))
        # Public review directories only — structural themes later, never verbatim quotes.
        queries.append(("reviews", f"{brand} site:g2.com"))
        queries.append(("reviews", f"{brand} site:capterra.com"))

        for kind, query in queries:
            if kind in have and kind != "pricing":
                continue
            try:
                hits = discover_source(query, limit=3) or []
            except Exception:
                logger.warning("Search supplement failed for %s (%s)", kind, query[:60])
                continue
            for hit in hits:
                url = (hit.get("url") or "").strip().rstrip("/")
                if not url or url in have_urls:
                    continue
                if self._should_skip_host(url):
                    continue
                hit_host = urlparse(url).netloc.lower()
                if kind in ("pricing", "careers", "press"):
                    if host and host not in hit_host and not hit_host.endswith(host):
                        continue
                if kind == "reviews":
                    if "g2.com" not in hit_host and "capterra.com" not in hit_host:
                        continue
                    path = urlparse(url).path.lower()
                    if not any(p in path for p in ("/products/", "/software/", "/p/")):
                        continue
                try:
                    alive = self._looks_alive(url)
                except Exception:
                    alive = False
                if not alive:
                    continue
                extras.append({"url": url, "kind": kind})
                have.add(kind)
                have_urls.add(url)
                break
        return extras

    def _looks_alive(self, url: str) -> bool:
        try:
            response = requests.get(
                url, headers=_HEADERS, timeout=12, allow_redirects=True, stream=True
            )
            content_type = response.headers.get("Content-Type", "")
            final = response.url
            status = response.status_code
            response.close()
            if status != 200 or "text/html" not in content_type:
                return False
            # Homepage path OK for review sites; for on-host pages require a subpath
            path = urlparse(final).path.rstrip("/")
            host = urlparse(final).netloc.lower()
            if "g2.com" in host or "capterra.com" in host:
                return bool(path)
            return path not in ("", "/")
        except requests.exceptions.RequestException:
            return False

    def _scan_homepage_links(self, url: str) -> Dict[str, str]:
        html = ""
        try:
            response = requests.get(url, headers=_HEADERS, timeout=14)
            if response.status_code == 200:
                html = decode_response_text(response)
        except requests.exceptions.RequestException:
            html = ""
        if not html or len(html) < 400:
            try:
                from agents.browser_fetch import fetch_impersonated
                html = fetch_impersonated(url) or html
            except Exception:
                pass
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
