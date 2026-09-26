"""Per-user usage quota helpers.

One "search" = one pipeline run that may call the LLM (manual check, scheduled
check, quick check, or name-based discovery). Viewing dashboard/history is free.
"""

from __future__ import annotations

from typing import Optional, Tuple

import db
from config import settings


def remaining(user_id: int) -> Tuple[int, int, int]:
    """Return (used, quota, left). left is never negative."""
    user = db.get_user_by_id(user_id)
    if user is None:
        return 0, 0, 0
    used = int(user["usage_count"] if "usage_count" in user.keys() else 0)
    quota = int(user["usage_quota"] if "usage_quota" in user.keys() else settings.default_user_quota)
    left = max(0, quota - used)
    return used, quota, left


def has_quota(user_id: int) -> bool:
    _, _, left = remaining(user_id)
    return left > 0


def quota_exhausted_message(user_id: int) -> str:
    used, quota, _ = remaining(user_id)
    return (
        f"You've used all {quota} of your available searches. "
        "Contact the admin for more."
        if quota > 0
        else "You have no searches available. Contact the admin for access."
    )


def friendly_quota_label(user_id: int) -> str:
    used, quota, left = remaining(user_id)
    if left <= 0:
        return f"You've used all {quota} free searches. Ask an admin if you need more."
    if left == 1:
        return "You have 1 free search left. Ask an admin if you need more."
    return f"You have {left} of {quota} free searches left."


def try_consume(user_id: int) -> Tuple[bool, Optional[str]]:
    """Atomically consume one unit of quota. Returns (ok, error_message)."""
    if db.consume_usage(user_id):
        return True, None
    return False, quota_exhausted_message(user_id)
