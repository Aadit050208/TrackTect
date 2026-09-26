"""Weekly/daily digest generation and scheduling helpers.

Builds Markdown digests from stored changes, persists them to the digests
table, and optionally fans them out through configured notifiers (webhook /
email). Manual export still works; the scheduler just automates the same path.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

import db
from notifiers import notify_all

logger = logging.getLogger(__name__)


def build_digest_markdown(user_id: int, days: int = 7) -> str:
    data = db.digest_data(user_id, days=days)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    period = "daily" if days <= 1 else "weekly"
    lines = [f"# TrackTect {period} digest — {today}", ""]
    if not any(d["insights"] or d["diffs"] for d in data):
        lines.append(f"_No changes detected across tracked competitors in the last {days} day(s)._")
    for entry in data:
        comp = entry["competitor"]
        lines.append(f"## {comp['name']} ({comp['url']})")
        if not entry["insights"] and not entry["diffs"]:
            lines.append("- No changes detected.")
        for item in entry["insights"]:
            marker = {"high": "high", "medium": "medium", "low": "low"}.get(item["severity"], "low")
            lines.append(f"- [{marker}] **[{item['category']}]** {item['text']}")
        for diff in entry["diffs"]:
            try:
                n = len(json.loads(diff["diff"] or "[]"))
            except (TypeError, ValueError):
                n = 0
            lines.append(f"- Landing-page messaging changed on {diff['url']} ({n} diff lines)")
        lines.append("")
    return "\n".join(lines)


def generate_and_store(user_id: int, days: int = 7, notify: bool = True) -> Optional[int]:
    """Create a digest, store it, and optionally notify. Returns digest id."""
    content = build_digest_markdown(user_id, days=days)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    period = "Daily" if days <= 1 else "Weekly"
    title = f"{period} digest — {today}"
    digest_id = db.save_digest(user_id, title, content, period_days=days)
    logger.info("Stored digest %s for user %s", digest_id, user_id)

    if notify:
        notify_all({
            "competitor": "TrackTect digest",
            "severity": "info",
            "message": f"{period} digest ready ({title}). Open Digests in the app.",
            "url": f"/digests/{digest_id}",
            "digest_id": digest_id,
            "digest_content": content,
        })
    return digest_id


def generate_for_all_enabled_users() -> None:
    """Scheduler entry point — weekly digests for users who opted in."""
    for user in db.get_all_users():
        if not user["digest_enabled"]:
            continue
        try:
            generate_and_store(user["id"], days=7, notify=True)
        except Exception:  # noqa: BLE001
            logger.exception("Digest generation failed for user %s", user["id"])


def generate_daily_for_enabled_users() -> None:
    """Scheduler entry point — daily digests for users with digest_daily on."""
    for user in db.get_all_users():
        daily = 0
        try:
            daily = int(user["digest_daily"] if "digest_daily" in user.keys() else 0)
        except (TypeError, ValueError, KeyError):
            daily = 0
        if not daily:
            continue
        try:
            generate_and_store(user["id"], days=1, notify=True)
        except Exception:  # noqa: BLE001
            logger.exception("Daily digest failed for user %s", user["id"])
