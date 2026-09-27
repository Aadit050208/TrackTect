"""SQLite / Turso connection factory.

Local development: stdlib sqlite3 against data/tracktect.db.
Production (Turso env vars set): libsql over HTTP. Failures never fall back
to a local file — that file is wiped on Render restarts.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping
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
    def __init__(self, conn, *, remote: bool = False) -> None:
        self._conn = conn
        self.remote = remote

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
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self.commit()
            else:
                self.rollback()
        finally:
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


def _connect_turso():
    try:
        import libsql
    except ImportError as exc:
        raise DatabaseUnavailable(
            "The libsql package is not installed. Run: pip install libsql"
        ) from exc
    try:
        conn = libsql.connect(
            database=settings.turso_database_url,
            auth_token=settings.turso_auth_token,
            timeout=20.0,
        )
        # Cheap ping so a bad token fails now, not on the first user request.
        conn.execute("SELECT 1")
        return ConnectionAdapter(conn, remote=True)
    except DatabaseUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DatabaseUnavailable(
            "Could not reach the hosted database. Check TURSO_DATABASE_URL and TURSO_AUTH_TOKEN."
        ) from exc


def _connect_local() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def connect():
    """Open a DB connection. Raises DatabaseUnavailable when remote is required but down."""
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
            return _connect_turso()
        except DatabaseUnavailable as exc:
            mark_unavailable(exc)
            raise

    return _connect_local()
