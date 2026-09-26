"""Lightweight auth hardening for TrackTect demos.

- Stronger password rules (no third-party auth service)
- In-memory IP rate limits for login / register
- Helpers for session cookie settings
"""

from __future__ import annotations

import re
import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Optional, Tuple

# Password policy
MIN_PASSWORD_LEN = 8
MIN_USERNAME_LEN = 3
MAX_USERNAME_LEN = 32

# Rate limits: max attempts per window per IP (+ optional username key)
_LOGIN_LIMIT = 8
_LOGIN_WINDOW_SEC = 15 * 60
_REGISTER_LIMIT = 5
_REGISTER_WINDOW_SEC = 60 * 60

_lock = threading.Lock()
_buckets: Dict[str, Deque[float]] = defaultdict(deque)

_WEAK_SECRETS = {
    "",
    "change-me-to-a-random-string",
    "dev-insecure-secret-change-me",
    "secret",
    "changeme",
}


def validate_username(username: str) -> Optional[str]:
    """Return an error message, or None if OK."""
    name = (username or "").strip()
    if len(name) < MIN_USERNAME_LEN:
        return f"Username must be at least {MIN_USERNAME_LEN} characters."
    if len(name) > MAX_USERNAME_LEN:
        return f"Username must be at most {MAX_USERNAME_LEN} characters."
    if not re.fullmatch(r"[A-Za-z0-9_]+", name):
        return "Username can only use letters, numbers, and underscores."
    return None


def validate_password(password: str) -> Optional[str]:
    """Return an error message, or None if OK."""
    if len(password or "") < MIN_PASSWORD_LEN:
        return f"Password must be at least {MIN_PASSWORD_LEN} characters."
    if password.isspace() or not password.strip():
        return "Password cannot be blank."
    # Require some variety without being annoying
    classes = sum(
        [
            bool(re.search(r"[A-Za-z]", password)),
            bool(re.search(r"[0-9]", password)),
            bool(re.search(r"[^A-Za-z0-9]", password)),
        ]
    )
    if classes < 2:
        return "Use letters plus a number or symbol in your password."
    lowered = password.lower()
    if lowered in {"password", "password1", "12345678", "qwerty123", "tracktect"}:
        return "Please choose a less common password."
    return None


def client_ip(remote_addr: Optional[str], forwarded_for: Optional[str] = None) -> str:
    """Best-effort client IP (first X-Forwarded-For hop when behind a proxy)."""
    if forwarded_for:
        first = forwarded_for.split(",")[0].strip()
        if first:
            return first
    return (remote_addr or "unknown").strip() or "unknown"


def _prune(bucket: Deque[float], window_sec: int, now: float) -> None:
    cutoff = now - window_sec
    while bucket and bucket[0] < cutoff:
        bucket.popleft()


def check_rate_limit(key: str, *, limit: int, window_sec: int) -> Tuple[bool, int]:
    """Return (allowed, retry_after_seconds). Records an attempt when allowed."""
    now = time.time()
    with _lock:
        bucket = _buckets[key]
        _prune(bucket, window_sec, now)
        if len(bucket) >= limit:
            retry = max(1, int(window_sec - (now - bucket[0])) + 1)
            return False, retry
        bucket.append(now)
        return True, 0


def allow_login_attempt(ip: str, username: str = "") -> Tuple[bool, int]:
    ok_ip, retry_ip = check_rate_limit(
        f"login:ip:{ip}", limit=_LOGIN_LIMIT, window_sec=_LOGIN_WINDOW_SEC
    )
    if not ok_ip:
        return False, retry_ip
    if username:
        ok_user, retry_user = check_rate_limit(
            f"login:user:{username.lower()}",
            limit=_LOGIN_LIMIT,
            window_sec=_LOGIN_WINDOW_SEC,
        )
        if not ok_user:
            return False, retry_user
    return True, 0


def allow_register_attempt(ip: str) -> Tuple[bool, int]:
    return check_rate_limit(
        f"register:ip:{ip}", limit=_REGISTER_LIMIT, window_sec=_REGISTER_WINDOW_SEC
    )


def is_weak_secret_key(secret: str) -> bool:
    value = (secret or "").strip()
    if value in _WEAK_SECRETS:
        return True
    if len(value) < 24:
        return True
    return False
