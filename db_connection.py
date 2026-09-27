"""SQLite / Turso connection factory.

Local development: stdlib sqlite3 against data/tracktect.db (one connection
per `with _connect()` — unchanged).

Production (Turso env vars set): libsql over HTTP. The libsql Python client
wraps a Tokio runtime and is **not** safe to share across threads. Concurrent
`connect()` / `close()` from Flask + APScheduler deadlocks the worker
(`failed to join thread: Resource deadlock avoided`). Rules:

- Never keep a process-global Turso client.
- One Turso connection per thread, scoped to a request or scheduler job.
- Serialize connect() and close() so two threads never start/join Tokio at once.
- Local SQLite path is untouched.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Mapping
from contextlib import contextmanager
from typing import Any, Iterable, Optional, Sequence

from config import settings

logger = logging.getLogger(__name__)


class DatabaseUnavailable(Exception):
    """Remote DB is required but cannot be reached. Do not use a local file."""


class Row(Mapping):
    """Dict-like row so existing `row['col']` / `dict(row)` code keeps working.

    libsql returns plain tuples; stdlib sqlite3.Row is not settable on that client.
    """

    def __init__(self, columns: Sequence[str], values: Sequence[Any]) -> None:
        self._columns = list(columns)
        self._values = list(values)
        self._map = {name: values[i] for i, name in enumerate(self._columns) if i < len(values)}

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return self._map[key]

    def __iter__(self):
        return iter(self._columns)

    def __len__(self) -> int:
        return len(self._columns)

    def keys(self):
        return self._columns

    def __contains__(self, key) -> bool:
        if isinstance(key, int):
            return 0 <= key < len(self._values)
        return key in self._map


def _columns_from_cursor(cursor) -> list:
    desc = getattr(cursor, "description", None) or ()
    return [d[0] for d in desc]


def _raise_if_constraint(exc: BaseException) -> None:
    text = str(exc).lower()
    if "constraint" in text or "unique" in text:
        raise sqlite3.IntegrityError(str(exc)) from exc


class CursorAdapter:
    def __init__(self, cursor) -> None:
        self._cursor = cursor
        self.lastrowid = getattr(cursor, "lastrowid", None)
        self.rowcount = getattr(cursor, "rowcount", -1)
        self.description = getattr(cursor, "description", None)

    def _wrap(self, row):
        if row is None:
            return None
        if isinstance(row, sqlite3.Row):
            return row
        if isinstance(row, Row):
            return row
        cols = _columns_from_cursor(self._cursor)
        if not cols and isinstance(row, (tuple, list)):
            cols = [str(i) for i in range(len(row))]
        return Row(cols, row)

    def fetchone(self):
        return self._wrap(self._cursor.fetchone())

    def fetchall(self):
        return [self._wrap(r) for r in self._cursor.fetchall()]

    def fetchmany(self, size=None):
        rows = self._cursor.fetchmany(size) if size is not None else self._cursor.fetchmany()
        return [self._wrap(r) for r in rows]

    def execute(self, sql: str, params: Sequence[Any] = ()):
        try:
            self._cursor.execute(sql, params)
        except ValueError as exc:
            _raise_if_constraint(exc)
            raise
        self.lastrowid = getattr(self._cursor, "lastrowid", None)
        self.description = getattr(self._cursor, "description", None)
        return self

    def executemany(self, sql: str, seq_of_params: Iterable[Sequence[Any]]):
        try:
            self._cursor.executemany(sql, seq_of_params)
        except ValueError as exc:
            _raise_if_constraint(exc)
            raise
        self.lastrowid = getattr(self._cursor, "lastrowid", None)
        return self

    def close(self) -> None:
        close = getattr(self._cursor, "close", None)
        if close:
            close()


class ConnectionAdapter:
    def __init__(self, conn, *, remote: bool = False, close_on_exit: bool = True) -> None:
        self._conn = conn
        self.remote = remote
        self.close_on_exit = close_on_exit

    def _adapt_cursor(self, raw) -> CursorAdapter:
        return raw if isinstance(raw, CursorAdapter) else CursorAdapter(raw)

    def execute(self, sql: str, params: Sequence[Any] = ()):
        if self.remote and sql.lstrip().upper().startswith("PRAGMA JOURNAL_MODE"):
            return CursorAdapter(_EmptyCursor())
        try:
            raw = self._conn.execute(sql, params)
        except ValueError as exc:
            _raise_if_constraint(exc)
            raise
        return self._adapt_cursor(raw)

    def executemany(self, sql: str, seq_of_params: Iterable[Sequence[Any]]):
        try:
            raw = self._conn.executemany(sql, seq_of_params)
        except ValueError as exc:
            _raise_if_constraint(exc)
            raise
        return self._adapt_cursor(raw)

    def executescript(self, script: str):
        if hasattr(self._conn, "executescript"):
            try:
                raw = self._conn.executescript(script)
                return self._adapt_cursor(raw) if raw is not None else self
            except Exception:
                # Some remote builds reject multi-statement scripts.
                pass
        last = None
        for statement in _split_sql(script):
            last = self.execute(statement)
        return last

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        rollback = getattr(self._conn, "rollback", None)
        if rollback:
            rollback()

    def close(self) -> None:
        raw = self._conn
        if raw is None:
            return
        self._conn = None
        # close() joins the Tokio worker — must not race another thread's connect().
        with _turso_lifecycle_lock:
            try:
                raw.close()
            except Exception:  # noqa: BLE001
                logger.debug("Turso connection close failed", exc_info=True)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self.commit()
            else:
                self.rollback()
        finally:
            if self.close_on_exit:
                self.close()


class _EmptyCursor:
    description = None
    lastrowid = None

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def fetchmany(self, size=None):
        return []


def _split_sql(script: str) -> list:
    parts = []
    buf = []
    for line in script.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        buf.append(line)
        if stripped.endswith(";"):
            stmt = "\n".join(buf).strip().rstrip(";").strip()
            if stmt:
                parts.append(stmt)
            buf = []
    tail = "\n".join(buf).strip().rstrip(";").strip()
    if tail:
        parts.append(tail)
    return parts


_unavailable = False
_unavailable_reason = ""

# Tokio runtime start/join is process-global and not thread-safe.
_turso_lifecycle_lock = threading.Lock()
# One live Turso client per OS thread (Flask request thread vs APScheduler thread).
_thread_state = threading.local()


class _ScopedCheckout:
    """`with _connect()` during a request/job: commit, but do not destroy the client."""

    def __init__(self, owner: ConnectionAdapter) -> None:
        self._owner = owner

    def __getattr__(self, name):
        return getattr(self._owner, name)

    def __enter__(self):
        return self._owner

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self._owner.commit()
        else:
            self._owner.rollback()


def enter_db_scope() -> None:
    """Mark this thread as owning a Turso connection until exit_db_scope()."""
    depth = getattr(_thread_state, "scope_depth", 0)
    _thread_state.scope_depth = depth + 1


def exit_db_scope() -> None:
    depth = getattr(_thread_state, "scope_depth", 0) - 1
    _thread_state.scope_depth = max(0, depth)
    if depth <= 0:
        release_thread_connection()


def release_thread_connection() -> None:
    """Close this thread's Turso client. No-op for local SQLite."""
    owner = getattr(_thread_state, "conn", None)
    _thread_state.conn = None
    _thread_state.scope_depth = 0
    if owner is not None:
        owner.close()


@contextmanager
def thread_db_scope():
    """Scheduler / init: isolate this thread's Turso client from Flask requests."""
    enter_db_scope()
    try:
        yield
    finally:
        exit_db_scope()


def is_available() -> bool:
    return not _unavailable


def unavailable_reason() -> str:
    return _unavailable_reason


def mark_unavailable(exc: BaseException) -> None:
    global _unavailable, _unavailable_reason
    _unavailable = True
    _unavailable_reason = f"{exc.__class__.__name__}: {exc}"
    logger.error("Database unavailable — not falling back to local disk: %s", _unavailable_reason)


def reset_availability_for_tests() -> None:
    global _unavailable, _unavailable_reason
    _unavailable = False
    _unavailable_reason = ""


def _connect_turso_fresh() -> ConnectionAdapter:
    """Create a brand-new libsql client. Caller must hold _turso_lifecycle_lock."""
    try:
        import libsql
    except ImportError as exc:
        raise DatabaseUnavailable(
            "The libsql package is not installed. Run: pip install libsql"
        ) from exc
    try:
        # _check_same_thread=True: this object must stay on the creating thread.
        conn = libsql.connect(
            database=settings.turso_database_url,
            auth_token=settings.turso_auth_token,
            timeout=20.0,
            _check_same_thread=True,
        )
        conn.execute("SELECT 1")
        return ConnectionAdapter(conn, remote=True, close_on_exit=False)
    except DatabaseUnavailable:
        raise
    except TypeError:
        # Older libsql builds may not accept _check_same_thread.
        conn = libsql.connect(
            database=settings.turso_database_url,
            auth_token=settings.turso_auth_token,
            timeout=20.0,
        )
        conn.execute("SELECT 1")
        return ConnectionAdapter(conn, remote=True, close_on_exit=False)
    except Exception as exc:  # noqa: BLE001
        raise DatabaseUnavailable(
            "Could not reach the hosted database. Check TURSO_DATABASE_URL and TURSO_AUTH_TOKEN."
        ) from exc


def _checkout_turso() -> _ScopedCheckout:
    owner = getattr(_thread_state, "conn", None)
    if owner is not None and getattr(owner, "_conn", None) is not None:
        return _ScopedCheckout(owner)
    with _turso_lifecycle_lock:
        owner = _connect_turso_fresh()
    _thread_state.conn = owner
    return _ScopedCheckout(owner)


def _connect_local() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def connect():
    """Open a DB connection. Raises DatabaseUnavailable when remote is required but down.

    Turso: reuse this thread's client for the current request/job (never share
    it with another thread). Local SQLite: a new stdlib connection every call.
    """
    if _unavailable:
        raise DatabaseUnavailable(_unavailable_reason or "Database unavailable")

    if settings.require_remote_db and not settings.turso_configured:
        exc = DatabaseUnavailable(
            "Hosted database is required on this server. "
            "Set TURSO_DATABASE_URL and TURSO_AUTH_TOKEN."
        )
        mark_unavailable(exc)
        raise exc

    if settings.use_turso:
        try:
            checkout = _checkout_turso()
            if getattr(_thread_state, "scope_depth", 0) <= 0:
                # CLI / one-off: open, use, close in this `with` block only.
                return _UnscopedTurso(checkout._owner)
            return checkout
        except DatabaseUnavailable as exc:
            mark_unavailable(exc)
            raise

    return _connect_local()


class _UnscopedTurso:
    """Single db.py call outside a request/job: close the client when the `with` ends."""

    def __init__(self, owner: ConnectionAdapter) -> None:
        self._owner = owner

    def __getattr__(self, name):
        return getattr(self._owner, name)

    def __enter__(self):
        return self._owner

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self._owner.commit()
            else:
                self._owner.rollback()
        finally:
            release_thread_connection()
