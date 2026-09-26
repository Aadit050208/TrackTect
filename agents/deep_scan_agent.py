"""Deep page scan: extract products, discounts, collabs, careers, CSR from scraped HTML/text.

Rule-based (no LLM) so findings always run when pages scrape successfully.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# kind → (url path fragments, link/text keywords)
PAGE_KINDS = {
    "pricing": (("/pricing", "/plans", "/price", "/subscription"), ("pricing", "plans", "price")),
    "products": (
        ("/products", "/product", "/shop", "/store", "/collections", "/catalog", "/solutions", "/features"),
        ("products", "shop", "collections", "solutions", "features", "marketplace"),
    ),
    "careers": (
        ("/careers", "/jobs", "/join", "/hiring", "/life-at", "/vacancies"),
        ("careers", "jobs", "we're hiring", "join us", "open roles", "openings"),
    ),
    "partners": (
        ("/partners", "/partner", "/ecosystem", "/integrations", "/alliance", "/collaborat"),
        ("partners", "partner with", "integrations", "ecosystem", "collaboration"),
    ),
    "promotions": (
        ("/offers", "/deals", "/sale", "/promo", "/discounts", "/coupons"),
        ("offers", "deals", "sale", "% off", "discount", "promo"),
    ),
    "about": (
        ("/about", "/company", "/who-we-are", "/our-story"),
        ("about us", "our company", "who we are", "our story"),
    ),
    "impact": (
        ("/impact", "/sustainability", "/esg", "/responsibility", "/community", "/giving", "/foundation"),
        ("sustainability", "impact", "esg", "carbon", "community", "giving back", "social responsibility"),
    ),
    "blog": (
        ("/blog", "/news", "/press", "/articles", "/resources", "/updates"),
        ("blog", "news", "press", "resources", "articles"),
    ),
}

_FINDING_PATTERNS: List[Tuple[str, str, Tuple[str, ...], str]] = [
    # category, severity, keywords/regex hints, label prefix
    (
        "Discount / Offer",
        "high",
        (
            r"\d+\s*%\s*off",
            r"flat\s+\d+\s*%",
            r"save\s+(up\s+to\s+)?\$?\d+",
            r"discount",
            r"\bsale\b",
            r"limited[- ]time offer",
            r"promo code",
            r"coupon",
            r"buy one get",
            r"\bbogo\b",
            r"free shipping",
            r"introductory price",
        ),
        "Offer",
    ),
    (
        "New Product / Launch",
        "medium",
        (
            r"\bnew\s+(product|collection|line|model|feature|plan|sku)\b",
            r"\blaunch(ing|ed)?\b",
            r"\bintroduc(e|ing|es|ed)\b",
            r"\bunveil(s|ed|ing)?\b",
            r"\bnow available\b",
            r"\bjust dropped\b",
            r"\bcoming soon\b",
        ),
        "Product",
    ),
    (
        "Partnership / Collab",
        "medium",
        (
            r"\bpartner(s|ship|ing)?\b",
            r"\bcollab(oration|orating|orates)?\b",
            r"\bin partnership with\b",
            r"\bpowered by\b",
            r"\bintegrat(es|ion|ed)\s+with\b",
            r"\balliance\b",
            r"\bjoins forces\b",
            r"\bco[- ]brand",
        ),
        "Collab",
    ),
    (
        "Careers",
        "low",
        (
            r"\bwe'?re hiring\b",
            r"\bopen (role|position|job)s?\b",
            r"\bjob opening",
            r"\bapply now\b",
            r"\bjoin (our|the) team\b",
            r"\bcareer(s)?\b",
            r"\bhiring\b",
            r"\bvacanc(y|ies)\b",
        ),
        "Careers",
    ),
    (
        "Impact / CSR",
        "low",
        (
            r"\bsustainab",
            r"\bcarbon\b",
            r"\bnet[- ]zero\b",
            r"\besg\b",
            r"\bgiving back\b",
            r"\bcommunity impact\b",
            r"\bcorporate social",
            r"\bnonprofit\b",
            r"\bcharit",
            r"\bdiversity[, ]+equity",
            r"\brenewable\b",
            r"\brecycl",
        ),
        "Impact",
    ),
]


def infer_page_kind(url: str) -> str:
    path = (urlparse(url).path or "").lower()
    for kind, (paths, _kw) in PAGE_KINDS.items():
        if any(p in path for p in paths):
            return kind
    return "page"


def _clean_line(text: str) -> str:
    return " ".join((text or "").split())


def _iter_candidate_lines(text: str) -> List[str]:
    lines = []
    for raw in re.split(r"[\n\r•·|]+", text or ""):
        cleaned = _clean_line(raw)
        if 28 <= len(cleaned) <= 200:
            lines.append(cleaned)
    return lines


class DeepScanAgent:
    """Scan all scraped pages for PM-relevant structured findings."""

    def __init__(self, max_per_category: int = 5, max_total: int = 18) -> None:
        self.max_per_category = max_per_category
        self.max_total = max_total

    def scan(self, scraped: Dict[str, str], competitor_name: str = "") -> List[Dict]:
        """Return insight-shaped dicts: category, text, severity, triage_reason, confidence."""
        if not scraped:
            return []

        buckets: Dict[str, List[Dict]] = {}
        for url, text in scraped.items():
            kind = infer_page_kind(url)
            # Bias by page kind
            kind_boost = {
                "promotions": ("Discount / Offer",),
                "pricing": ("Discount / Offer", "New Product / Launch"),
                "products": ("New Product / Launch", "Discount / Offer"),
                "partners": ("Partnership / Collab",),
                "careers": ("Careers",),
                "impact": ("Impact / CSR",),
                "about": ("Impact / CSR", "Partnership / Collab"),
                "blog": ("New Product / Launch", "Partnership / Collab", "Impact / CSR"),
            }.get(kind, ())

            for line in _iter_candidate_lines(text):
                lower = line.lower()
                matches = []
                for category, severity, patterns, prefix in _FINDING_PATTERNS:
                    hit_pats = [p for p in patterns if re.search(p, lower)]
                    if not hit_pats:
                        continue
                    matches.append((len(hit_pats), category, severity, prefix))
                if not matches:
                    continue
                # Prefer the category with the most pattern hits; tie-break by list order weight
                priority = {
                    "Discount / Offer": 0,
                    "Partnership / Collab": 1,
                    "New Product / Launch": 2,
                    "Careers": 3,
                    "Impact / CSR": 4,
                }
                matches.sort(key=lambda m: (-m[0], priority.get(m[1], 9)))
                _n, category, severity, prefix = matches[0]

                if kind_boost and category not in kind_boost and kind not in ("page", "blog"):
                    sev = "low" if severity != "high" else "medium"
                    conf = 0.55
                else:
                    sev = severity
                    conf = 0.8 if kind != "page" else 0.65

                entry = {
                    "category": category,
                    "text": f"{prefix}: {line}"[:400],
                    "severity": sev,
                    "confidence": conf,
                    "triage_reason": f"Found on {kind} page ({urlparse(url).path or '/'})",
                    "needs_review": conf < 0.7,
                    "roadmap_match": "",
                    "source_url": url,
                    "page_kind": kind,
                }
                buckets.setdefault(category, []).append(entry)

        # Deduplicate by normalized text, cap per category
        out: List[Dict] = []
        seen = set()
        for category, items in buckets.items():
            count = 0
            for item in items:
                key = re.sub(r"\W+", "", item["text"].lower())[:120]
                if key in seen:
                    continue
                seen.add(key)
                out.append(item)
                count += 1
                if count >= self.max_per_category:
                    break
            if len(out) >= self.max_total:
                break

        # Prefer higher severity first when trimming
        severity_rank = {"high": 0, "medium": 1, "low": 2}
        out.sort(key=lambda i: (severity_rank.get(i["severity"], 9), -i.get("confidence", 0)))
        return out[: self.max_total]
