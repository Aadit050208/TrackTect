"""SQLite persistence layer for TrackTect.

Uses stdlib sqlite3 locally, or Turso (libSQL) when TURSO_* env vars are set
so data survives host restarts. Stores users, tracked competitors, run history,
insights, diffs, social items, alerts, tags, views, digests, and suggestions.
"""

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence

from config import settings
from db_connection import (
    DatabaseUnavailable,
    connect as _open_connection,
    is_available,
    mark_unavailable,
    unavailable_reason,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    has_onboarded INTEGER DEFAULT 0,
    preferences TEXT DEFAULT '{}',
    digest_enabled INTEGER DEFAULT 0,
    default_interval_hours INTEGER DEFAULT 24
);

CREATE TABLE IF NOT EXISTS competitors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    twitter_handle TEXT DEFAULT '',
    youtube_url TEXT DEFAULT '',
    interval_hours INTEGER DEFAULT 24,
    enable_twitter INTEGER DEFAULT 1,
    enable_youtube INTEGER DEFAULT 1,
    enable_notion INTEGER DEFAULT 1,
    active INTEGER DEFAULT 1,
    paused INTEGER DEFAULT 0,
    consecutive_failures INTEGER DEFAULT 0,
    extra_sources TEXT DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    competitor_id INTEGER REFERENCES competitors(id),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT DEFAULT 'running',
    trigger TEXT DEFAULT 'manual',
    logs TEXT DEFAULT '[]',
    agent_status TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS insights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    competitor_id INTEGER REFERENCES competitors(id),
    category TEXT NOT NULL,
    text TEXT NOT NULL,
    severity TEXT DEFAULT 'low',
    confidence REAL DEFAULT 1.0,
    triage_reason TEXT DEFAULT '',
    needs_review INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS diffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    competitor_id INTEGER REFERENCES competitors(id),
    url TEXT NOT NULL,
    status TEXT NOT NULL,
    diff TEXT DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS social_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    competitor_id INTEGER REFERENCES competitors(id),
    source TEXT NOT NULL,
    title TEXT DEFAULT '',
    url TEXT DEFAULT '',
    content TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    competitor_id INTEGER REFERENCES competitors(id),
    run_id INTEGER REFERENCES runs(id),
    severity TEXT DEFAULT 'high',
    message TEXT NOT NULL,
    seen INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    UNIQUE(user_id, name)
);

CREATE TABLE IF NOT EXISTS competitor_tags (
    competitor_id INTEGER NOT NULL REFERENCES competitors(id),
    tag_id INTEGER NOT NULL REFERENCES tags(id),
    PRIMARY KEY (competitor_id, tag_id)
);

CREATE TABLE IF NOT EXISTS saved_views (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    filters TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS digests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    period_days INTEGER DEFAULT 7,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS insight_notes (
    insight_id INTEGER PRIMARY KEY REFERENCES insights(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    note TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quarterly_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    range_start TEXT NOT NULL,
    range_end TEXT NOT NULL,
    summary TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(user_id, range_start, range_end)
);

CREATE TABLE IF NOT EXISTS suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    competitor_id INTEGER NOT NULL REFERENCES competitors(id),
    kind TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS discovered_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    competitor_id INTEGER NOT NULL REFERENCES competitors(id),
    url TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    competitor_id INTEGER NOT NULL REFERENCES competitors(id),
    run_id INTEGER REFERENCES runs(id),
    message TEXT NOT NULL,
    category TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS section_briefs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    competitor_id INTEGER REFERENCES competitors(id),
    section TEXT NOT NULL,
    summary TEXT DEFAULT '',
    bullets TEXT DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS news_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    competitor_id INTEGER REFERENCES competitors(id),
    signal_type TEXT NOT NULL DEFAULT 'other',
    title TEXT NOT NULL,
    url TEXT DEFAULT '',
    summary TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS insight_decisions (
    insight_id INTEGER PRIMARY KEY REFERENCES insights(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    decision TEXT NOT NULL DEFAULT 'monitor',
    owner TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messaging_snapshots (
    url TEXT PRIMARY KEY,
    lines TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);
"""

# Columns added after the initial schema — applied idempotently on startup.
_MIGRATIONS = [
    ("users", "has_onboarded", "INTEGER DEFAULT 0"),
    ("users", "preferences", "TEXT DEFAULT '{}'"),
    ("users", "digest_enabled", "INTEGER DEFAULT 0"),
    ("users", "digest_daily", "INTEGER DEFAULT 0"),
    ("users", "default_interval_hours", "INTEGER DEFAULT 24"),
    ("users", "usage_quota", "INTEGER DEFAULT 2"),
    ("users", "usage_count", "INTEGER DEFAULT 0"),
    ("users", "is_admin", "INTEGER DEFAULT 0"),
    ("users", "roadmap_items", "TEXT DEFAULT '[]'"),
    ("users", "roadmap_setup_done", "INTEGER DEFAULT 0"),
    ("insights", "roadmap_match", "TEXT DEFAULT ''"),
    ("competitors", "paused", "INTEGER DEFAULT 0"),
    ("competitors", "consecutive_failures", "INTEGER DEFAULT 0"),
    ("competitors", "extra_sources", "TEXT DEFAULT '[]'"),
    ("competitors", "instagram_handle", "TEXT DEFAULT ''"),
    ("insights", "confidence", "REAL DEFAULT 1.0"),
    ("insights", "triage_reason", "TEXT DEFAULT ''"),
    ("insights", "needs_review", "INTEGER DEFAULT 0"),
    ("runs", "tokens_used", "INTEGER DEFAULT 0"),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect():
    """Open a connection. Local file when Turso is unset; Turso otherwise."""
    return _open_connection()


def _column_exists(conn, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)


def init_db() -> None:
    """Apply schema. On Turso failure, mark DB down instead of using a wipeable local file."""
    try:
        with _connect() as conn:
            conn.executescript(_SCHEMA)
            for table, column, typedef in _MIGRATIONS:
                if not _column_exists(conn, table, column):
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {typedef}")
            # Backfill defaults for existing users created before quota columns.
            conn.execute(
                "UPDATE users SET usage_quota = ? WHERE usage_quota IS NULL",
                (settings.default_user_quota,),
            )
            conn.execute("UPDATE users SET usage_count = 0 WHERE usage_count IS NULL")
            _sync_admin_flags(conn)
        backend = "Turso (libSQL)" if settings.use_turso else f"local SQLite ({settings.db_path})"
        import logging
        logging.getLogger(__name__).info("Database ready: %s", backend)
    except DatabaseUnavailable as exc:
        mark_unavailable(exc)


def _sync_admin_flags(conn: sqlite3.Connection) -> None:
    """Keep admin tightly scoped so normal accounts cannot open /admin.

    - If ADMIN_USERNAMES is set: ONLY those usernames are admins (everyone else demoted).
    - If unset: ONLY the earliest registered user is admin (bootstrap); all others demoted.
    """
    allow = settings.admin_usernames
    if allow:
        for name in allow:
            conn.execute(
                "UPDATE users SET is_admin = 1 WHERE lower(username) = ?",
                (name,),
            )
        placeholders = ",".join("?" * len(allow))
        conn.execute(
            f"UPDATE users SET is_admin = 0 WHERE lower(username) NOT IN ({placeholders})",
            tuple(allow),
        )
        return

    first = conn.execute(
        "SELECT id FROM users ORDER BY id ASC LIMIT 1"
    ).fetchone()
    if not first:
        return
    conn.execute("UPDATE users SET is_admin = 0 WHERE id != ?", (first["id"],))
    conn.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (first["id"],))


# ---------------------------------------------------------------- users ----

def create_user(username: str, password_hash: str) -> Optional[int]:
    try:
        with _connect() as conn:
            # Only the very first account (bootstrap) or ADMIN_USERNAMES get admin.
            existing = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
            uname = username.strip().lower()
            if settings.admin_usernames:
                is_admin = 1 if uname in settings.admin_usernames else 0
            else:
                is_admin = 1 if existing == 0 else 0
            quota = settings.default_user_quota
            cur = conn.execute(
                """INSERT INTO users
                   (username, password_hash, created_at, usage_quota, usage_count, is_admin)
                   VALUES (?, ?, ?, ?, 0, ?)""",
                (username, password_hash, _now(), quota, is_admin),
            )
            return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def get_user_by_username(username: str) -> Optional[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def get_user_by_id(user_id: int) -> Optional[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_all_users() -> List[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute("SELECT * FROM users ORDER BY username").fetchall()


def is_admin_user(user_id: int) -> bool:
    """True only when the DB flag is set. Flags are synced from ADMIN_USERNAMES on startup."""
    user = get_user_by_id(user_id)
    if user is None:
        return False
    try:
        return int(user["is_admin"] or 0) == 1
    except (KeyError, TypeError, ValueError):
        return False


def set_user_quota(user_id: int, quota: int, reset_count: bool = False) -> None:
    quota = max(0, int(quota))
    with _connect() as conn:
        if reset_count:
            conn.execute(
                "UPDATE users SET usage_quota = ?, usage_count = 0 WHERE id = ?",
                (quota, user_id),
            )
        else:
            conn.execute(
                "UPDATE users SET usage_quota = ? WHERE id = ?",
                (quota, user_id),
            )


def reset_user_usage(user_id: int) -> None:
    with _connect() as conn:
        conn.execute("UPDATE users SET usage_count = 0 WHERE id = ?", (user_id,))


def consume_usage(user_id: int) -> bool:
    """Increment usage_count by 1 if under quota. Returns True if consumed."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT usage_count, usage_quota FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return False
        used = int(row["usage_count"] or 0)
        quota = int(row["usage_quota"] if row["usage_quota"] is not None else settings.default_user_quota)
        if used >= quota:
            return False
        cur = conn.execute(
            """UPDATE users SET usage_count = usage_count + 1
               WHERE id = ? AND usage_count < usage_quota""",
            (user_id,),
        )
        return cur.rowcount == 1


def mark_onboarded(user_id: int) -> None:
    with _connect() as conn:
        conn.execute("UPDATE users SET has_onboarded = 1 WHERE id = ?", (user_id,))


def get_roadmap_items(user_id: int) -> List[str]:
    user = get_user_by_id(user_id)
    if user is None:
        return []
    raw = ""
    try:
        raw = user["roadmap_items"] if "roadmap_items" in user.keys() else "[]"
    except (KeyError, IndexError):
        raw = "[]"
    try:
        items = json.loads(raw or "[]")
    except (TypeError, ValueError):
        items = []
    if not isinstance(items, list):
        return []
    return [str(x).strip() for x in items if str(x).strip()]


def set_roadmap_items(user_id: int, items: Sequence[str], *, mark_setup_done: bool = True) -> None:
    cleaned = [str(x).strip() for x in items if str(x).strip()]
    # De-dupe while preserving order
    seen = set()
    unique: List[str] = []
    for line in cleaned:
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(line)
    with _connect() as conn:
        if mark_setup_done:
            conn.execute(
                "UPDATE users SET roadmap_items = ?, roadmap_setup_done = 1 WHERE id = ?",
                (json.dumps(unique, ensure_ascii=False), user_id),
            )
        else:
            conn.execute(
                "UPDATE users SET roadmap_items = ? WHERE id = ?",
                (json.dumps(unique, ensure_ascii=False), user_id),
            )


def mark_roadmap_setup_done(user_id: int) -> None:
    with _connect() as conn:
        conn.execute("UPDATE users SET roadmap_setup_done = 1 WHERE id = ?", (user_id,))


def roadmap_setup_needed(user_id: int) -> bool:
    user = get_user_by_id(user_id)
    if user is None:
        return False
    try:
        done = int(user["roadmap_setup_done"] or 0) if "roadmap_setup_done" in user.keys() else 0
    except (TypeError, ValueError, KeyError):
        done = 0
    return done == 0


def update_user_preferences(user_id: int, **fields) -> None:
    allowed = {
        "digest_enabled", "digest_daily", "default_interval_hours",
        "preferences", "has_onboarded",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    assignments = ", ".join(f"{k} = ?" for k in updates)
    with _connect() as conn:
        conn.execute(f"UPDATE users SET {assignments} WHERE id = ?", (*updates.values(), user_id))


# ---------------------------------------------------------- competitors ----

def add_competitor(user_id: int, name: str, url: str, twitter_handle: str = "",
                   youtube_url: str = "", interval_hours: int = 24,
                   instagram_handle: str = "") -> int:
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO competitors
               (user_id, name, url, twitter_handle, youtube_url, interval_hours,
                instagram_handle, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id, name, url, twitter_handle, youtube_url, interval_hours,
                instagram_handle, _now(),
            ),
        )
        return cur.lastrowid


def get_competitor(competitor_id: int) -> Optional[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute("SELECT * FROM competitors WHERE id = ?", (competitor_id,)).fetchone()


def get_competitors(user_id: int, include_paused: bool = True) -> List[sqlite3.Row]:
    with _connect() as conn:
        if include_paused:
            return conn.execute(
                "SELECT * FROM competitors WHERE user_id = ? AND active = 1 ORDER BY name",
                (user_id,),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM competitors WHERE user_id = ? AND active = 1 AND paused = 0 ORDER BY name",
            (user_id,),
        ).fetchall()


def get_all_active_competitors() -> List[sqlite3.Row]:
    """Active and not paused — used by the scheduler."""
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM competitors WHERE active = 1 AND paused = 0"
        ).fetchall()


def update_competitor_config(competitor_id: int, **fields) -> None:
    allowed = {
        "name", "url", "twitter_handle", "youtube_url", "interval_hours",
        "enable_twitter", "enable_youtube", "enable_notion", "paused",
        "consecutive_failures", "extra_sources", "instagram_handle",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    assignments = ", ".join(f"{k} = ?" for k in updates)
    with _connect() as conn:
        conn.execute(
            f"UPDATE competitors SET {assignments} WHERE id = ?",
            (*updates.values(), competitor_id),
        )


def delete_competitor(competitor_id: int) -> None:
    with _connect() as conn:
        conn.execute("UPDATE competitors SET active = 0 WHERE id = ?", (competitor_id,))


def set_competitors_paused(competitor_ids: Sequence[int], paused: bool) -> None:
    if not competitor_ids:
        return
    placeholders = ",".join("?" * len(competitor_ids))
    with _connect() as conn:
        conn.execute(
            f"UPDATE competitors SET paused = ? WHERE id IN ({placeholders})",
            (1 if paused else 0, *competitor_ids),
        )


def set_competitors_interval(competitor_ids: Sequence[int], interval_hours: int) -> None:
    if not competitor_ids:
        return
    placeholders = ",".join("?" * len(competitor_ids))
    with _connect() as conn:
        conn.execute(
            f"UPDATE competitors SET interval_hours = ? WHERE id IN ({placeholders})",
            (max(1, int(interval_hours)), *competitor_ids),
        )


def delete_competitors(competitor_ids: Sequence[int]) -> None:
    if not competitor_ids:
        return
    placeholders = ",".join("?" * len(competitor_ids))
    with _connect() as conn:
        conn.execute(
            f"UPDATE competitors SET active = 0 WHERE id IN ({placeholders})",
            tuple(competitor_ids),
        )


def bump_failure(competitor_id: int) -> int:
    with _connect() as conn:
        conn.execute(
            "UPDATE competitors SET consecutive_failures = consecutive_failures + 1 WHERE id = ?",
            (competitor_id,),
        )
        row = conn.execute(
            "SELECT consecutive_failures FROM competitors WHERE id = ?", (competitor_id,)
        ).fetchone()
        return int(row["consecutive_failures"]) if row else 0


def reset_failures(competitor_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE competitors SET consecutive_failures = 0 WHERE id = ?", (competitor_id,)
        )


def get_extra_sources(competitor_id: int) -> List[str]:
    comp = get_competitor(competitor_id)
    if not comp:
        return []
    try:
        return json.loads(comp["extra_sources"] or "[]")
    except (TypeError, ValueError):
        return []


def add_extra_source(competitor_id: int, url: str) -> None:
    sources = get_extra_sources(competitor_id)
    if url not in sources:
        sources.append(url)
        update_competitor_config(competitor_id, extra_sources=json.dumps(sources))


# ------------------------------------------------------------------ tags ----

def get_or_create_tag(user_id: int, name: str) -> int:
    name = name.strip()
    with _connect() as conn:
        row = conn.execute(
            "SELECT id FROM tags WHERE user_id = ? AND name = ?", (user_id, name)
        ).fetchone()
        if row:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO tags (user_id, name) VALUES (?, ?)", (user_id, name)
        )
        return cur.lastrowid


def get_tags(user_id: int) -> List[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM tags WHERE user_id = ? ORDER BY name", (user_id,)
        ).fetchall()


def get_competitor_tags(competitor_id: int) -> List[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            """SELECT tags.* FROM tags
               JOIN competitor_tags ON competitor_tags.tag_id = tags.id
               WHERE competitor_tags.competitor_id = ? ORDER BY tags.name""",
            (competitor_id,),
        ).fetchall()


def set_competitor_tags(competitor_id: int, user_id: int, tag_names: Sequence[str]) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM competitor_tags WHERE competitor_id = ?", (competitor_id,))
        for raw in tag_names:
            name = raw.strip()
            if not name:
                continue
            existing = conn.execute(
                "SELECT id FROM tags WHERE user_id = ? AND name = ?", (user_id, name)
            ).fetchone()
            if existing:
                tag_id = existing["id"]
            else:
                tag_id = conn.execute(
                    "INSERT INTO tags (user_id, name) VALUES (?, ?)", (user_id, name)
                ).lastrowid
            conn.execute(
                "INSERT OR IGNORE INTO competitor_tags (competitor_id, tag_id) VALUES (?, ?)",
                (competitor_id, tag_id),
            )


def competitors_with_tag(user_id: int, tag_name: str) -> List[int]:
    with _connect() as conn:
        rows = conn.execute(
            """SELECT competitors.id FROM competitors
               JOIN competitor_tags ON competitor_tags.competitor_id = competitors.id
               JOIN tags ON tags.id = competitor_tags.tag_id
               WHERE competitors.user_id = ? AND competitors.active = 1 AND tags.name = ?""",
            (user_id, tag_name),
        ).fetchall()
        return [r["id"] for r in rows]


# ----------------------------------------------------------- saved views ----

def create_saved_view(user_id: int, name: str, filters: Dict) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO saved_views (user_id, name, filters, created_at) VALUES (?, ?, ?, ?)",
            (user_id, name.strip(), json.dumps(filters), _now()),
        )
        return cur.lastrowid


def get_saved_views(user_id: int) -> List[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM saved_views WHERE user_id = ? ORDER BY name", (user_id,)
        ).fetchall()


def get_saved_view(view_id: int) -> Optional[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute("SELECT * FROM saved_views WHERE id = ?", (view_id,)).fetchone()


def delete_saved_view(view_id: int, user_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "DELETE FROM saved_views WHERE id = ? AND user_id = ?", (view_id, user_id)
        )


# --------------------------------------------------------------- digests ----

def save_digest(user_id: int, title: str, content: str, period_days: int = 7) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO digests (user_id, title, content, period_days, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, title, content, period_days, _now()),
        )
        return cur.lastrowid


def get_digests(user_id: int, limit: int = 30) -> List[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM digests WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()


def get_digest(digest_id: int) -> Optional[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute("SELECT * FROM digests WHERE id = ?", (digest_id,)).fetchone()


# ----------------------------------------------------------- suggestions ----

def add_suggestion(competitor_id: int, kind: str, payload: Dict, reason: str) -> int:
    with _connect() as conn:
        # Deduplicate pending suggestions of the same kind for this competitor.
        existing = conn.execute(
            """SELECT id FROM suggestions
               WHERE competitor_id = ? AND kind = ? AND status = 'pending'""",
            (competitor_id, kind),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE suggestions SET payload = ?, reason = ?, created_at = ? WHERE id = ?",
                (json.dumps(payload), reason, _now(), existing["id"]),
            )
            return existing["id"]
        cur = conn.execute(
            """INSERT INTO suggestions (competitor_id, kind, payload, reason, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (competitor_id, kind, json.dumps(payload), reason, _now()),
        )
        return cur.lastrowid


def get_pending_suggestions(competitor_id: int) -> List[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            """SELECT * FROM suggestions
               WHERE competitor_id = ? AND status = 'pending' ORDER BY created_at DESC""",
            (competitor_id,),
        ).fetchall()


def get_user_pending_suggestions(user_id: int) -> List[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            """SELECT suggestions.*, competitors.name AS competitor_name
               FROM suggestions JOIN competitors ON competitors.id = suggestions.competitor_id
               WHERE competitors.user_id = ? AND suggestions.status = 'pending'
               ORDER BY suggestions.created_at DESC""",
            (user_id,),
        ).fetchall()


def resolve_suggestion(suggestion_id: int, status: str) -> Optional[sqlite3.Row]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE suggestions SET status = ? WHERE id = ?", (status, suggestion_id)
        )
        return row


# ---------------------------------------------------- discovered sources ----

def add_discovered_source(competitor_id: int, url: str, kind: str) -> Optional[int]:
    with _connect() as conn:
        existing = conn.execute(
            "SELECT id FROM discovered_sources WHERE competitor_id = ? AND url = ?",
            (competitor_id, url),
        ).fetchone()
        if existing:
            return None
        cur = conn.execute(
            """INSERT INTO discovered_sources (competitor_id, url, kind, created_at)
               VALUES (?, ?, ?, ?)""",
            (competitor_id, url, kind, _now()),
        )
        return cur.lastrowid


def get_pending_sources(competitor_id: int) -> List[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            """SELECT * FROM discovered_sources
               WHERE competitor_id = ? AND status = 'pending' ORDER BY kind""",
            (competitor_id,),
        ).fetchall()


def resolve_discovered_source(source_id: int, status: str) -> Optional[sqlite3.Row]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM discovered_sources WHERE id = ?", (source_id,)
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE discovered_sources SET status = ? WHERE id = ?", (status, source_id)
        )
        return row


# --------------------------------------------------------------- patterns ----

def add_pattern(competitor_id: int, run_id: Optional[int], message: str, category: str = "") -> int:
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO patterns (competitor_id, run_id, message, category, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (competitor_id, run_id, message, category, _now()),
        )
        return cur.lastrowid


def get_patterns(competitor_id: int, limit: int = 20) -> List[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM patterns WHERE competitor_id = ? ORDER BY created_at DESC LIMIT ?",
            (competitor_id, limit),
        ).fetchall()


# ------------------------------------------------------------------ runs ----

def create_run(competitor_id: Optional[int], trigger: str = "manual") -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO runs (competitor_id, started_at, trigger) VALUES (?, ?, ?)",
            (competitor_id, _now(), trigger),
        )
        return cur.lastrowid


def finish_run(
    run_id: int,
    status: str,
    logs: List[str],
    agent_status: Dict,
    tokens_used: int = 0,
) -> None:
    with _connect() as conn:
        conn.execute(
            """UPDATE runs SET finished_at = ?, status = ?, logs = ?, agent_status = ?,
               tokens_used = ? WHERE id = ?""",
            (
                _now(),
                status,
                json.dumps(logs, ensure_ascii=False),
                json.dumps(agent_status, ensure_ascii=False),
                max(0, int(tokens_used or 0)),
                run_id,
            ),
        )


def get_run(run_id: int) -> Optional[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()


def get_runs(competitor_id: int, limit: int = 50) -> List[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM runs WHERE competitor_id = ? ORDER BY started_at DESC LIMIT ?",
            (competitor_id, limit),
        ).fetchall()


# ------------------------------------------------------- run artefacts ----

def add_insights(run_id: int, competitor_id: Optional[int], items: List[Dict]) -> None:
    with _connect() as conn:
        conn.executemany(
            """INSERT INTO insights
               (run_id, competitor_id, category, text, severity, confidence,
                triage_reason, needs_review, roadmap_match, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    run_id,
                    competitor_id,
                    it["category"],
                    it["text"],
                    it.get("severity", "low"),
                    float(it.get("confidence", 1.0)),
                    it.get("triage_reason", ""),
                    1 if it.get("needs_review") else 0,
                    (it.get("roadmap_match") or "")[:240],
                    _now(),
                )
                for it in items
            ],
        )


def add_diff(run_id: int, competitor_id: Optional[int], url: str, status: str, diff_lines: List[str]) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO diffs (run_id, competitor_id, url, status, diff, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, competitor_id, url, status, json.dumps(diff_lines, ensure_ascii=False), _now()),
        )


def add_section_briefs(run_id: int, competitor_id: Optional[int], sections: List[Dict]) -> None:
    if not sections:
        return
    with _connect() as conn:
        conn.executemany(
            """INSERT INTO section_briefs
               (run_id, competitor_id, section, summary, bullets, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                (
                    run_id,
                    competitor_id,
                    s.get("section", "Other signals"),
                    s.get("summary", ""),
                    json.dumps(s.get("bullets") or [], ensure_ascii=False),
                    _now(),
                )
                for s in sections
            ],
        )


def add_news_signals(run_id: int, competitor_id: Optional[int], items: List[Dict]) -> None:
    if not items:
        return
    with _connect() as conn:
        conn.executemany(
            """INSERT INTO news_signals
               (run_id, competitor_id, signal_type, title, url, summary, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    run_id,
                    competitor_id,
                    it.get("signal_type", "other"),
                    it.get("title", "")[:300],
                    it.get("url", ""),
                    it.get("summary", "")[:400],
                    _now(),
                )
                for it in items
            ],
        )


def get_run_sections(run_id: int) -> List[Dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM section_briefs WHERE run_id = ? ORDER BY id",
            (run_id,),
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        try:
            item["bullets"] = json.loads(item.get("bullets") or "[]")
        except (TypeError, ValueError):
            item["bullets"] = []
        out.append(item)
    return out


def get_run_news(run_id: int) -> List[Dict]:
    with _connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM news_signals WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
        ]


def get_competitor_sections(competitor_id: int, days: int = 14, limit: int = 40) -> List[Dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _connect() as conn:
        rows = conn.execute(
            """SELECT * FROM section_briefs
               WHERE competitor_id = ? AND created_at >= ?
               ORDER BY created_at DESC LIMIT ?""",
            (competitor_id, since, limit),
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        try:
            item["bullets"] = json.loads(item.get("bullets") or "[]")
        except (TypeError, ValueError):
            item["bullets"] = []
        out.append(item)
    return out


def get_competitor_news(competitor_id: int, days: int = 14, limit: int = 30) -> List[Dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                """SELECT * FROM news_signals
                   WHERE competitor_id = ? AND created_at >= ?
                   ORDER BY created_at DESC LIMIT ?""",
                (competitor_id, since, limit),
            ).fetchall()
        ]


def pm_briefing_data(user_id: int, days: int = 7) -> List[Dict]:
    """Unified PM briefing: sections, insights by category, news, patterns per competitor."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    from agents.categories import INSIGHT_CATEGORIES, PM_SECTIONS, SIGNAL_LABELS

    result: List[Dict] = []
    with _connect() as conn:
        for comp in conn.execute(
            "SELECT * FROM competitors WHERE user_id = ? AND active = 1 ORDER BY name",
            (user_id,),
        ).fetchall():
            cid = comp["id"]
            insights = [
                dict(r)
                for r in conn.execute(
                    """SELECT * FROM insights WHERE competitor_id = ? AND created_at >= ?
                       ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                                created_at DESC""",
                    (cid, since),
                ).fetchall()
            ]
            by_category = {c: [] for c in INSIGHT_CATEGORIES}
            for ins in insights:
                cat = ins["category"] if ins["category"] in by_category else "Other"
                by_category[cat].append(ins)

            section_rows = conn.execute(
                """SELECT section, summary, bullets, created_at FROM section_briefs
                   WHERE competitor_id = ? AND created_at >= ?
                   ORDER BY created_at DESC LIMIT 40""",
                (cid, since),
            ).fetchall()
            sections_map: Dict[str, Dict] = {s: {"summary": "", "bullets": []} for s in PM_SECTIONS}
            for row in section_rows:
                sec = row["section"] if row["section"] in sections_map else "Other signals"
                if not sections_map[sec]["summary"]:
                    sections_map[sec]["summary"] = row["summary"] or ""
                try:
                    bullets = json.loads(row["bullets"] or "[]")
                except (TypeError, ValueError):
                    bullets = []
                for b in bullets:
                    if b not in sections_map[sec]["bullets"] and len(sections_map[sec]["bullets"]) < 5:
                        sections_map[sec]["bullets"].append(b)

            news = [
                {**dict(r), "signal_label": SIGNAL_LABELS.get(r["signal_type"], "News")}
                for r in conn.execute(
                    """SELECT * FROM news_signals WHERE competitor_id = ? AND created_at >= ?
                       ORDER BY created_at DESC LIMIT 12""",
                    (cid, since),
                ).fetchall()
            ]
            patterns = [
                dict(r)
                for r in conn.execute(
                    """SELECT * FROM patterns WHERE competitor_id = ? AND created_at >= ?
                       ORDER BY created_at DESC LIMIT 5""",
                    (cid, since),
                ).fetchall()
            ]
            result.append({
                "competitor": dict(comp),
                "insights": insights,
                "by_category": by_category,
                "sections": [
                    {"section": s, **sections_map[s]}
                    for s in PM_SECTIONS
                    if sections_map[s]["summary"] or sections_map[s]["bullets"]
                ],
                "news": news,
                "patterns": patterns,
                "high_count": sum(1 for i in insights if i.get("severity") == "high"),
            })
    return result


def add_social_items(run_id: int, competitor_id: Optional[int], source: str, items: List[Dict]) -> None:
    with _connect() as conn:
        conn.executemany(
            """INSERT INTO social_items (run_id, competitor_id, source, title, url, content, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [(run_id, competitor_id, source, it.get("title", ""), it.get("url", ""),
              it.get("content", ""), _now()) for it in items],
        )


def get_run_insights(run_id: int) -> List[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM insights WHERE run_id = ? ORDER BY "
            "CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, "
            "needs_review DESC",
            (run_id,),
        ).fetchall()


def get_run_diffs(run_id: int) -> List[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute("SELECT * FROM diffs WHERE run_id = ?", (run_id,)).fetchall()


def get_run_social(run_id: int) -> List[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute("SELECT * FROM social_items WHERE run_id = ?", (run_id,)).fetchall()


def get_recent_insights(competitor_id: int, days: int = 14, limit: int = 100) -> List[sqlite3.Row]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _connect() as conn:
        return conn.execute(
            """SELECT * FROM insights WHERE competitor_id = ? AND created_at >= ?
               ORDER BY created_at DESC LIMIT ?""",
            (competitor_id, since, limit),
        ).fetchall()


# ---------------------------------------------------------------- alerts ----

def add_alert(competitor_id: Optional[int], run_id: int, message: str, severity: str = "high") -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO alerts (competitor_id, run_id, severity, message, created_at) VALUES (?, ?, ?, ?, ?)",
            (competitor_id, run_id, severity, message, _now()),
        )
        return cur.lastrowid


def get_alerts(user_id: int, unseen_only: bool = False, limit: int = 30) -> List[sqlite3.Row]:
    query = """SELECT alerts.*, competitors.name AS competitor_name
               FROM alerts JOIN competitors ON competitors.id = alerts.competitor_id
               WHERE competitors.user_id = ?"""
    if unseen_only:
        query += " AND alerts.seen = 0"
    query += " ORDER BY alerts.created_at DESC LIMIT ?"
    with _connect() as conn:
        return conn.execute(query, (user_id, limit)).fetchall()


def mark_alerts_seen(user_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            """UPDATE alerts SET seen = 1 WHERE competitor_id IN
               (SELECT id FROM competitors WHERE user_id = ?)""",
            (user_id,),
        )


# ------------------------------------------------------ health indicator ----

def competitor_health(competitor: Dict, last_run: Optional[Dict],
                      days_since_change: Optional[int], failure_threshold: int = 3) -> Dict:
    """Return {status, label, detail} for dashboard badges."""
    if competitor.get("paused"):
        return {"status": "paused", "label": "paused", "detail": "Checks paused"}
    failures = int(competitor.get("consecutive_failures") or 0)
    if failures >= failure_threshold:
        return {
            "status": "failing",
            "label": "fetch failing",
            "detail": f"{failures} consecutive failures — check the site or sources",
        }
    if last_run is None:
        return {"status": "new", "label": "not checked yet", "detail": "Run Check now to start"}
    run_status = (last_run.get("status") or "").lower()
    if run_status in ("error", "failed"):
        return {
            "status": "failing",
            "label": "last check failed",
            "detail": "Open the run log for details",
        }
    # Overdue vs configured interval
    try:
        started = datetime.fromisoformat(last_run["started_at"])
        hours = int(competitor.get("interval_hours") or 24)
        age_h = (datetime.now(timezone.utc) - started).total_seconds() / 3600.0
        if age_h > max(hours * 1.75, hours + 6):
            return {
                "status": "stale",
                "label": "check overdue",
                "detail": f"Last check {int(age_h)}h ago (interval {hours}h)",
            }
    except (TypeError, ValueError, KeyError):
        pass
    if days_since_change is not None and days_since_change >= 30:
        return {
            "status": "stale",
            "label": "quiet 30+ days",
            "detail": "Still tracking — no product/page changes lately",
        }
    return {"status": "active", "label": "healthy", "detail": "On schedule"}


# ------------------------------------------------------ dashboard queries ----

def competitor_overview(user_id: int, tag: Optional[str] = None) -> List[Dict]:
    """Dashboard rows: competitor + last run + latest high insight + weekly change count + health."""
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    thirty_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    rows: List[Dict] = []
    with _connect() as conn:
        comps = conn.execute(
            "SELECT * FROM competitors WHERE user_id = ? AND active = 1 ORDER BY name", (user_id,)
        ).fetchall()
        if tag:
            allowed = set(competitors_with_tag(user_id, tag))
            comps = [c for c in comps if c["id"] in allowed]

        for comp in comps:
            last_run = conn.execute(
                "SELECT * FROM runs WHERE competitor_id = ? ORDER BY started_at DESC LIMIT 1",
                (comp["id"],),
            ).fetchone()
            top_insight = conn.execute(
                """SELECT * FROM insights WHERE competitor_id = ? AND severity = 'high'
                   ORDER BY created_at DESC LIMIT 1""",
                (comp["id"],),
            ).fetchone()
            week_changes = conn.execute(
                """SELECT COUNT(*) AS n FROM insights
                   WHERE competitor_id = ? AND created_at >= ?""",
                (comp["id"], week_ago),
            ).fetchone()["n"]
            week_diffs = conn.execute(
                """SELECT COUNT(*) AS n FROM diffs
                   WHERE competitor_id = ? AND status = 'changed' AND created_at >= ?""",
                (comp["id"], week_ago),
            ).fetchone()["n"]
            last_change = conn.execute(
                """SELECT created_at FROM insights WHERE competitor_id = ?
                   ORDER BY created_at DESC LIMIT 1""",
                (comp["id"],),
            ).fetchone()
            last_diff_change = conn.execute(
                """SELECT created_at FROM diffs WHERE competitor_id = ? AND status = 'changed'
                   ORDER BY created_at DESC LIMIT 1""",
                (comp["id"],),
            ).fetchone()
            change_times = [r["created_at"] for r in (last_change, last_diff_change) if r]
            days_since = None
            if change_times:
                latest = max(change_times)
                try:
                    days_since = (datetime.now(timezone.utc) - datetime.fromisoformat(latest)).days
                except ValueError:
                    days_since = None
            elif last_run and last_run["started_at"] < thirty_ago:
                days_since = 30

            tags = conn.execute(
                """SELECT tags.name FROM tags
                   JOIN competitor_tags ON competitor_tags.tag_id = tags.id
                   WHERE competitor_tags.competitor_id = ?""",
                (comp["id"],),
            ).fetchall()
            health = competitor_health(
                dict(comp),
                dict(last_run) if last_run else None,
                days_since,
            )
            rows.append({
                "competitor": dict(comp),
                "last_run": dict(last_run) if last_run else None,
                "top_insight": dict(top_insight) if top_insight else None,
                "week_changes": week_changes + week_diffs,
                "tags": [t["name"] for t in tags],
                "health": health,
            })
    return rows


def competitor_timeline(competitor_id: int, limit: int = 200) -> List[Dict]:
    """Chronological feed of every detected change across all sources."""
    events: List[Dict] = []
    with _connect() as conn:
        for row in conn.execute(
            "SELECT * FROM insights WHERE competitor_id = ? ORDER BY created_at DESC LIMIT ?",
            (competitor_id, limit),
        ):
            events.append({
                "type": "insight", "created_at": row["created_at"], "run_id": row["run_id"],
                "id": row["id"],
                "category": row["category"], "text": row["text"], "severity": row["severity"],
                "confidence": row["confidence"] if "confidence" in row.keys() else 1.0,
                "triage_reason": row["triage_reason"] if "triage_reason" in row.keys() else "",
                "needs_review": row["needs_review"] if "needs_review" in row.keys() else 0,
                "roadmap_match": row["roadmap_match"] if "roadmap_match" in row.keys() else "",
            })
        for row in conn.execute(
            "SELECT * FROM diffs WHERE competitor_id = ? AND status IN ('changed','failed') "
            "ORDER BY created_at DESC LIMIT ?",
            (competitor_id, limit),
        ):
            events.append({"type": "diff", "created_at": row["created_at"], "run_id": row["run_id"],
                           "url": row["url"], "status": row["status"],
                           "diff": json.loads(row["diff"] or "[]")})
        for row in conn.execute(
            "SELECT * FROM social_items WHERE competitor_id = ? ORDER BY created_at DESC LIMIT ?",
            (competitor_id, limit),
        ):
            events.append({"type": row["source"], "created_at": row["created_at"], "run_id": row["run_id"],
                           "title": row["title"], "url": row["url"], "content": row["content"]})
        for row in conn.execute(
            "SELECT * FROM patterns WHERE competitor_id = ? ORDER BY created_at DESC LIMIT ?",
            (competitor_id, 20),
        ):
            events.append({"type": "pattern", "created_at": row["created_at"], "run_id": row["run_id"],
                           "text": row["message"], "category": row["category"], "severity": "medium"})
        for row in conn.execute(
            "SELECT * FROM news_signals WHERE competitor_id = ? ORDER BY created_at DESC LIMIT ?",
            (competitor_id, limit),
        ):
            events.append({
                "type": "news",
                "created_at": row["created_at"],
                "run_id": row["run_id"],
                "title": row["title"],
                "url": row["url"],
                "content": row["summary"],
                "signal_type": row["signal_type"],
                "category": row["signal_type"],
            })
        for row in conn.execute(
            "SELECT * FROM section_briefs WHERE competitor_id = ? ORDER BY created_at DESC LIMIT ?",
            (competitor_id, 30),
        ):
            try:
                bullets = json.loads(row["bullets"] or "[]")
            except (TypeError, ValueError):
                bullets = []
            preview = row["summary"] or (bullets[0] if bullets else row["section"])
            events.append({
                "type": "section",
                "created_at": row["created_at"],
                "run_id": row["run_id"],
                "section": row["section"],
                "text": preview,
                "bullets": bullets,
                "category": row["section"],
            })
    events.sort(key=lambda e: e["created_at"], reverse=True)
    return events[:limit]


def user_activity_feed(user_id: int, limit: int = 60, tag: Optional[str] = None,
                       severity: Optional[str] = None, category: Optional[str] = None,
                       days: Optional[int] = None, needs_review: Optional[bool] = None,
                       annotated_only: Optional[bool] = None,
                       roadmap_only: Optional[bool] = None) -> List[Dict]:
    """Recent detected changes across all of a user's competitors, with optional filters."""
    events: List[Dict] = []
    since = None
    if days:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    with _connect() as conn:
        comps = {
            row["id"]: row
            for row in conn.execute(
                "SELECT * FROM competitors WHERE user_id = ? AND active = 1", (user_id,)
            ).fetchall()
        }
        if tag:
            allowed = set(competitors_with_tag(user_id, tag))
            comps = {k: v for k, v in comps.items() if k in allowed}
        if not comps:
            return []
        placeholders = ",".join("?" * len(comps))
        ids = tuple(comps)

        insight_q = f"SELECT * FROM insights WHERE competitor_id IN ({placeholders})"
        params: list = list(ids)
        if since:
            insight_q += " AND created_at >= ?"
            params.append(since)
        if severity:
            insight_q += " AND severity = ?"
            params.append(severity)
        if category:
            insight_q += " AND category = ?"
            params.append(category)
        if needs_review:
            insight_q += " AND needs_review = 1"
        if roadmap_only:
            insight_q += " AND roadmap_match IS NOT NULL AND TRIM(roadmap_match) != ''"
        insight_q += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        notes_by_id = {
            r["insight_id"]: r["note"]
            for r in conn.execute(
                "SELECT insight_id, note FROM insight_notes WHERE user_id = ?",
                (user_id,),
            ).fetchall()
        }

        for row in conn.execute(insight_q, params):
            note = notes_by_id.get(row["id"], "")
            if annotated_only and not note:
                continue
            events.append({
                "type": "insight", "created_at": row["created_at"], "run_id": row["run_id"],
                "id": row["id"],
                "competitor_id": row["competitor_id"], "category": row["category"],
                "text": row["text"], "severity": row["severity"],
                "confidence": row["confidence"] if "confidence" in row.keys() else 1.0,
                "triage_reason": row["triage_reason"] if "triage_reason" in row.keys() else "",
                "needs_review": row["needs_review"] if "needs_review" in row.keys() else 0,
                "roadmap_match": row["roadmap_match"] if "roadmap_match" in row.keys() else "",
                "note": note,
            })

        if not severity and not category and not needs_review and not annotated_only and not roadmap_only:
            diff_q = (f"SELECT * FROM diffs WHERE competitor_id IN ({placeholders}) "
                      f"AND status IN ('changed','failed')")
            diff_params: list = list(ids)
            if since:
                diff_q += " AND created_at >= ?"
                diff_params.append(since)
            diff_q += " ORDER BY created_at DESC LIMIT ?"
            diff_params.append(limit)
            for row in conn.execute(diff_q, diff_params):
                events.append({"type": "diff", "created_at": row["created_at"], "run_id": row["run_id"],
                               "competitor_id": row["competitor_id"], "url": row["url"],
                               "status": row["status"], "diff": json.loads(row["diff"] or "[]")})

            social_q = f"SELECT * FROM social_items WHERE competitor_id IN ({placeholders})"
            social_params: list = list(ids)
            if since:
                social_q += " AND created_at >= ?"
                social_params.append(since)
            social_q += " ORDER BY created_at DESC LIMIT ?"
            social_params.append(limit)
            for row in conn.execute(social_q, social_params):
                events.append({"type": row["source"], "created_at": row["created_at"], "run_id": row["run_id"],
                               "competitor_id": row["competitor_id"], "title": row["title"],
                               "url": row["url"], "content": row["content"]})

    for event in events:
        comp = comps[event["competitor_id"]]
        event["competitor_name"] = comp["name"]
        event["competitor_url"] = comp["url"]
    events.sort(key=lambda e: e["created_at"], reverse=True)
    trimmed = events[:limit]
    insight_ids = [e["id"] for e in trimmed if e.get("type") == "insight" and e.get("id")]
    decisions = decisions_for_insights(user_id, insight_ids)
    for event in trimmed:
        if event.get("type") == "insight" and event.get("id"):
            dec = decisions.get(event["id"])
            if dec:
                event["decision"] = dec.get("decision") or ""
                event["decision_owner"] = dec.get("owner") or ""
            else:
                event["decision"] = ""
                event["decision_owner"] = ""
    return trimmed


def search_competitors(user_id: int, query: str) -> List[Dict]:
    """Match tracked competitors by name or URL."""
    q = f"%{query.strip()}%"
    with _connect() as conn:
        rows = conn.execute(
            """SELECT * FROM competitors
               WHERE user_id = ? AND active = 1
                 AND (name LIKE ? OR url LIKE ?)
               ORDER BY name COLLATE NOCASE""",
            (user_id, q, q),
        ).fetchall()
        return [dict(r) for r in rows]


def search_changes(user_id: int, query: str, limit: int = 80) -> Dict[str, List[Dict]]:
    """Full-text-ish LIKE search across insights, diffs, and social items, grouped by competitor."""
    q = f"%{query.strip()}%"
    grouped: Dict[str, List[Dict]] = {}
    with _connect() as conn:
        comps = {
            row["id"]: row
            for row in conn.execute(
                "SELECT * FROM competitors WHERE user_id = ? AND active = 1", (user_id,)
            ).fetchall()
        }
        if not comps:
            return {}
        placeholders = ",".join("?" * len(comps))
        ids = tuple(comps)

        for row in conn.execute(
            f"""SELECT * FROM insights WHERE competitor_id IN ({placeholders})
                AND (text LIKE ? OR category LIKE ?) ORDER BY created_at DESC LIMIT ?""",
            (*ids, q, q, limit),
        ):
            name = comps[row["competitor_id"]]["name"]
            grouped.setdefault(name, []).append({
                "type": "insight", "text": row["text"], "category": row["category"],
                "severity": row["severity"], "run_id": row["run_id"],
                "created_at": row["created_at"], "competitor_id": row["competitor_id"],
                "competitor_url": comps[row["competitor_id"]]["url"],
            })
        for row in conn.execute(
            f"""SELECT * FROM diffs WHERE competitor_id IN ({placeholders})
                AND (url LIKE ? OR diff LIKE ?) ORDER BY created_at DESC LIMIT ?""",
            (*ids, q, q, limit),
        ):
            name = comps[row["competitor_id"]]["name"]
            grouped.setdefault(name, []).append({
                "type": "diff", "text": f"Diff on {row['url']}", "url": row["url"],
                "status": row["status"], "run_id": row["run_id"],
                "created_at": row["created_at"], "competitor_id": row["competitor_id"],
                "competitor_url": comps[row["competitor_id"]]["url"],
            })
        for row in conn.execute(
            f"""SELECT * FROM social_items WHERE competitor_id IN ({placeholders})
                AND (title LIKE ? OR content LIKE ?) ORDER BY created_at DESC LIMIT ?""",
            (*ids, q, q, limit),
        ):
            name = comps[row["competitor_id"]]["name"]
            grouped.setdefault(name, []).append({
                "type": row["source"], "text": row["title"] or row["content"][:160],
                "url": row["url"], "run_id": row["run_id"],
                "created_at": row["created_at"], "competitor_id": row["competitor_id"],
                "competitor_url": comps[row["competitor_id"]]["url"],
            })
    return grouped


def comparison_data(competitor_ids: Sequence[int], days: int = 14) -> Dict[str, Dict[str, List[Dict]]]:
    """Side-by-side recent insights grouped by category for 2–3 competitors."""
    from agents.categories import INSIGHT_CATEGORIES

    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    categories = list(INSIGHT_CATEGORIES)
    result: Dict[str, Dict[str, List[Dict]]] = {}
    with _connect() as conn:
        for cid in competitor_ids:
            comp = conn.execute("SELECT * FROM competitors WHERE id = ?", (cid,)).fetchone()
            if not comp:
                continue
            by_cat = {c: [] for c in categories}
            for row in conn.execute(
                """SELECT * FROM insights WHERE competitor_id = ? AND created_at >= ?
                   ORDER BY created_at DESC""",
                (cid, since),
            ):
                cat = row["category"] if row["category"] in by_cat else "Other"
                by_cat[cat].append(dict(row))
            result[comp["name"]] = {
                "competitor": dict(comp),
                "by_category": by_cat,
            }
    return result


def digest_data(user_id: int, days: int = 7) -> List[Dict]:
    """Per-competitor changes in the last `days` days, for the export digest."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    result: List[Dict] = []
    with _connect() as conn:
        for comp in conn.execute(
            "SELECT * FROM competitors WHERE user_id = ? AND active = 1 ORDER BY name", (user_id,)
        ).fetchall():
            insights = conn.execute(
                """SELECT * FROM insights WHERE competitor_id = ? AND created_at >= ?
                   ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, created_at DESC""",
                (comp["id"], since),
            ).fetchall()
            diffs = conn.execute(
                "SELECT * FROM diffs WHERE competitor_id = ? AND status = 'changed' AND created_at >= ?",
                (comp["id"], since),
            ).fetchall()
            result.append({
                "competitor": dict(comp),
                "insights": [dict(r) for r in insights],
                "diffs": [dict(r) for r in diffs],
            })
    return result


def change_frequency_stats(competitor_id: int, days: int = 14) -> Dict:
    """How often a competitor produced changes over the last N days (for adaptive frequency)."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _connect() as conn:
        runs = conn.execute(
            "SELECT COUNT(*) AS n FROM runs WHERE competitor_id = ? AND started_at >= ?",
            (competitor_id, since),
        ).fetchone()["n"]
        changes = conn.execute(
            """SELECT COUNT(*) AS n FROM insights WHERE competitor_id = ? AND created_at >= ?""",
            (competitor_id, since),
        ).fetchone()["n"]
        diffs = conn.execute(
            """SELECT COUNT(*) AS n FROM diffs
               WHERE competitor_id = ? AND status = 'changed' AND created_at >= ?""",
            (competitor_id, since),
        ).fetchone()["n"]
        month_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        month_changes = conn.execute(
            """SELECT COUNT(*) AS n FROM insights WHERE competitor_id = ? AND created_at >= ?""",
            (competitor_id, month_ago),
        ).fetchone()["n"]
    return {
        "runs": runs,
        "changes": changes + diffs,
        "month_changes": month_changes,
        "days": days,
    }


# ------------------------------------------ roadmap / notes / parity / PM ----

def get_insight(insight_id: int) -> Optional[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute("SELECT * FROM insights WHERE id = ?", (insight_id,)).fetchone()


def get_insight_for_user(insight_id: int, user_id: int) -> Optional[Dict]:
    with _connect() as conn:
        row = conn.execute(
            """SELECT i.*, c.name AS competitor_name, c.url AS competitor_url, c.user_id
               FROM insights i
               JOIN competitors c ON c.id = i.competitor_id
               WHERE i.id = ? AND c.user_id = ? AND c.active = 1""",
            (insight_id, user_id),
        ).fetchone()
        if not row:
            return None
        item = dict(row)
        note = conn.execute(
            "SELECT note FROM insight_notes WHERE insight_id = ? AND user_id = ?",
            (insight_id, user_id),
        ).fetchone()
        item["note"] = note["note"] if note else ""
        dec = conn.execute(
            "SELECT decision, owner FROM insight_decisions WHERE insight_id = ? AND user_id = ?",
            (insight_id, user_id),
        ).fetchone()
        item["decision"] = dec["decision"] if dec else ""
        item["decision_owner"] = dec["owner"] if dec else ""
        return item


def set_insight_note(insight_id: int, user_id: int, note: str) -> bool:
    """Save or clear a private note. Returns False if insight not owned by user."""
    owned = get_insight_for_user(insight_id, user_id)
    if owned is None:
        return False
    text = (note or "").strip()
    with _connect() as conn:
        if not text:
            conn.execute(
                "DELETE FROM insight_notes WHERE insight_id = ? AND user_id = ?",
                (insight_id, user_id),
            )
        else:
            conn.execute(
                """INSERT INTO insight_notes (insight_id, user_id, note, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(insight_id) DO UPDATE SET
                     note = excluded.note,
                     updated_at = excluded.updated_at,
                     user_id = excluded.user_id""",
                (insight_id, user_id, text[:2000], _now()),
            )
    return True


_VALID_DECISIONS = {"monitor", "respond", "ignore", ""}


def set_insight_decision(
    insight_id: int, user_id: int, decision: str, owner: str = ""
) -> bool:
    """Record PM decision on an insight. Empty decision clears the record."""
    owned = get_insight_for_user(insight_id, user_id)
    if owned is None:
        return False
    decision = (decision or "").strip().lower()
    if decision not in _VALID_DECISIONS:
        return False
    owner = (owner or "").strip()[:80]
    with _connect() as conn:
        if not decision:
            conn.execute(
                "DELETE FROM insight_decisions WHERE insight_id = ? AND user_id = ?",
                (insight_id, user_id),
            )
        else:
            conn.execute(
                """INSERT INTO insight_decisions
                   (insight_id, user_id, decision, owner, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(insight_id) DO UPDATE SET
                     decision = excluded.decision,
                     owner = excluded.owner,
                     updated_at = excluded.updated_at,
                     user_id = excluded.user_id""",
                (insight_id, user_id, decision, owner, _now()),
            )
    return True


def decisions_for_insights(user_id: int, insight_ids: Sequence[int]) -> Dict[int, Dict]:
    if not insight_ids:
        return {}
    placeholders = ",".join("?" * len(insight_ids))
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT insight_id, decision, owner FROM insight_decisions
                WHERE user_id = ? AND insight_id IN ({placeholders})""",
            (user_id, *insight_ids),
        ).fetchall()
    return {
        int(r["insight_id"]): {"decision": r["decision"], "owner": r["owner"] or ""}
        for r in rows
    }


def build_daily_brief(user_id: int, hours: int = 48) -> Dict:
    """Morning ritual payload: top changes, roadmap overlaps, health issues, news."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    overview = competitor_overview(user_id)
    health_issues = [
        {
            "competitor_id": row["competitor"]["id"],
            "name": row["competitor"]["name"],
            "status": row["health"]["status"],
            "label": row["health"]["label"],
            "detail": row["health"].get("detail") or "",
        }
        for row in overview
        if row["health"]["status"] in ("failing", "stale", "new")
    ]
    with _connect() as conn:
        comps = {
            r["id"]: r
            for r in conn.execute(
                "SELECT id, name, url FROM competitors WHERE user_id = ? AND active = 1",
                (user_id,),
            ).fetchall()
        }
        if not comps:
            return {
                "hours": hours,
                "top_changes": [],
                "roadmap_hits": [],
                "news": [],
                "health_issues": [],
                "counts": {"changes": 0, "high": 0, "roadmap": 0, "news": 0, "health": 0},
            }
        ids = tuple(comps.keys())
        ph = ",".join("?" * len(ids))
        insights = conn.execute(
            f"""SELECT * FROM insights
                WHERE competitor_id IN ({ph}) AND created_at >= ?
                ORDER BY
                  CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                  created_at DESC
                LIMIT 40""",
            (*ids, since),
        ).fetchall()
        news_rows = conn.execute(
            f"""SELECT * FROM news_signals
                WHERE competitor_id IN ({ph}) AND created_at >= ?
                ORDER BY created_at DESC LIMIT 8""",
            (*ids, since),
        ).fetchall()

    top_changes = []
    roadmap_hits = []
    high = 0
    for row in insights:
        item = {
            "id": row["id"],
            "competitor_id": row["competitor_id"],
            "competitor_name": comps[row["competitor_id"]]["name"],
            "text": row["text"],
            "severity": row["severity"],
            "category": row["category"],
            "roadmap_match": row["roadmap_match"] if "roadmap_match" in row.keys() else "",
            "created_at": row["created_at"],
            "run_id": row["run_id"],
        }
        if row["severity"] == "high":
            high += 1
        if item["roadmap_match"]:
            roadmap_hits.append(item)
        if len(top_changes) < 5:
            top_changes.append(item)

    news = []
    for row in news_rows[:5]:
        cid = row["competitor_id"]
        cname = comps[cid]["name"] if cid in comps else ""
        news.append({
            "competitor_name": cname,
            "title": row["title"],
            "url": row["url"],
            "signal_type": row["signal_type"],
            "created_at": row["created_at"],
        })

    return {
        "hours": hours,
        "top_changes": top_changes,
        "roadmap_hits": roadmap_hits[:5],
        "news": news,
        "health_issues": health_issues[:6],
        "counts": {
            "changes": len(insights),
            "high": high,
            "roadmap": len(roadmap_hits),
            "news": len(news),
            "health": len(health_issues),
        },
    }


def notes_for_insights(user_id: int, insight_ids: Sequence[int]) -> Dict[int, str]:
    if not insight_ids:
        return {}
    placeholders = ",".join("?" * len(insight_ids))
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT insight_id, note FROM insight_notes
                WHERE user_id = ? AND insight_id IN ({placeholders})""",
            (user_id, *insight_ids),
        ).fetchall()
    return {r["insight_id"]: r["note"] for r in rows}


def pricing_history(competitor_id: int, limit: int = 100) -> List[Dict]:
    with _connect() as conn:
        rows = conn.execute(
            """SELECT * FROM insights
               WHERE competitor_id = ? AND category = 'Pricing Change'
               ORDER BY created_at DESC LIMIT ?""",
            (competitor_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def pricing_change_counts_by_week(competitor_id: int, weeks: int = 12) -> List[Dict]:
    """Sparkline data: [{week_start, count}, ...] oldest → newest."""
    since = (datetime.now(timezone.utc) - timedelta(weeks=weeks)).isoformat()
    with _connect() as conn:
        rows = conn.execute(
            """SELECT created_at FROM insights
               WHERE competitor_id = ? AND category = 'Pricing Change' AND created_at >= ?""",
            (competitor_id, since),
        ).fetchall()
    buckets: Dict[str, int] = {}
    now = datetime.now(timezone.utc)
    for i in range(weeks):
        start = (now - timedelta(weeks=weeks - 1 - i)).date()
        # Monday-based week label
        monday = start - timedelta(days=start.weekday())
        buckets[monday.isoformat()] = 0
    for row in rows:
        try:
            dt = datetime.fromisoformat(row["created_at"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            monday = (dt.date() - timedelta(days=dt.weekday())).isoformat()
            if monday in buckets:
                buckets[monday] += 1
        except (TypeError, ValueError):
            continue
    return [{"week_start": k, "count": buckets[k]} for k in sorted(buckets.keys())]


def feature_parity_matrix(user_id: int) -> Dict:
    """Rows = roadmap features, columns = competitors, cells = status + evidence."""
    features = get_roadmap_items(user_id)
    comps = [dict(c) for c in get_competitors(user_id, include_paused=True) if c["active"]]
    recent_cut = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    cells: Dict[str, Dict[int, Dict]] = {}
    with _connect() as conn:
        for feature in features:
            cells[feature] = {}
            for comp in comps:
                # Prefer explicit roadmap_match; also accept loose text overlap for older rows
                rows = conn.execute(
                    """SELECT * FROM insights
                       WHERE competitor_id = ?
                         AND (
                           lower(roadmap_match) = lower(?)
                           OR (roadmap_match = '' AND (
                             lower(text) LIKE ? OR lower(text) LIKE ?
                           ))
                         )
                       ORDER BY created_at DESC LIMIT 20""",
                    (
                        comp["id"],
                        feature,
                        f"%{feature.lower()}%",
                        f"%{feature.lower().split(':')[-1].strip()}%" if ":" in feature else f"%{feature.lower()}%",
                    ),
                ).fetchall()
                evidence = [dict(r) for r in rows]
                # Filter weak LIKE false positives when no roadmap_match
                filtered = []
                feat_tokens = {t for t in re.findall(r"[a-z0-9]+", feature.lower()) if len(t) > 2}
                for ev in evidence:
                    if (ev.get("roadmap_match") or "").strip():
                        filtered.append(ev)
                        continue
                    text_tokens = {t for t in re.findall(r"[a-z0-9]+", (ev.get("text") or "").lower()) if len(t) > 2}
                    if feat_tokens and len(feat_tokens & text_tokens) >= max(1, len(feat_tokens) // 2):
                        filtered.append(ev)
                if not filtered:
                    cells[feature][comp["id"]] = {
                        "status": "no_signal",
                        "label": "No signal yet",
                        "evidence": [],
                    }
                else:
                    latest = filtered[0]["created_at"]
                    status = "recent" if latest >= recent_cut else "has"
                    cells[feature][comp["id"]] = {
                        "status": status,
                        "label": "Recently added" if status == "recent" else "Has it",
                        "evidence": filtered[:8],
                        "latest_at": latest,
                    }
    return {"features": features, "competitors": comps, "cells": cells}


def insights_in_range(user_id: int, range_start: str, range_end: str) -> List[Dict]:
    with _connect() as conn:
        rows = conn.execute(
            """SELECT i.*, c.name AS competitor_name, c.url AS competitor_url
               FROM insights i
               JOIN competitors c ON c.id = i.competitor_id
               WHERE c.user_id = ? AND c.active = 1
                 AND i.created_at >= ? AND i.created_at <= ?
               ORDER BY i.category, i.created_at DESC""",
            (user_id, range_start, range_end),
        ).fetchall()
        return [dict(r) for r in rows]


def get_quarterly_summary(user_id: int, range_start: str, range_end: str) -> Optional[Dict]:
    with _connect() as conn:
        row = conn.execute(
            """SELECT * FROM quarterly_summaries
               WHERE user_id = ? AND range_start = ? AND range_end = ?""",
            (user_id, range_start, range_end),
        ).fetchone()
        return dict(row) if row else None


def save_quarterly_summary(user_id: int, range_start: str, range_end: str, summary: str) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO quarterly_summaries
               (user_id, range_start, range_end, summary, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id, range_start, range_end) DO UPDATE SET
                 summary = excluded.summary,
                 created_at = excluded.created_at""",
            (user_id, range_start, range_end, summary, _now()),
        )


def competitor_snapshot_for_battlecard(competitor_id: int, days: int = 60) -> Dict:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _connect() as conn:
        comp = conn.execute("SELECT * FROM competitors WHERE id = ?", (competitor_id,)).fetchone()
        insights = [
            dict(r)
            for r in conn.execute(
                """SELECT * FROM insights WHERE competitor_id = ? AND created_at >= ?
                   ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                            created_at DESC LIMIT 40""",
                (competitor_id, since),
            ).fetchall()
        ]
        pricing = [
            dict(r)
            for r in conn.execute(
                """SELECT * FROM insights
                   WHERE competitor_id = ? AND category = 'Pricing Change'
                   ORDER BY created_at DESC LIMIT 8""",
                (competitor_id,),
            ).fetchall()
        ]
    by_cat: Dict[str, List[Dict]] = {}
    for item in insights:
        by_cat.setdefault(item["category"], []).append(item)
    return {
        "competitor": dict(comp) if comp else {},
        "by_category": by_cat,
        "pricing": pricing,
        "insights": insights,
        "days": days,
    }


def load_messaging_snapshots() -> Dict[str, List]:
    """Landing-page text snapshots — stored in the DB so they survive host restarts."""
    with _connect() as conn:
        rows = conn.execute("SELECT url, lines FROM messaging_snapshots").fetchall()
    out: Dict[str, List] = {}
    for row in rows:
        try:
            out[row["url"]] = json.loads(row["lines"] or "[]")
        except (TypeError, ValueError):
            out[row["url"]] = []
    return out


def save_messaging_snapshot(url: str, lines: List[str]) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO messaging_snapshots (url, lines, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(url) DO UPDATE SET
                 lines = excluded.lines,
                 updated_at = excluded.updated_at""",
            (url, json.dumps(lines, ensure_ascii=False), _now()),
        )
