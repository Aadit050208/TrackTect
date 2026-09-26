"""Adaptive frequency agent: propose interval changes based on observed activity.

Never changes the schedule silently — always writes a pending suggestion the
user must accept or reject. Reasoning is stored with the suggestion.
"""

import logging
from typing import Dict, Optional

import db

logger = logging.getLogger(__name__)

MIN_INTERVAL = 6
MAX_INTERVAL = 168  # weekly


class AdaptiveFrequencyAgent:
    """Propose faster/slower check intervals from recent change frequency."""

    def analyze(self, competitor: Dict) -> Optional[Dict]:
        """Return a suggestion dict or None if the current interval looks fine."""
        stats = db.change_frequency_stats(competitor["id"], days=14)
        current = int(competitor.get("interval_hours") or 24)
        changes = stats["changes"]
        runs = max(stats["runs"], 1)
        month_changes = stats["month_changes"]

        proposed = current
        reason = ""

        # Active: something changed on most recent runs over two weeks → check more often.
        if runs >= 4 and changes >= runs:
            proposed = max(MIN_INTERVAL, current // 2)
            reason = (
                f"Changed on roughly every check over the last {stats['days']} days "
                f"({changes} changes across {runs} runs) — propose checking every {proposed}h"
            )
        # Quiet for a month → throttle back.
        elif month_changes == 0 and runs >= 2:
            proposed = min(MAX_INTERVAL, max(current * 2, 48))
            reason = (
                f"Zero changes in the last 30 days across {runs}+ recent runs — "
                f"propose checking every {proposed}h"
            )

        if proposed == current or not reason:
            return None

        logger.info(
            "Adaptive frequency for %s: %sh → %sh (%s)",
            competitor.get("name"), current, proposed, reason,
        )
        suggestion_id = db.add_suggestion(
            competitor["id"],
            kind="interval",
            payload={"from_hours": current, "to_hours": proposed},
            reason=reason,
        )
        return {
            "id": suggestion_id,
            "from_hours": current,
            "to_hours": proposed,
            "reason": reason,
        }
