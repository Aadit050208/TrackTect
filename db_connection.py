"""SQLite / Turso connection factory.

Local development: stdlib sqlite3 against data/tracktect.db (unchanged).

Production (TURSO_* set): SQL over HTTP (Hrana `/v2/pipeline`) via `requests`.
No native libsql / Tokio client. Each statement is its own HTTP POST — nothing
is shared across threads except immutable settings (URL + token).
"""

from __future__ import annotations

import base64
import logging
import sqlite3
from collections.abc import Mapping
from contextlib import contextmanager
from typing import Any, Iterable, List, Optional, Sequence
from urllib.parse import urlparse, urlunparse

import requests

from config import settings

logger = logging.getLogger(__name__)

# Turso /v2/pipeline rejects oversized request arrays (HTTP 400).
PIPELINE_CHUNK_SIZE = 20


class DatabaseUnavailable(Exception):
    """Remote DB is required but cannot be reached. Do not use a local file."""


class Row(Mapping):
    """Dict-like row so existing `row['col']` / `dict(row)` code keeps working."""

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


# Kept as no-ops so Flask / scheduler wrappers stay harmless.
# HTTP calls are already independent — no client to scope or close.
def enter_db_scope() -> None:
    return None


def exit_db_scope() -> None:
    return None


def release_thread_connection() -> None:
    return None


@contextmanager
def thread_db_scope():
    yield


def _pipeline_url(raw_url: str) -> str:
    """libsql://host → https://host/v2/pipeline (Turso SQL-over-HTTP)."""
    url = (raw_url or "").strip()
    if not url:
        raise DatabaseUnavailable("TURSO_DATABASE_URL is empty")
    if url.startswith("libsql://"):
        url = "https://" + url[len("libsql://") :]
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise DatabaseUnavailable("TURSO_DATABASE_URL must be a libsql:// or https:// host")
    path = parsed.path.rstrip("/")
    if path.endswith("/v2/pipeline") or path.endswith("/v3/pipeline"):
        return urlunparse(parsed._replace(path=path, query="", fragment=""))
    return urlunparse(parsed._replace(path="/v2/pipeline", query="", fragment=""))


def _hrana_arg(value: Any) -> dict:
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": "1" if value else "0"}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    if isinstance(value, (bytes, bytearray)):
        return {"type": "blob", "base64": base64.b64encode(bytes(value)).decode("ascii")}
    return {"type": "text", "value": str(value)}


def _decode_hrana_value(cell: Any) -> Any:
    if cell is None:
        return None
    if not isinstance(cell, dict):
        return cell
    kind = (cell.get("type") or "").lower()
    if kind == "null":
        return None
    if kind == "integer":
        raw = cell.get("value")
        try:
            return int(raw)
        except (TypeError, ValueError):
            return raw
    if kind == "float":
        raw = cell.get("value")
        try:
            return float(raw)
        except (TypeError, ValueError):
            return raw
    if kind == "blob":
        blob = cell.get("base64") or cell.get("value") or ""
        try:
            return base64.b64decode(blob)
        except (TypeError, ValueError):
            return blob
    return cell.get("value")


def _raise_sql_error(message: str) -> None:
    text = (message or "").lower()
    if "constraint" in text or "unique" in text:
        raise sqlite3.IntegrityError(message)
    raise sqlite3.Error(message)


class HttpCursor:
    def __init__(self, columns: Sequence[str], rows: Sequence[Sequence[Any]], lastrowid=None, rowcount: int = -1):
        self.description = [(name, None, None, None, None, None, None) for name in columns]
        self._rows = [Row(columns, row) for row in rows]
        self._i = 0
        self.lastrowid = lastrowid
        self.rowcount = rowcount

    def fetchone(self):
        if self._i >= len(self._rows):
            return None
        row = self._rows[self._i]
        self._i += 1
        return row

    def fetchall(self):
        rest = self._rows[self._i :]
        self._i = len(self._rows)
        return rest

    def fetchmany(self, size=None):
        if size is None:
            size = 1
        chunk = self._rows[self._i : self._i + size]
        self._i += len(chunk)
        return chunk

    def close(self) -> None:
        return None

    def __iter__(self):
        return self

    def __next__(self):
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row


class TursoHttpConnection:
    """Drop-in for sqlite3.Connection: execute / executemany / executescript.

    Every call is an independent POST to /v2/pipeline (execute + close).
    No persistent stream, no shared client, no native runtime.
    """

    def __init__(self, database_url: str, auth_token: str) -> None:
        self._url = _pipeline_url(database_url)
        self._token = auth_token
        self.lastrowid = None

    def _post_pipeline(self, requests_body: List[dict]) -> dict:
        try:
            response = requests.post(
                self._url,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                },
                json={"requests": requests_body},
                timeout=25,
            )
        except requests.exceptions.RequestException as exc:
            raise DatabaseUnavailable(
                "Could not reach the hosted database over HTTP."
            ) from exc
        if response.status_code >= 400:
            try:
                detail = response.json()
            except ValueError:
                detail = (response.text or "")[:500]
            if response.status_code in (401, 403):
                raise DatabaseUnavailable(
                    f"Hosted database rejected the auth token (HTTP {response.status_code}): {detail}"
                )
            raise DatabaseUnavailable(
                f"Hosted database HTTP {response.status_code}: {detail}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise DatabaseUnavailable("Hosted database returned invalid JSON") from exc

    def _execute_pipeline(self, stmts: List[dict]) -> List[dict]:
        payload = [{"type": "execute", "stmt": stmt} for stmt in stmts]
        payload.append({"type": "close"})
        data = self._post_pipeline(payload)
        results = data.get("results") or []
        out = []
        for item in results:
            if item.get("type") == "error":
                err = item.get("error") or {}
                _raise_sql_error(err.get("message") or str(err) or "SQL error")
            resp = item.get("response") or {}
            if resp.get("type") == "execute":
                result = resp.get("result") or {}
                if result.get("error"):
                    _raise_sql_error(str(result["error"]))
                out.append(result)
        return out

    def execute(self, sql: str, params: Sequence[Any] = ()):
        if sql.lstrip().upper().startswith("PRAGMA JOURNAL_MODE"):
            return HttpCursor([], [])
        stmt: dict = {"sql": sql}
        if params:
            stmt["args"] = [_hrana_arg(p) for p in params]
        results = self._execute_pipeline([stmt])
        return self._cursor_from_result(results[-1] if results else {})

    def executemany(self, sql: str, seq_of_params: Iterable[Sequence[Any]]):
        stmts = [{"sql": sql, "args": [_hrana_arg(p) for p in row]} for row in seq_of_params]
        if not stmts:
            return HttpCursor([], [])
        last = HttpCursor([], [])
        for i in range(0, len(stmts), PIPELINE_CHUNK_SIZE):
            chunk = stmts[i : i + PIPELINE_CHUNK_SIZE]
            results = self._execute_pipeline(chunk)
            if results:
                last = self._cursor_from_result(results[-1])
        return last

    def executescript(self, script: str):
        statements = _split_sql(script)
        last = HttpCursor([], [])
        for i in range(0, len(statements), PIPELINE_CHUNK_SIZE):
            chunk = [{"sql": s} for s in statements[i : i + PIPELINE_CHUNK_SIZE]]
            results = self._execute_pipeline(chunk)
            if results:
                last = self._cursor_from_result(results[-1])
        return last

    def _cursor_from_result(self, result: dict) -> HttpCursor:
        cols = [c.get("name") or "" for c in (result.get("cols") or [])]
        rows = []
        for raw_row in result.get("rows") or []:
            rows.append([_decode_hrana_value(cell) for cell in raw_row])
        last_id = result.get("last_insert_rowid")
        if last_id is not None and last_id != "":
            try:
                last_id = int(last_id)
            except (TypeError, ValueError):
                pass
        else:
            last_id = None
        self.lastrowid = last_id
        affected = result.get("affected_row_count")
        try:
            rowcount = int(affected) if affected is not None else -1
        except (TypeError, ValueError):
            rowcount = -1
        return HttpCursor(cols, rows, lastrowid=last_id, rowcount=rowcount)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


def _connect_local() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _connect_turso() -> TursoHttpConnection:
    if not settings.turso_auth_token:
        raise DatabaseUnavailable("TURSO_AUTH_TOKEN is empty")
    return TursoHttpConnection(settings.turso_database_url, settings.turso_auth_token)


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
