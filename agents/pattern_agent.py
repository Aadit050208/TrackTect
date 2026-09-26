"""Pattern detection agent: finds cross-run trends a single check would miss.

Looks at recent insight history for a competitor and emits human-readable
pattern messages (e.g. "third pricing-related change in 2 weeks").
"""

import logging
from collections import Counter
from typing import Dict, List, Optional

import db

logger = logging.getLogger(__name__)


class PatternDetectionAgent:
    """Detect recurring themes across a competitor's recent history."""

    def analyze(self, competitor_id: int, run_id: Optional[int] = None) -> List[Dict]:
        recent = db.get_recent_insights(competitor_id, days=14, limit=80)
        if len(recent) < 2:
            return []

        by_category = Counter(row["category"] for row in recent)
        tone_count = sum(1 for row in recent if row["category"] == "Tone Shift")
        patterns: List[Dict] = []

        for category, count in by_category.items():
            if count >= 3:
                message = f"Third+ {category.lower()} in 2 weeks ({count} detected)"
                if count == 3:
                    message = f"Third {category.lower()} in 2 weeks"
                patterns.append({"message": message, "category": category})

        # Tone consistency: 4+ tone shifts → messaging drift signal.
        if tone_count >= 4:
            patterns.append({
                "message": "Messaging has shifted tone consistently across the last several checks",
                "category": "Tone Shift",
            })

        # High-severity clustering.
        high = [r for r in recent if r["severity"] == "high"]
        if len(high) >= 3:
            patterns.append({
                "message": f"{len(high)} high-priority changes clustered in the last 2 weeks",
                "category": "Other",
            })

        for pattern in patterns:
            db.add_pattern(competitor_id, run_id, pattern["message"], pattern["category"])
            logger.info("Pattern for competitor %s: %s", competitor_id, pattern["message"])

        return patterns
