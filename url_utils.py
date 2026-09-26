"""Shared helpers: URL cleanup and run locks."""

from __future__ import annotations

import threading
from typing import Set
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Tracking / ad junk to strip so we don't store giant Google Ads URLs.
_DROP_QUERY_PREFIXES = ("utm_", "gclid", "gad_", "fbclid", "mc_", "yclid", "msclkid")
_DROP_QUERY_KEYS = {
    "ref", "source", "campaign", "affiliate", "spm", "si", "igshid",
}


def normalize_url(raw: str) -> str:
    """Normalize a competitor URL: add scheme, strip tracking params and fragments."""
    url = (raw or "").strip()
    if not url:
        return url
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    parsed = urlparse(url)
    # Drop fragment
    # Keep only non-tracking query params
    kept = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=False):
        lower = key.lower()
        if lower in _DROP_QUERY_KEYS:
            continue
        if any(lower.startswith(p) for p in _DROP_QUERY_PREFIXES):
            continue
        kept.append((key, value))

    clean = parsed._replace(
        query=urlencode(kept) if kept else "",
        fragment="",
    )
    # Prefer no trailing slash except for bare domain
    path = clean.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return urlunparse(clean._replace(path=path))


# Prevent overlapping pipeline runs for the same competitor (double-clicks / retries).
_running: Set[int] = set()
_running_lock = threading.Lock()


def try_begin_run(competitor_id: int) -> bool:
    with _running_lock:
        if competitor_id in _running:
            return False
        _running.add(competitor_id)
        return True


def end_run(competitor_id: int) -> None:
    with _running_lock:
        _running.discard(competitor_id)
