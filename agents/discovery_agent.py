"""Discovery Agent: resolve a brand/company name into official sources.

Works even when big sites (Amazon, etc.) block scrapers with HTTP 202/403:
we soft-accept brand-matching domains and use a known-brand homepage map.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from agents.http_text import decode_response_text, safe_accept_encoding
from agents.known_brands import known_site_for
from agents.news_agent import NewsAgent
from agents.search_provider import discover_source, guess_brand_domains
from url_utils import normalize_url

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": safe_accept_encoding(),
}

_SKIP_HOST_FRAGMENTS = (
    "wikipedia.org", "wikidata.org", "linkedin.com", "facebook.com",
    "twitter.com", "x.com", "instagram.com", "youtube.com", "youtu.be",
    "crunchbase.com", "bloomberg.com", "reddit.com", "medium.com",
    "github.com", "play.google.com", "apps.apple.com", "duckduckgo.com",
    "google.com", "bing.com", "glassdoor.com", "britannica.com",
    "forbes.com", "techcrunch.com",
)

# Paths that are product/deep links — never treat as the company homepage.
_BAD_PATH_MARKERS = (
    "/dp/", "/gp/", "/products/", "/p/", "/item/", "/ip/",
    "/watch?", "/shorts/", "/reel/", "/status/",
)

_TWITTER_RE = re.compile(
    r"(?:twitter\.com|x\.com)/(@?[A-Za-z0-9_]{1,15})(?:/|$|\?)", re.I
)
_IG_RE = re.compile(r"instagram\.com/([A-Za-z0-9_.]{1,30})(?:/|$|\?)", re.I)
_YT_RE = re.compile(
    r"(youtube\.com/(?:@[\w.-]+|channel/[\w-]+|c/[\w.-]+|user/[\w.-]+)(?:/[^\s\"']*)?)",
    re.I,
)

_SOFT_OK_STATUSES = {200, 201, 202, 203, 301, 302, 303, 307, 308, 401, 403, 429, 503}


class DiscoveryAgent:
    """Name or URL → verified website + socials + recent news headlines."""

    def __init__(self) -> None:
        self.news = NewsAgent(max_items=5)
        self.last_error: Optional[str] = None

    @staticmethod
    def _normalize_handle(raw: str) -> str:
        return (raw or "").strip().lstrip("@").split("/")[0].split("?")[0]

    @staticmethod
    def _brand_tokens(name: str) -> List[str]:
        cleaned = re.sub(r"[^a-z0-9\s]", " ", (name or "").lower())
        stop = {"inc", "ltd", "llc", "corp", "co", "company", "the", "official"}
        return [t for t in cleaned.split() if len(t) > 1 and t not in stop]

    @staticmethod
    def _brand_slug(name: str) -> str:
        return "".join(DiscoveryAgent._brand_tokens(name))

    @staticmethod
    def _looks_like_url(text: str) -> bool:
        t = (text or "").strip()
        if not t:
            return False
        if t.startswith(("http://", "https://", "www.")):
            return True
        return bool(re.match(r"^[a-z0-9-]+(\.[a-z0-9-]+)+(/.*)?$", t, re.I))

    @staticmethod
    def _is_homepage_like(url: str) -> bool:
        lower = (url or "").lower()
        if any(m in lower for m in _BAD_PATH_MARKERS):
            return False
        path = urlparse(url).path or "/"
        if path in ("", "/"):
            return True
        # Allow short marketing paths like /software/jira
        parts = [p for p in path.split("/") if p]
        return len(parts) <= 2 and all(len(p) < 40 for p in parts)

    def _host_ok(self, url: str) -> bool:
        host = urlparse(url).netloc.lower().replace("www.", "")
        if not host:
            return False
        return not any(skip in host for skip in _SKIP_HOST_FRAGMENTS)

    def _host_matches_brand(self, url: str, name: str) -> bool:
        host = urlparse(url).netloc.lower().replace("www.", "")
        compact = host.replace("-", "").replace(".", "")
        slug = self._brand_slug(name)
        if not slug or len(slug) < 2:
            return False
        # amazon.com, amazon.in, amazon.co.uk — host starts with brand
        host_base = host.split(".")[0].replace("-", "")
        return slug == host_base or slug in compact[: len(slug) + 2]

    def _probe(self, url: str) -> Tuple[Optional[int], Optional[str], str]:
        """Return (status, final_url, html_or_empty). Never raises."""
        try:
            response = requests.get(
                url, headers=_HEADERS, timeout=12, allow_redirects=True,
            )
            html = decode_response_text(response) if response.status_code == 200 else ""
            return response.status_code, response.url, html
        except requests.exceptions.RequestException as exc:
            logger.info("Probe failed for %s: %s", url, exc.__class__.__name__)
            return None, None, ""

    def _mentions_brand(self, html: str, name: str, url: str = "") -> bool:
        if self._host_matches_brand(url, name):
            return True
        tokens = self._brand_tokens(name)
        if not tokens or not html:
            return False
        soup = BeautifulSoup(html, "html.parser")
        title = (soup.title.get_text(" ", strip=True) if soup.title else "").lower()
        text = soup.get_text(" ", strip=True).lower()[:8000]
        phrase = " ".join(tokens)
        if phrase in title or phrase in text:
            return True
        if len(tokens) == 1:
            return tokens[0] in title or tokens[0] in text
        return all(t in text for t in tokens)

    def _accept(
        self,
        url: str,
        name: str,
        *,
        title: str = "",
        note: str = "",
        confidence: str = "high",
        html: str = "",
    ) -> Dict:
        return {
            "status": "found",
            "url": normalize_url(url) or url,
            "title": title or name,
            "confidence": confidence,
            "html": html or None,
            "note": note,
        }

    def _dns_resolves(self, url: str) -> bool:
        """True when the hostname has DNS A/AAAA records (site exists even if bots are blocked)."""
        host = urlparse(url).hostname
        if not host:
            return False
        try:
            import socket
            socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            return True
        except OSError:
            return False

    def _verify_candidate(
        self,
        url: str,
        name: str,
        title: str = "",
        note: str = "",
        *,
        trusted_source: bool = False,
    ) -> Optional[Dict]:
        clean = normalize_url(url) or url
        if not clean or not self._host_ok(clean) or not self._is_homepage_like(clean):
            return None

        status, final, html = self._probe(clean)
        final_url = normalize_url(final or clean) or clean
        if not self._host_ok(final_url):
            return None

        host_match = self._host_matches_brand(final_url, name) or self._host_matches_brand(clean, name)

        if status == 200 and html and self._mentions_brand(html, name, final_url):
            return self._accept(
                final_url, name, title=title, note=note, confidence="high", html=html,
            )

        # Bot walls (Amazon 202/403): still accept when the domain clearly is the brand.
        if host_match and status in _SOFT_OK_STATUSES:
            return self._accept(
                final_url if final else clean,
                name,
                title=title or name,
                note=note or "Domain matches the brand (page blocked bots — please confirm).",
                confidence="high" if status == 200 else "medium",
                html=html,
            )

        # Clearbit / knowledge-graph domains: accept when DNS exists even if fetch fails.
        if trusted_source and (host_match or self._dns_resolves(clean)):
            return self._accept(
                final_url if final else clean,
                name,
                title=title or name,
                note=note or "Official domain from company directory — please confirm.",
                confidence="medium",
                html=html,
            )

        # Host matches brand but probe failed (timeout / TLS / empty) — still usable.
        if host_match and (final_url or self._dns_resolves(clean)):
            return self._accept(
                final_url or clean, name, title=title or name,
                note=note or "Matched brand domain.", confidence="medium", html=html,
            )
        return None

    def _pick_website(self, name: str) -> Dict:
        seen: set = set()

        # 0) Curated known brands (Amazon, Flipkart, …) — most reliable.
        known = known_site_for(name)
        if known:
            verified = self._verify_candidate(
                known, name, title=name, note="Known official homepage",
            )
            if verified:
                return verified
            # Even if probe fails entirely, trust the curated map.
            return self._accept(
                known, name, title=name,
                note="Known official homepage (could not fully open it from this network).",
                confidence="medium",
            )

        # 1) Knowledge graph / Wikipedia / search hits
        queries = [
            f"{name} company official website",
            f"{name} (company)",
            name,
        ]
        hits: List[Dict] = []
        for q in queries:
            hits.extend(discover_source(q, limit=6))

        def _rank(hit: Dict) -> int:
            src = hit.get("source") or ""
            url = hit.get("url") or ""
            score = int(hit.get("score") or 0)
            if src == "clearbit" and score >= 70:
                return 0
            if src in ("wikidata", "ddg_infobox") and self._is_homepage_like(url):
                return 1
            if src == "clearbit" or self._host_matches_brand(url, name):
                return 2
            if src == "ddg_html" and self._host_matches_brand(url, name):
                return 2
            if src in ("ddg_abstract", "wikipedia"):
                return 4
            return 3

        for hit in sorted(hits, key=_rank):
            url = normalize_url(hit.get("url") or "")
            if not url or url in seen:
                continue
            seen.add(url)
            if "wikipedia.org" in urlparse(url).netloc:
                continue
            trusted = (hit.get("source") or "") in ("clearbit", "wikidata", "ddg_infobox")
            verified = self._verify_candidate(
                url,
                name,
                title=hit.get("title") or "",
                note=hit.get("snippet") or "",
                trusted_source=trusted,
            )
            if verified:
                return verified

        # 2) Heuristic domains: Amazon → amazon.com / amazon.in
        for guess in guess_brand_domains(name):
            if guess in seen:
                continue
            seen.add(guess)
            verified = self._verify_candidate(
                guess, name, title=f"{name} (guessed domain)",
                note="Matched common brand domain",
            )
            if verified:
                return verified

        return {"status": "not_found", "url": "", "confidence": "none"}

    def _from_url(self, raw: str, display_name: str = "") -> Dict:
        url = normalize_url(raw)
        if not url:
            return {"status": "not_found", "url": "", "confidence": "none"}
        name = display_name or urlparse(url).netloc.replace("www.", "").split(".")[0]
        verified = self._verify_candidate(url, name, title=name)
        if verified:
            return verified
        status, final, html = self._probe(url)
        final_url = normalize_url(final or url) or url
        if status in _SOFT_OK_STATUSES or html:
            return self._accept(
                final_url, name,
                note="Opened the link you provided — please confirm it's the right site.",
                confidence="low", html=html,
            )
        # User pasted it — still keep it so they can confirm/edit.
        return self._accept(
            url, name,
            note="Could not reach this link from the server — please confirm or edit.",
            confidence="low",
        )

    def _extract_socials_from_html(self, html: str, base_url: str) -> Dict[str, str]:
        if not html:
            return {"twitter": "", "instagram": "", "youtube": ""}
        soup = BeautifulSoup(html, "html.parser")
        hrefs = [urljoin(base_url, a["href"]) for a in soup.find_all("a", href=True)]
        blob = " ".join(hrefs) + "\n" + html[:20000]

        twitter = ig = youtube = ""
        for match in _TWITTER_RE.finditer(blob):
            handle = self._normalize_handle(match.group(1))
            if handle.lower() in ("share", "intent", "home", "search", "login", "i", "hashtag"):
                continue
            twitter = handle
            break
        for match in _IG_RE.finditer(blob):
            handle = self._normalize_handle(match.group(1))
            if handle.lower() in ("p", "reel", "reels", "stories", "explore", "accounts"):
                continue
            ig = handle
            break
        for match in _YT_RE.finditer(blob):
            youtube = "https://www." + match.group(1).rstrip("/")
            break
        return {"twitter": twitter, "instagram": ig, "youtube": youtube}

    def _search_social(self, name: str, network: str) -> Optional[str]:
        hits = discover_source(f"{name} official {network}", limit=5)
        for hit in hits:
            url = hit.get("url") or ""
            if network == "twitter":
                m = _TWITTER_RE.search(url)
                if m:
                    handle = self._normalize_handle(m.group(1))
                    if handle.lower() not in ("share", "intent", "home"):
                        return handle
            elif network == "instagram":
                m = _IG_RE.search(url)
                if m:
                    handle = self._normalize_handle(m.group(1))
                    if handle.lower() not in ("p", "reel", "explore"):
                        return handle
            elif network == "youtube":
                m = _YT_RE.search(url)
                if m:
                    return "https://www." + m.group(1).rstrip("/")
        return None

    def discover(self, name_or_url: str) -> Dict:
        """Return a discovery payload for the confirm UI."""
        self.last_error = None
        raw = (name_or_url or "").strip()
        if len(raw) < 2:
            self.last_error = "Enter a company or brand name."
            return {"ok": False, "error": self.last_error, "name": raw}

        if self._looks_like_url(raw):
            website = self._from_url(raw)
            brand = urlparse(website.get("url") or normalize_url(raw) or "").netloc
            brand = brand.replace("www.", "").split(".")[0].title() or raw
        else:
            brand = raw
            website = self._pick_website(brand)

        socials = {"twitter": "", "instagram": "", "youtube": ""}
        if website.get("html"):
            socials = self._extract_socials_from_html(website["html"], website.get("url") or "")

        twitter_status = "found" if socials["twitter"] else "not_found"
        ig_status = "found" if socials["instagram"] else "not_found"
        yt_status = "found" if socials["youtube"] else "not_found"

        if twitter_status == "not_found":
            found = self._search_social(brand, "twitter")
            if found:
                socials["twitter"] = found
                twitter_status = "found"
        if ig_status == "not_found":
            found = self._search_social(brand, "instagram")
            if found:
                socials["instagram"] = found
                ig_status = "found"
        if yt_status == "not_found":
            found = self._search_social(brand, "youtube")
            if found:
                socials["youtube"] = found
                yt_status = "found"

        news_items = []
        try:
            news_items = self.news.fetch(competitor_name=brand, url=website.get("url") or "")
        except Exception:  # noqa: BLE001
            logger.exception("Discovery news fetch failed")

        # Enrich with Yahoo Finance news when a public ticker resolves (free).
        try:
            from agents.market_intel import lookup_market_intel
            market = lookup_market_intel(brand, max_news=6)
            existing = {(n.get("title") or "").lower() for n in news_items}
            for item in market.get("news") or []:
                title = (item.get("title") or "").strip()
                if not title or title.lower() in existing:
                    continue
                row = {
                    "signal_type": item.get("signal_type") or "other",
                    "signal_label": item.get("signal_label") or "News",
                    "title": title[:240],
                    "url": item.get("url") or "",
                    "summary": item.get("summary") or item.get("source") or "",
                    "source": item.get("source") or "",
                }
                sent = item.get("sentiment") or {}
                if sent.get("label"):
                    extra = f"tone {sent['label']}"
                    row["summary"] = f"{row['summary']} · {extra}".strip(" ·") if row["summary"] else extra
                news_items.append(row)
                existing.add(title.lower())
                if len(news_items) >= 8:
                    break
        except Exception:  # noqa: BLE001
            logger.exception("Market intel enrichment failed during discovery")

        website_out = {k: v for k, v in website.items() if k != "html"}

        return {
            "ok": website_out.get("status") == "found",
            "name": brand,
            "website": website_out,
            "twitter": {"status": twitter_status, "handle": socials["twitter"]},
            "instagram": {"status": ig_status, "handle": socials["instagram"]},
            "youtube": {"status": yt_status, "url": socials["youtube"]},
            "news": news_items[:5],
            "error": None if website_out.get("status") == "found" else (
                f"Couldn't confidently find an official website for “{brand}”. "
                "Try the full brand name, or paste their website link instead."
            ),
        }
