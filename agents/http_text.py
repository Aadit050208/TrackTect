"""Decode HTTP response bodies to clean Unicode text.

Fixes the common failure mode where we advertise `br` (brotli) without the
brotli package installed: the body stays compressed, `response.text` Latin-1
decodes binary, and the pipeline logs garbage like ``ah''TA$Y`.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# Safe for servers that support brotli — we only ask for what we can decode.
SAFE_ACCEPT_ENCODING = "gzip, deflate"

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _brotli_available() -> bool:
    try:
        import brotli  # noqa: F401
        return True
    except ImportError:
        try:
            import brotlicffi  # noqa: F401
            return True
        except ImportError:
            return False


def safe_accept_encoding() -> str:
    """Accept-Encoding value that matches installed decompressors."""
    if _brotli_available():
        return "gzip, deflate, br"
    return SAFE_ACCEPT_ENCODING


def _charset_from_headers(response: requests.Response) -> Optional[str]:
    ctype = response.headers.get("Content-Type") or ""
    match = re.search(r"charset\s*=\s*([^\s;]+)", ctype, flags=re.I)
    if not match:
        return None
    return match.group(1).strip("\"'").lower()


def _looks_like_compressed_garbage(raw: bytes) -> bool:
    """Heuristic: compressed or binary payloads that slipped past decompress."""
    if not raw:
        return False
    # Brotli / gzip magic-ish high-entropy starts often have nulls early
    sample = raw[:64]
    if b"\x00" in sample:
        return True
    # Very high non-text byte ratio
    non_text = sum(1 for b in sample if b < 9 or (13 < b < 32) or b > 126)
    return non_text / max(len(sample), 1) > 0.35


def looks_like_mojibake(text: str) -> bool:
    """True when text is mostly undecoded binary / replacement junk."""
    if not text or len(text) < 40:
        return False
    sample = text[:800]
    replacement = sample.count("\ufffd")
    controls = len(_CONTROL_RE.findall(sample))
    # Private-use / box-drawing spam common in Latin-1-of-gzip
    weird = sum(1 for c in sample if ord(c) > 0x24F and not c.isalpha())
    ratio = (replacement + controls + weird * 0.5) / max(len(sample), 1)
    if ratio > 0.12:
        return True
    # Dense run of Latin-1 high bytes decoded as separate glyphs
    high = sum(1 for c in sample if 0x80 <= ord(c) <= 0xFF)
    if high / max(len(sample), 1) > 0.25 and replacement + controls > 5:
        return True
    return False


def decode_response_text(response: requests.Response) -> str:
    """Return Unicode text from an HTTP response, or '' if undecodable garbage."""
    raw = response.content or b""
    if not raw:
        return ""

    encoding_hdr = (response.headers.get("Content-Encoding") or "").lower()
    if "br" in encoding_hdr and not _brotli_available() and _looks_like_compressed_garbage(raw):
        logger.warning(
            "Response is brotli-compressed but brotli is not installed — refusing to decode binary as text"
        )
        return ""

    if _looks_like_compressed_garbage(raw) and "gzip" not in encoding_hdr and "deflate" not in encoding_hdr:
        # Requests should have decompressed gzip; leftover binary is not HTML.
        logger.warning("Response body looks binary/compressed; skipping text decode")
        return ""

    candidates = []
    for enc in (
        _charset_from_headers(response),
        getattr(response, "encoding", None),
        "utf-8",
        getattr(response, "apparent_encoding", None),
        "cp1252",
        "latin-1",
    ):
        if not enc:
            continue
        enc_l = str(enc).lower()
        if enc_l not in candidates:
            candidates.append(enc_l)

    best = ""
    for enc in candidates:
        try:
            text = raw.decode(enc, errors="strict")
        except (LookupError, UnicodeDecodeError):
            try:
                text = raw.decode(enc, errors="replace")
            except (LookupError, UnicodeDecodeError):
                continue
        if looks_like_mojibake(text):
            continue
        # Prefer decodes with fewer replacement chars
        score = text.count("\ufffd")
        if not best or score < best.count("\ufffd"):
            best = text
        if score == 0 and enc in ("utf-8", candidates[0]):
            break

    if not best:
        best = raw.decode("utf-8", errors="replace")

    if looks_like_mojibake(best):
        logger.warning("Decoded text looks like mojibake — discarding")
        return ""

    # Strip NULs that break SQLite / logs
    return best.replace("\x00", "")


def response_html_text(response: requests.Response) -> str:
    """Decode as text and require it to look vaguely like HTML/page content."""
    text = decode_response_text(response)
    if not text:
        return ""
    lowered = text.lstrip()[:200].lower()
    if "<html" in lowered or "<!doctype" in lowered or "<head" in lowered or "<body" in lowered:
        return text
    # Some sites return HTML fragments or JSON — still usable if not garbage
    if looks_like_mojibake(text):
        return ""
    return text
