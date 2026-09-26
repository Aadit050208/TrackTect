"""Scraper Agent: fetches competitor pages and extracts clean readable text.

Input : a list of URLs (optional consecutive_failures for cross-run self-healing).
Output: {url: text} dict. URLs that cannot be fetched are omitted
        (with details in `self.errors[url]`) — one bad URL never crashes the pipeline.

Within a run: retries once with fallback headers.
Across runs: when consecutive_failures >= threshold, also tries archive.org
and a minimal Accept-only request before giving up.
"""

import logging
import re
from typing import Dict, List, Optional
from urllib.parse import quote, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from agents.browser_fetch import (
    fetch_impersonated,
    fetch_selenium,
    looks_like_challenge,
    selenium_available,
)
from agents.http_text import (
    decode_response_text,
    looks_like_mojibake,
    safe_accept_encoding,
)
from config import settings
from url_utils import normalize_url

logger = logging.getLogger(__name__)

_NOISE_TAGS = ("script", "style", "nav", "footer", "header", "noscript", "svg", "form", "iframe")

# Browser-like defaults — many CDNs (Nike, etc.) 403 bare / bot UAs.
# Accept-Encoding must match what we can decompress (no orphaned `br`).
_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": safe_accept_encoding(),
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

# Second try: different browser profile (never identify as a bot — that worsens 403s).
_FALLBACK_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.4 Safari/605.1.15"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept-Encoding": safe_accept_encoding(),
}

_MINIMAL_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) "
        "Gecko/20100101 Firefox/133.0"
    ),
    "Accept": "text/html,*/*",
    "Accept-Encoding": safe_accept_encoding(),
}

MIN_USEFUL_LENGTH = 200

# Homepages are often the most bot-walled URL; inner pages still carry brand copy.
_CONTENT_FALLBACK_PATHS = (
    "/about", "/about-us", "/en", "/en-in", "/en-us", "/in", "/us",
    "/company", "/who-we-are",
)


class ScraperAgent:
    """Fetch pages with requests + BeautifulSoup and return cleaned text."""

    def __init__(self, timeout: int = 12, max_chars: int = 8000) -> None:
        self.timeout = timeout
        self.max_chars = max_chars
        self.errors: Dict[str, str] = {}
        self.attempts_log: List[str] = []
        self.failure_threshold = settings.fetch_failure_threshold

    def _fetch(self, url: str, headers: dict) -> Optional[str]:
        try:
            response = requests.get(
                url,
                headers=headers,
                timeout=self.timeout,
                allow_redirects=True,
            )
        except requests.exceptions.Timeout:
            self.errors[url] = f"timed out after {self.timeout}s"
            return None
        except requests.exceptions.RequestException as exc:
            self.errors[url] = f"unreachable ({exc.__class__.__name__})"
            return None

        if response.status_code != 200:
            # Some CDNs park a real homepage behind 202/403 with usable HTML;
            # still record the status so fallbacks know why requests failed.
            self.errors[url] = f"HTTP {response.status_code}"
            html = decode_response_text(response)
            text = self._html_to_text(html) if html else None
            if text and len(text) >= MIN_USEFUL_LENGTH:
                return text
            return None

        html = decode_response_text(response)
        if not html:
            enc = response.headers.get("Content-Encoding", "")
            self.errors[url] = (
                f"undecodable body (encoding={enc or 'none'}; "
                "page may be compressed in a format we can't read)"
            )
            return None

        text = self._html_to_text(html)
        if text is None:
            self.errors[url] = "page text looked corrupted or was a bot-wall"
            return None
        return text

    def _fetch_archive(self, url: str) -> Optional[str]:
        """Last-resort: Wayback Machine availability API → closest snapshot."""
        try:
            avail = requests.get(
                f"https://archive.org/wayback/available?url={quote(url, safe='')}",
                timeout=10,
            )
            avail.raise_for_status()
            snapshot = avail.json().get("archived_snapshots", {}).get("closest", {})
            archive_url = snapshot.get("url")
            if not archive_url:
                self.attempts_log.append(f"{url}: no archive.org snapshot available")
                return None
            self.attempts_log.append(f"{url}: trying archive.org snapshot {archive_url}")
            return self._fetch(archive_url, _DEFAULT_HEADERS)
        except (requests.exceptions.RequestException, ValueError, KeyError) as exc:
            self.attempts_log.append(f"{url}: archive.org fallback failed ({exc.__class__.__name__})")
            return None

    def _embedded_page_text(self, html: str, soup: BeautifulSoup) -> str:
        """Pull copy from JSON-LD / Next.js payloads when the DOM is a JS shell."""
        chunks: List[str] = []
        for meta_name in ("description", "og:description", "twitter:description"):
            tag = soup.find("meta", attrs={"name": meta_name}) or soup.find(
                "meta", attrs={"property": meta_name}
            )
            if tag and tag.get("content"):
                chunks.append(tag["content"].strip())
        if soup.title and soup.title.string:
            chunks.append(soup.title.string.strip())

        for script in soup.find_all("script"):
            stype = (script.get("type") or "").lower()
            sid = script.get("id") or ""
            raw = script.string or script.get_text() or ""
            if not raw or len(raw) < 20:
                continue
            if stype == "application/ld+json" or sid == "__NEXT_DATA__":
                chunks.extend(self._strings_from_json(raw))

        if "__NEXT_DATA__" in html and not any(len(c) > 40 for c in chunks):
            match = re.search(
                r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S
            )
            if match:
                chunks.extend(self._strings_from_json(match.group(1)))

        seen = set()
        unique = []
        for chunk in chunks:
            key = chunk.lower()
            if key in seen or len(chunk) < 12:
                continue
            seen.add(key)
            unique.append(chunk)
        return "\n".join(unique)

    @staticmethod
    def _strings_from_json(raw: str) -> List[str]:
        import json
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return []
        out: List[str] = []

        def walk(obj) -> None:
            if isinstance(obj, str):
                s = obj.strip()
                if len(s) < 20 or s.startswith("{") or s.startswith("http"):
                    return
                if s.startswith("/") or s.startswith("function"):
                    return
                out.append(s)
            elif isinstance(obj, dict):
                for value in obj.values():
                    walk(value)
            elif isinstance(obj, list):
                for value in obj:
                    walk(value)

        walk(data)
        return out[:80]

    def _extract_text(self, html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        embedded = self._embedded_page_text(html, soup)
        for tag in soup(_NOISE_TAGS):
            tag.decompose()
        lines = [line.strip() for line in soup.get_text(separator="\n").splitlines()]
        text = "\n".join(line for line in lines if line)
        if len(text) < MIN_USEFUL_LENGTH and embedded:
            text = f"{text}\n{embedded}".strip() if text else embedded
        elif embedded and len(text) < 800:
            text = f"{text}\n{embedded}".strip()
        return text[: self.max_chars]

    def _html_to_text(self, html: str) -> Optional[str]:
        if not html or looks_like_challenge(html):
            return None
        text = self._extract_text(html)
        if not text or looks_like_mojibake(text):
            return None
        return text

    def _text_from_html_fetch(self, url: str, html: Optional[str], label: str) -> Optional[str]:
        text = self._html_to_text(html or "")
        if text and len(text) >= 80:
            self.attempts_log.append(f"{url}: recovered via {label} ({len(text)} chars)")
            return text
        return None

    def scrape_site(self, url: str, consecutive_failures: int = 0) -> Optional[str]:
        """Scrape one URL: requests → Chrome impersonate → Selenium → archive.org."""
        clean = normalize_url(url) or url
        text = self._fetch(clean, _DEFAULT_HEADERS)
        if text and len(text) >= MIN_USEFUL_LENGTH:
            return text

        reason = self.errors.get(clean, self.errors.get(url, f"content too short ({len(text or '')} chars)"))
        # Keep error keyed to the caller's URL so pipeline logs stay consistent.
        self.errors[url] = reason
        self.attempts_log.append(
            f"{url}: first attempt failed ({reason}); retrying with fallback headers"
        )
        logger.info("Retrying %s with fallback strategy (%s)", url, reason)

        # 403/202/timeout means the CDN blocked `requests` — another header set
        # usually wastes 20s. Jump to Chrome impersonate instead.
        blocked = any(tok in reason.lower() for tok in ("http 403", "http 202", "http 429", "timed out"))
        retry_text = None
        if not blocked:
            retry_text = self._fetch(clean, _FALLBACK_HEADERS)
        else:
            self.attempts_log.append(
                f"{url}: skip extra requests retry ({reason}) — CDN likely blocking Python"
            )
        best = max((t for t in (text, retry_text) if t), key=len, default=None)
        if best and len(best) >= MIN_USEFUL_LENGTH:
            self.errors.pop(url, None)
            self.errors.pop(clean, None)
            return best

        # Luxury / e-com CDNs often 403 `requests` but allow a Chrome TLS fingerprint.
        self.attempts_log.append(f"{url}: trying Chrome-impersonate fetch")
        impersonated = self._text_from_html_fetch(
            url, fetch_impersonated(clean), "Chrome impersonate",
        )
        if impersonated and (not best or len(impersonated) > len(best)):
            best = impersonated

        # Homepage bot-walls (Meesho, etc.): /about and locale paths often still work.
        parsed = urlparse(clean)
        if (not best or len(best) < MIN_USEFUL_LENGTH) and (parsed.path in ("", "/")):
            for path in _CONTENT_FALLBACK_PATHS:
                alt = urlunparse(parsed._replace(path=path, query="", fragment=""))
                self.attempts_log.append(f"{url}: homepage blocked; trying {alt}")
                alt_html = fetch_impersonated(alt)
                alt_text = self._text_from_html_fetch(url, alt_html, f"impersonate {path}")
                if not alt_text:
                    alt_text = self._fetch(alt, _DEFAULT_HEADERS)
                if alt_text and (not best or len(alt_text) > len(best)):
                    best = alt_text
                    break

        if (not best or len(best) < MIN_USEFUL_LENGTH) and selenium_available():
            self.attempts_log.append(f"{url}: trying headless Chrome (Selenium)")
            rendered = self._text_from_html_fetch(
                url, fetch_selenium(clean), "Selenium",
            )
            if rendered and (not best or len(rendered) > len(best)):
                best = rendered
        elif not selenium_available():
            self.attempts_log.append(f"{url}: Selenium not installed; skip browser render")

        # Escalate immediately (not only after N failed runs) — first search should work.
        if not best or len(best) < MIN_USEFUL_LENGTH:
            if consecutive_failures >= self.failure_threshold:
                self.attempts_log.append(
                    f"{url}: {consecutive_failures} consecutive failures — extra header + archive"
                )
                minimal = self._fetch(clean, _MINIMAL_HEADERS)
                if minimal and (not best or len(minimal) > len(best)):
                    best = minimal
            self.attempts_log.append(f"{url}: trying archive.org snapshot")
            archived = self._fetch_archive(clean)
            if archived and (not best or len(archived) > len(best)):
                best = archived
                self.attempts_log.append(f"{url}: recovered content via archive.org")

        if best:
            self.errors.pop(url, None)
            self.errors.pop(clean, None)
            if len(best) < MIN_USEFUL_LENGTH:
                self.attempts_log.append(
                    f"{url}: retry still returned only {len(best)} chars; using it anyway"
                )
            return best

        self.attempts_log.append(
            f"{url}: all strategies failed ({self.errors.get(url, 'unknown')}); giving up"
        )
        return None

    def run(self, urls: List[str], consecutive_failures: int = 0) -> Dict[str, str]:
        """Scrape every URL; return {url: text} for successes."""
        self.errors = {}
        self.attempts_log = []
        scraped: Dict[str, str] = {}
        for url in urls:
            logger.info("Scraping %s", url)
            content = self.scrape_site(url, consecutive_failures=consecutive_failures)
            if content:
                scraped[url] = content
            else:
                logger.warning(
                    "Failed to scrape %s: %s", url, self.errors.get(url, "unknown error")
                )
        return scraped
