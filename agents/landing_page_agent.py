"""Landing Page Watcher Agent: detects messaging changes on competitor pages.

Input : a list of URLs.
Output: {url: {"status": "changed"|"no_change"|"first_snapshot"|"failed",
               "diff": [unified diff lines], "reason": ...}}.
Snapshots of visible text are persisted to a JSON file and diffed line-by-line
against the previous snapshot on each run.

When the pipeline already scraped the page, pass `prefetched` so we do not
re-fetch (avoids a second 403 and wastes no quota).
"""

import difflib
import json
import logging
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup

from agents.http_text import decode_response_text, safe_accept_encoding
from config import settings
from url_utils import normalize_url

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": safe_accept_encoding(),
    "Upgrade-Insecure-Requests": "1",
}

_FALLBACK_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.4 Safari/605.1.15"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept-Encoding": safe_accept_encoding(),
}


class LandingPageWatcherAgent:
    """Snapshot visible page text and report line-level diffs between runs."""

    def __init__(self) -> None:
        self.snapshot_file = settings.snapshot_file
        if not self.snapshot_file.exists():
            self.snapshot_file.write_text("{}", encoding="utf-8")

    @staticmethod
    def _text_to_lines(text: str) -> List[str]:
        return [line.strip() for line in text.splitlines() if line.strip()][:400]

    def get_visible_text(self, url: str) -> List[str]:
        """Fetch a URL and return its visible text as a list of stripped lines."""
        clean = normalize_url(url) or url
        html = ""
        for headers in (_HEADERS, _FALLBACK_HEADERS):
            try:
                response = requests.get(
                    clean, headers=headers, timeout=20, allow_redirects=True,
                )
                if response.status_code == 200:
                    html = decode_response_text(response)
                    if html:
                        break
            except requests.exceptions.RequestException as exc:
                logger.warning(
                    "Failed to fetch %s: %s", url, exc.__class__.__name__,
                )
                continue
        if not html or len(html) < 400:
            from agents.browser_fetch import fetch_impersonated
            html = fetch_impersonated(clean) or html
        if html:
            soup = BeautifulSoup(html, "html.parser")
            for tag in soup(["style", "script", "nav", "footer", "noscript", "svg"]):
                tag.decompose()
            visible_text = soup.get_text(separator="\n")
            lines = self._text_to_lines(visible_text)
            if lines:
                return lines
        return []

    def load_snapshots(self) -> Dict[str, List[str]]:
        try:
            content = self.snapshot_file.read_text(encoding="utf-8").strip()
            return json.loads(content) if content else {}
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to load snapshot file: %s", exc)
            return {}

    def save_snapshot(self, url: str, lines: List[str]) -> None:
        data = self.load_snapshots()
        data[url] = lines
        try:
            self.snapshot_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("Failed to save snapshot for %s: %s", url, exc)

    @staticmethod
    def compare(old: List[str], new: List[str]) -> List[str]:
        return list(difflib.unified_diff(old, new, lineterm=""))

    def run(
        self,
        urls: List[str],
        prefetched: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Dict]:
        """Check each URL against its last snapshot; return per-URL results."""
        snapshots = self.load_snapshots()
        all_changes: Dict[str, Dict] = {}
        prefetched = prefetched or {}

        for url in urls:
            logger.info("Checking landing page: %s", url)
            if url in prefetched and prefetched[url]:
                new_lines = self._text_to_lines(prefetched[url])
            else:
                new_lines = self.get_visible_text(url)

            if not new_lines:
                all_changes[url] = {
                    "status": "failed",
                    "diff": [],
                    "reason": "empty or unreachable content (site may block scrapers / HTTP 403)",
                }
                continue

            # Prefer clean URL as snapshot key so tracking-param variants collapse.
            snap_key = normalize_url(url) or url
            old_lines = snapshots.get(snap_key) or snapshots.get(url)
            if old_lines is None:
                all_changes[url] = {"status": "first_snapshot", "diff": []}
            else:
                changes = self.compare(old_lines, new_lines)
                if changes:
                    all_changes[url] = {"status": "changed", "diff": changes}
                else:
                    all_changes[url] = {"status": "no_change", "diff": []}

            self.save_snapshot(snap_key, new_lines)

        return all_changes
