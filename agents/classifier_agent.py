"""Classifier Agent: labels summary bullets into change categories.

Uses lower token budgets and repairs slightly broken JSON from fast models
(common on Groq free-tier truncations).
"""

import json
import logging
import re
from typing import Dict, List, Optional

from agents.categories import VALID_CATEGORIES
from agents.llm_client import LLMClient

logger = logging.getLogger(__name__)

LOW_CONFIDENCE = 0.55


class ClassifierAgent:
    """Classify summarized product changes via the configured LLM endpoint."""

    def __init__(self, llm: Optional[LLMClient] = None) -> None:
        self.llm = llm or LLMClient()

    @staticmethod
    def _repair_json(text: str) -> str:
        """Best-effort cleanup for truncated / slightly invalid JSON arrays."""
        cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
        if not cleaned.startswith("["):
            match = re.search(r"\[.*", cleaned, flags=re.DOTALL)
            cleaned = match.group(0) if match else cleaned

        # Truncated mid-string → close quote + object + array
        if cleaned.count('"') % 2 == 1:
            cleaned += '"'
        # Balance braces/brackets
        opens = cleaned.count("{") - cleaned.count("}")
        if opens > 0:
            cleaned += "}" * opens
        if not cleaned.rstrip().endswith("]"):
            # Drop a trailing incomplete object fragment after last complete `}`
            last_obj = cleaned.rfind("}")
            if last_obj != -1:
                cleaned = cleaned[: last_obj + 1]
            if not cleaned.rstrip().endswith("]"):
                cleaned = cleaned.rstrip().rstrip(",") + "]"
        return cleaned

    @staticmethod
    def _parse_json_array(text: str) -> List[Dict]:
        cleaned = ClassifierAgent._repair_json(text)
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            # Pull complete {...} objects individually
            objects = re.findall(r"\{[^{}]*\}", cleaned, flags=re.DOTALL)
            parsed = []
            for chunk in objects:
                try:
                    parsed.append(json.loads(chunk))
                except json.JSONDecodeError:
                    continue
        if not isinstance(parsed, list):
            raise ValueError("response JSON is not an array")
        return parsed

    @staticmethod
    def _heuristic_confidence(category: str, text: str) -> float:
        lower = text.lower()
        if category == "Pricing Change" and any(w in lower for w in ("price", "pricing", "$", "plan", "tier")):
            return 0.9
        if category == "Feature Update" and any(w in lower for w in ("launch", "new", "release", "introduce")):
            return 0.85
        if category == "Marketing Campaign" and any(w in lower for w in ("campaign", "ad", "marketing", "promo")):
            return 0.85
        if category == "Funding / Partnership" and any(
            w in lower for w in ("funding", "raised", "partner", "invest", "acqui")
        ):
            return 0.9
        if category == "User Engagement" and any(
            w in lower for w in ("users", "members", "community", "engagement", "loyalty")
        ):
            return 0.8
        if category == "Other":
            return 0.4
        return 0.7

    def classify(self, summary: str, url: Optional[str] = None) -> List[Dict]:
        if self.llm.is_cooling_down():
            logger.warning("Skipping classifier — LLM still cooling down after rate limit")
            return []

        prompt = f"""Return ONLY a JSON array. No markdown, no commentary.

Each element:
- "category": one of Feature Update | UI/UX Change | Pricing Change | Marketing Campaign | Funding / Partnership | User Engagement | Tone Shift | Other
- "text": the bullet text
- "confidence": 0.0 to 1.0

Max 6 items. Keep text short.

Summary:
\"\"\"{(summary or '')[:1800]}\"\"\"
"""
        raw = self.llm.chat(
            system="You output only valid JSON arrays for product-change classification.",
            user=prompt,
            temperature=0.2,
            max_tokens=450,
        )
        if raw is None:
            logger.warning("Classification unavailable for %s (LLM call failed)", url)
            return []

        try:
            items = self._parse_json_array(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            logger.warning("Classifier returned malformed JSON for %s: %s", url, exc)
            logger.debug("Raw classifier output: %s", raw[:500])
            return []

        results: List[Dict] = []
        for item in items:
            if not isinstance(item, dict) or "text" not in item:
                continue
            category = item.get("category", "Other")
            if category not in VALID_CATEGORIES:
                category = "Other"
            try:
                confidence = float(
                    item.get("confidence", self._heuristic_confidence(category, str(item["text"])))
                )
            except (TypeError, ValueError):
                confidence = self._heuristic_confidence(category, str(item["text"]))
            confidence = max(0.0, min(1.0, confidence))
            results.append({
                "category": category,
                "text": str(item["text"])[:400],
                "confidence": confidence,
            })
        return results[:6]
