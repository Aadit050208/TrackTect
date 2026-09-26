"""Triage Agent: scores classified changes by significance with explainable reasons.

Input : list of {"category", "text", optional "confidence"} dicts.
Output: same list with added keys:
        - severity: "high" | "medium" | "low"
        - triage_reason: one-line explanation of the score
        - needs_review: True when classification confidence is low
        - roadmap_match: matching user roadmap line (optional second pass)
"""

import json
import logging
import re
from typing import Dict, List, Optional, Tuple

from agents.classifier_agent import LOW_CONFIDENCE
from agents.llm_client import LLMClient
from agents.roadmap_scorer import RoadmapScorer
from config import settings

logger = logging.getLogger(__name__)

SEVERITIES = ("high", "medium", "low")

_HIGH_KEYWORDS = ("launch", "new ", "introduc", "major", "release", "acqui", "partner", "free ", "discount")


class TriageAgent:
    """Assign severity + reason to each classified change; flag low-confidence items."""

    def __init__(self, llm: Optional[LLMClient] = None) -> None:
        self.llm = llm or LLMClient()
        self.roadmap_scorer = RoadmapScorer(self.llm)

    @staticmethod
    def _rule_triage(item: Dict) -> Tuple[str, str]:
        category = item.get("category", "Other")
        text = item.get("text", "").lower()
        if category == "Pricing Change":
            return "high", "scored high: pricing change"
        if category == "Funding / Partnership":
            return "high", "scored high: funding or strategic partnership"
        if category == "Marketing Campaign":
            return "medium", "scored medium: marketing / campaign signal"
        if category == "User Engagement":
            return "medium", "scored medium: engagement / community signal"
        if category == "Feature Update":
            if any(kw in text for kw in _HIGH_KEYWORDS):
                hit = next(kw for kw in _HIGH_KEYWORDS if kw in text)
                return "high", f"scored high: feature update mentions '{hit.strip()}'"
            return "medium", "scored medium: feature update without major-launch signals"
        if category == "UI/UX Change":
            return "medium", "scored medium: UI/UX change"
        if category == "Tone Shift":
            return "low", "scored low: tone/copy shift"
        return "low", "scored low: miscellaneous / other"

    def _llm_triage(self, items: List[Dict]) -> Optional[List[Tuple[str, str]]]:
        numbered = "\n".join(f"{i + 1}. [{it['category']}] {it['text']}" for i, it in enumerate(items))
        prompt = f"""You triage competitor product changes for a PM team.

For each numbered change, return a JSON array of objects with:
- "severity": "high" | "medium" | "low"
- "reason": one short sentence starting with "scored <severity>:" explaining why

high = pricing changes, major feature launches, strategic shifts
medium = notable UI/UX or feature tweaks
low = minor copy/tone tweaks

Respond with ONLY valid JSON.

Changes:
{numbered}
"""
        raw = self.llm.chat(
            system="You are a strict JSON generator that triages competitor intelligence.",
            user=prompt,
            temperature=0.2,
            max_tokens=500,
        )
        if raw is None:
            return None
        try:
            cleaned = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
            match = re.search(r"\[.*\]", cleaned, flags=re.DOTALL)
            parsed = json.loads(match.group(0) if match else cleaned)
            if not isinstance(parsed, list) or len(parsed) != len(items):
                return None
            out: List[Tuple[str, str]] = []
            for entry in parsed:
                if isinstance(entry, str):
                    sev = entry.lower()
                    if sev not in SEVERITIES:
                        return None
                    out.append((sev, f"scored {sev}: LLM severity label"))
                elif isinstance(entry, dict):
                    sev = str(entry.get("severity", "")).lower()
                    reason = str(entry.get("reason", "")).strip()
                    if sev not in SEVERITIES:
                        return None
                    if not reason:
                        reason = f"scored {sev}"
                    out.append((sev, reason))
                else:
                    return None
            return out
        except (ValueError, json.JSONDecodeError, AttributeError):
            pass
        logger.info("Triage LLM response unusable; falling back to rules")
        return None

    def triage(self, items: List[Dict], roadmap: Optional[List[str]] = None) -> List[Dict]:
        """Annotate with severity, triage_reason, needs_review, and optional roadmap_match.

        Roadmap scoring piggybacks on this same pipeline run — no extra quota unit.
        """
        if not items:
            return []

        use_llm = settings.llm_triage_enabled and not self.llm.is_cooling_down()
        scored = self._llm_triage(items) if use_llm else None
        if scored is None:
            scored = [self._rule_triage(item) for item in items]

        results: List[Dict] = []
        for item, (sev, reason) in zip(items, scored):
            confidence = float(item.get("confidence", 1.0))
            needs_review = confidence < LOW_CONFIDENCE
            if needs_review and "low confidence" not in reason.lower():
                reason = f"{reason} · low classification confidence ({confidence:.0%})"
            results.append({
                **item,
                "severity": sev,
                "triage_reason": reason,
                "needs_review": needs_review,
                "confidence": confidence,
                "roadmap_match": "",
            })

        if roadmap:
            results = self.roadmap_scorer.score(results, roadmap)
            matched = sum(1 for r in results if r.get("roadmap_match"))
            if matched:
                logger.info("Roadmap overlap on %s / %s insight(s)", matched, len(results))
        return results
