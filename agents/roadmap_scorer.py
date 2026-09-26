"""Roadmap-relevance scoring for classified insights.

Runs inside an already-paid pipeline run (no extra quota unit). Uses one LLM
call to semantically match changes against the user's roadmap lines; falls
back to light token overlap if the LLM is unavailable.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Dict, List, Optional

from agents.llm_client import LLMClient

logger = logging.getLogger(__name__)


def _token_set(text: str) -> set:
    return {t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(t) > 2}


def _rule_match(change_text: str, roadmap: List[str]) -> str:
    """Cheap overlap fallback — not as good as LLM, but better than nothing."""
    change_tokens = _token_set(change_text)
    if not change_tokens:
        return ""
    # Light synonym bridges for common PM language
    bridges = {
        "csv": {"export", "download", "bulk"},
        "export": {"csv", "download", "bulk", "report"},
        "sso": {"saml", "oauth", "login", "auth"},
        "mobile": {"ios", "android", "app"},
        "pricing": {"price", "plan", "tier", "discount"},
    }
    expanded = set(change_tokens)
    for t in list(change_tokens):
        expanded |= bridges.get(t, set())
    best = ""
    best_score = 0.0
    for line in roadmap:
        tokens = _token_set(line)
        if not tokens:
            continue
        line_exp = set(tokens)
        for t in list(tokens):
            line_exp |= bridges.get(t, set())
        overlap = len(expanded & line_exp) / max(len(tokens), 1)
        cl = change_text.lower()
        ll = line.lower()
        if any(t in cl for t in tokens if len(t) > 3):
            overlap = max(overlap, 0.45)
        if any(t in ll for t in change_tokens if len(t) > 3):
            overlap = max(overlap, 0.4)
        if overlap > best_score:
            best_score = overlap
            best = line
    return best if best_score >= 0.35 else ""


class RoadmapScorer:
    """Annotate classified items with roadmap_match (matched roadmap line or "")."""

    def __init__(self, llm: Optional[LLMClient] = None) -> None:
        self.llm = llm or LLMClient()

    def score(self, items: List[Dict], roadmap: List[str]) -> List[Dict]:
        if not items:
            return []
        if not roadmap:
            return [{**it, "roadmap_match": it.get("roadmap_match") or ""} for it in items]

        matches = self._llm_match(items, roadmap)
        if matches is None:
            matches = [_rule_match(it.get("text", ""), roadmap) for it in items]

        out: List[Dict] = []
        for item, match in zip(items, matches):
            cleaned = (match or "").strip()
            if cleaned and cleaned not in roadmap:
                # Prefer the exact roadmap line when the model paraphrased
                lowered = cleaned.lower()
                for line in roadmap:
                    if line.lower() == lowered or lowered in line.lower() or line.lower() in lowered:
                        cleaned = line
                        break
                else:
                    cleaned = ""
            out.append({**item, "roadmap_match": cleaned})
        return out

    def _llm_match(self, items: List[Dict], roadmap: List[str]) -> Optional[List[str]]:
        if self.llm.is_cooling_down():
            return None
        numbered = "\n".join(
            f"{i + 1}. [{it.get('category', 'Other')}] {it.get('text', '')}"
            for i, it in enumerate(items)
        )
        themes = "\n".join(f"- {line}" for line in roadmap)
        prompt = f"""You help a product manager see if competitor changes overlap their roadmap.

User roadmap themes (exact strings — copy one of these when matching):
{themes}

Competitor changes:
{numbered}

For each numbered change, decide if it semantically overlaps any roadmap theme
(even if wording differs — e.g. "CSV export" overlaps "bulk export").

Return a JSON array of strings, one per change, same order.
Each string is either the exact matching roadmap theme, or "" if no overlap.
Respond with ONLY valid JSON.
"""
        raw = self.llm.chat(
            system="You are a strict JSON generator matching competitor changes to a PM roadmap.",
            user=prompt,
            temperature=0.1,
            max_tokens=400,
        )
        if raw is None:
            return None
        try:
            cleaned = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
            match = re.search(r"\[.*\]", cleaned, flags=re.DOTALL)
            parsed = json.loads(match.group(0) if match else cleaned)
            if not isinstance(parsed, list) or len(parsed) != len(items):
                return None
            return ["" if v is None else str(v).strip() for v in parsed]
        except (ValueError, json.JSONDecodeError, AttributeError):
            logger.info("Roadmap LLM match unusable; falling back to rules")
            return None
