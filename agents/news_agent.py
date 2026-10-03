"""News Agent: pulls recent public news about a competitor for PMs.

Fetches Google News RSS (no API key) and classifies each headline into
funding / marketing campaign / partnership / product launch / engagement.
Uses several targeted queries so thin categories get a real chance.
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

# Single-token brands that are also ordinary words. A bare word match is not
# enough — "Angel" must not pull in Angel Reese or Angel City FC.
_COMMON_WORDS = frozenset({
    "angel", "apple", "coach", "gap", "jar", "match", "meta", "one", "shell",
    "square", "stripe", "target", "visa", "block", "wish", "robin", "mint",
    "chase", "capital", "first", "national", "general", "united", "american",
    "bank", "pay", "one",
})

# Trailing words that are not the distinctive part of a brand.
_GENERIC_SUFFIXES = frozenset({
    "bank", "banks", "inc", "incorporated", "ltd", "limited", "llc", "corp",
    "corporation", "company", "co", "group", "holdings", "labs", "lab",
    "technologies", "technology", "tech", "payments", "financial", "finance",
    "services", "solutions", "plc", "pvt", "private",
})

# Extra confirmation for an ambiguous one-word brand. "Launch party" alone is
# not enough when the next word is a different proper name (checked separately).
_BUSINESS_CONTEXT = (
    "funding", "raises", "raised", "ipo", "acquires", "acquisition", "ceo",
    "cfo", "revenue", "profit", "earnings", "shares", "stock", "stocks",
    "trading", "broker", "brokerage", "invest", "investment", "partnership",
    "hires", "hiring", "valuation", "fintech", "nse", "bse", "sebi", "demat",
    "quarter", "quarterly", "results", "launch", "launches", "launched",
    "unveils", "reports", "sales", "appoints", "store", "customers", "users",
    "campaign", "campaigns", "collection", "collections", "introduces",
    "introduced",
)

# A token glued on with a hyphen, or these next words, names a different entity
# ("Hermes-Epitek", "Hermes Award", "Hermes Agent").
_OTHER_ENTITY_NEXT = frozenset({"award", "awards", "agent", "agents"})

# Words that may follow a brand in a headline without naming a different entity.
# "Angel Reese" / "Angel City" are not in this set; "Coach reports" is.
_NEXT_WORD_OK = _GENERIC_SUFFIXES | frozenset({
    "ceo", "cfo", "ipo", "inc", "ltd", "app", "stock", "shares",
    "reports", "report", "unveils", "unveil", "launches", "launch", "hires", "hire",
    "raises", "raise", "posts", "post", "cuts", "cut", "adds", "add", "names",
    "appoints", "partners", "partner", "expands", "opens", "wins", "sees", "plans",
    "says", "said", "to", "for", "in", "and", "the", "a", "of", "on", "with",
    "is", "has", "will", "did", "didn't", "does", "doesn't", "do", "was", "were",
    "q1", "q2", "q3", "q4",
})


def _norm_brand(name: str) -> str:
    return " ".join(re.sub(r"[^\w\s&+-]", " ", (name or "").lower()).split())


def _phrase_in(text: str, phrase: str) -> bool:
    parts = [re.escape(p) for p in phrase.split() if p]
    if not parts:
        return False
    pattern = r"(?<!\w)" + r"[\s\-]+".join(parts) + r"(?!\w)"
    return re.search(pattern, text or "", flags=re.I) is not None


def _is_ambiguous_token(token: str) -> bool:
    word = (token or "").lower()
    if not word:
        return True
    if word in _COMMON_WORDS:
        return True
    return len(word) <= 3


def _match_phrases(brand: str) -> List[str]:
    """Full brand first, then a distinctive core if the leftover name is not generic."""
    phrase = _norm_brand(brand)
    if not phrase:
        return []
    phrases = [phrase]
    tokens = phrase.split()
    core = list(tokens)
    while len(core) > 1 and core[-1] in _GENERIC_SUFFIXES:
        core = core[:-1]
    core_phrase = " ".join(core)
    if core_phrase != phrase and not _is_ambiguous_token(core_phrase):
        phrases.append(core_phrase)
    return phrases


def _title_is_all_caps(title: str) -> bool:
    letters = [c for c in title if c.isalpha()]
    return bool(letters) and all(c.isupper() for c in letters)


def _token_span(title: str, token: str):
    return re.search(rf"(?<!\w){re.escape(token)}\b", title or "", flags=re.I)


def _has_business_context(text: str) -> bool:
    """Whole words only, so 'quarter' does not match inside 'quarterback'."""
    lowered = text or ""
    return any(
        re.search(rf"(?<!\w){re.escape(term)}(?!\w)", lowered)
        for term in _BUSINESS_CONTEXT
    )


_PREV_WORD_OK = frozenset({
    "and", "or", "with", "for", "the", "of", "to", "in", "on", "vs", "versus",
    "a", "an", "at", "by", "from",
})


def _followed_by_other_name(title: str, token: str, *, strict: bool) -> bool:
    """True when the token is naming something else.

    strict=True (ordinary-word brands): "Angel Reese", "Angel City".
    strict=False (distinctive brands): only a hyphenated other name or a
    known other-entity word ("Hermes Award", "Hermes-Epitek").
    """
    if _title_is_all_caps(title):
        return False
    match = _token_span(title, token)
    if not match:
        return False
    rest = title[match.end():]
    if re.match(r"-[A-Za-z]", rest):
        return True
    nxt_match = re.match(r"[\s\-]+([A-Za-z][\w']*)", rest)
    if not nxt_match:
        return False
    nxt = nxt_match.group(1)
    nxt_l = nxt.lower().rstrip("'s")
    if nxt_l in _OTHER_ENTITY_NEXT:
        return True
    if not strict or not nxt[:1].isupper():
        return False
    return nxt_l not in _NEXT_WORD_OK


def _plain_title(title: str) -> str:
    return (title or "").replace("’", "'").replace("‘", "'").replace("`", "'")


def headline_is_about_company(brand: str, title: str) -> tuple:
    """Return (ok, reason). Conservative: ambiguous headlines are rejected.

    Only the headline is available from Google News RSS (the stored summary is
    the publisher and date, not article text).
    """
    title = _plain_title(title)
    phrases = _match_phrases(brand)
    if not phrases or not (title or "").strip():
        return False, "empty"
    lowered = title.lower()
    full = phrases[0]
    if _phrase_in(lowered, full):
        # Multi-word brands ("Angel One", "ICICI Bank") must match as a phrase.
        if " " in full:
            return True, "exact_name"
        # "Angel Reese" is a different entity. "Unicommerce Esolutions" is not —
        # that check is only for ordinary-word brands.
        if _is_ambiguous_token(full):
            span = _token_span(title, full)
            raw = title[span.start():span.end()] if span else ""
            if span and not _title_is_all_caps(title) and not raw[:1].isupper():
                return False, "common_noun"
            if span:
                prev = re.search(r"([A-Za-z][\w']*)\s+$", title[:span.start()])
                if prev:
                    prev_word = prev.group(1)
                    if prev_word[:1].isupper() and prev_word.lower() not in _PREV_WORD_OK:
                        return False, "different_entity"
            if _followed_by_other_name(title, full, strict=True):
                return False, "different_entity"
            # "Coach takes Tabby on tour" — the brand is the subject. A later,
            # lowercase "coach" still needs a separate business word.
            opens = re.match(rf"\W*{re.escape(full)}\b", title, flags=re.I) is not None
            if not opens and not _has_business_context(lowered):
                return False, "ambiguous_name"
            return True, "exact_name" if opens else "ambiguous_with_context"
        if _followed_by_other_name(title, full, strict=False):
            return False, "different_entity"
        return True, "exact_name"
    for phrase in phrases[1:]:
        if not _phrase_in(lowered, phrase):
            continue
        if " " not in phrase and _is_ambiguous_token(phrase):
            continue
        return True, "distinctive_core"
    return False, "name_absent"


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

    def __init__(self, max_items: int = 12) -> None:
        self.max_items = max_items
        self.last_error: Optional[str] = None

    @staticmethod
    def _brand_query(name: str, url: str) -> str:
        brand = (name or "").strip()
        if not brand or brand.lower().startswith("http"):
            host = urlparse(url or "").netloc.replace("www.", "")
            brand = host.split(".")[0] if host else "company"
        brand = brand.split("?")[0][:60]
        return brand

    @staticmethod
    def _query_brand(brand: str) -> str:
        """Quote multi-word names so the search is for the phrase, not one token."""
        cleaned = " ".join((brand or "").split())
        if " " in cleaned:
            return f'"{cleaned}"'
        return cleaned

    @staticmethod
    def classify_headline(title: str) -> str:
        lower = (title or "").lower()
        for signal, keywords in _SIGNAL_KEYWORDS.items():
            if any(kw in lower for kw in keywords):
                return signal
        return "other"

    def _fetch_rss(self, query: str, limit: int = 6) -> List[Dict]:
        rss_url = (
            "https://news.google.com/rss/search?"
            f"q={quote_plus(query + ' when:14d')}&hl=en-US&gl=US&ceid=US:en"
        )
        try:
            response = requests.get(rss_url, headers=_HEADERS, timeout=20)
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
            if len(items) >= limit:
                break
        return items

    def fetch(self, competitor_name: str = "", url: str = "") -> List[Dict]:
        """Return up to max_items news signals — several targeted queries per run."""
        self.last_error = None
        brand = self._brand_query(competitor_name, url)
        quoted = self._query_brand(brand)
        queries = [
            f'{quoted} (funding OR campaign OR partnership OR launch OR users OR marketing)',
            f'{quoted} pricing',
            f'{quoted} hiring OR careers OR "open roles"',
            f'{quoted} partnership OR collaborat OR acquisition',
        ]
        seen_titles = set()
        merged: List[Dict] = []

        def _consider(item: Dict) -> None:
            key = (item.get("title") or "").lower()
            if not key or key in seen_titles:
                return
            seen_titles.add(key)
            ok, reason = headline_is_about_company(brand, item.get("title") or "")
            if not ok:
                logger.info(
                    "News discarded (not about %s, %s): %s",
                    brand, reason, (item.get("title") or "")[:180],
                )
                return
            item["relevance"] = reason
            merged.append(item)

        for query in queries:
            for item in self._fetch_rss(query, limit=5):
                _consider(item)
                if len(merged) >= self.max_items:
                    break
            if len(merged) >= self.max_items:
                break

        if not merged:
            for item in self._fetch_rss(quoted, limit=self.max_items):
                _consider(item)
                if len(merged) >= self.max_items:
                    break

        if not merged and not self.last_error:
            self.last_error = "no recent news found"
        return merged[: self.max_items]
