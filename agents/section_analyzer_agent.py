"""Section Analyzer: organises competitor signals into PM-ready sections.

Builds a section-wise brief (Products, Pricing, Marketing, UX, Funding,
Engagement) from classified insights + page text. Uses one short LLM call
when available; falls back to rule-based bucketing so Groq rate limits
never block the section view.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Dict, List, Optional

from agents.categories import CATEGORY_TO_SECTION, PM_SECTIONS
from agents.llm_client import LLMClient

logger = logging.getLogger(__name__)

_SECTION_KEYWORDS = {
    "Products & Features": (
        "product", "feature", "launch", "release", "collection", "sneaker",
        "shoe", "apparel", "new model", "sku", "lineup", "now available",
    ),
    "Pricing & Packaging": (
        "price", "pricing", "discount", "sale", "offer", "deal", "₹", "$",
        "mrp", "plan", "tier", "subscription", "% off", "coupon", "promo",
    ),
    "Marketing & Campaigns": (
        "campaign", "ad", "advert", "marketing", "brand", "ambassador",
        "influencer", "promo", "commercial", "spot",
    ),
    "UX & Messaging": (
        "headline", "hero", "cta", "messaging", "tagline", "homepage",
        "redesign", "ui", "ux", "layout", "copy",
    ),
    "Funding & Partnerships": (
        "funding", "raised", "series", "invest", "partner", "acquisition",
        "merger", "sponsor", "collab", "collaboration", "integration",
    ),
    "User Engagement": (
        "community", "member", "loyalty", "app download", "engagement",
        "followers", "users", "subscribers", "membership", "club",
    ),
    "Careers & Hiring": (
        "career", "hiring", "job", "open role", "join our team", "vacancy",
        "we're hiring", "apply now",
    ),
    "Impact & CSR": (
        "sustainab", "esg", "carbon", "impact", "giving back", "community",
        "recycl", "diversity", "charit", "responsibility",
    ),
}


class SectionAnalyzerAgent:
    """Produce per-section summaries for a PM briefing."""

    def __init__(self, llm: Optional[LLMClient] = None) -> None:
        self.llm = llm or LLMClient()

    def analyze(
        self,
        insights: List[Dict],
        page_text: str = "",
        competitor_name: str = "",
        url: str = "",
    ) -> List[Dict]:
        """Return list of {section, summary, bullets[]} for non-empty sections."""
        buckets: Dict[str, List[str]] = {s: [] for s in PM_SECTIONS}

        for item in insights:
            section = CATEGORY_TO_SECTION.get(item.get("category", "Other"), "Other signals")
            text = (item.get("text") or "").strip()
            if text and text not in buckets[section]:
                buckets[section].append(text)

        # Pull extra bullets from page text via keywords (no LLM).
        for line in (page_text or "").splitlines():
            cleaned = " ".join(line.split())
            if len(cleaned) < 25 or len(cleaned) > 180:
                continue
            lower = cleaned.lower()
            for section, keywords in _SECTION_KEYWORDS.items():
                if any(kw in lower for kw in keywords):
                    if cleaned not in buckets[section] and len(buckets[section]) < 6:
                        buckets[section].append(cleaned)
                    break

        llm_sections = self._llm_enrich(buckets, page_text, competitor_name, url)
        if llm_sections:
            return llm_sections

        results: List[Dict] = []
        for section in PM_SECTIONS:
            bullets = buckets[section][:5]
            if not bullets:
                continue
            results.append({
                "section": section,
                "summary": f"{len(bullets)} signal(s) related to {section.lower()}.",
                "bullets": bullets,
            })
        return results

    def _llm_enrich(
        self,
        buckets: Dict[str, List[str]],
        page_text: str,
        competitor_name: str,
        url: str,
    ) -> Optional[List[Dict]]:
        if self.llm.is_cooling_down() or not self.llm.configured:
            return None

        seed = []
        for section, bullets in buckets.items():
            if bullets:
                seed.append(f"{section}: " + " | ".join(bullets[:3]))
        seed_block = "\n".join(seed) if seed else "(none yet)"

        prompt = f"""You are writing a PM competitor brief for {competitor_name or url or 'a competitor'}.

Return ONLY a JSON array. Each object:
- "section": one of {", ".join(PM_SECTIONS)}
- "summary": one short sentence of what changed / is notable
- "bullets": 1-4 short concrete bullets

Include ONLY sections with real signal. Max 5 sections.

Seed signals:
{seed_block}

Page excerpt:
\"\"\"{(page_text or '')[:1600]}\"\"\"
"""
        raw = self.llm.chat(
            system="You output only valid JSON arrays for PM section briefs.",
            user=prompt,
            temperature=0.3,
            max_tokens=500,
        )
        if not raw:
            return None

        try:
            cleaned = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
            match = re.search(r"\[.*\]", cleaned, flags=re.DOTALL)
            parsed = json.loads(match.group(0) if match else cleaned)
            if not isinstance(parsed, list):
                return None
        except (json.JSONDecodeError, ValueError):
            logger.warning("Section analyzer returned unusable JSON")
            return None

        results: List[Dict] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            section = item.get("section", "Other signals")
            if section not in PM_SECTIONS:
                section = "Other signals"
            bullets = item.get("bullets") or []
            if isinstance(bullets, str):
                bullets = [bullets]
            bullets = [str(b).strip() for b in bullets if str(b).strip()][:4]
            summary = str(item.get("summary") or "").strip()
            if not bullets and not summary:
                continue
            results.append({
                "section": section,
                "summary": summary or f"Updates in {section}.",
                "bullets": bullets,
            })
        return results or None
