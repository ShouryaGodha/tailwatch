"""One database adapter for SQLite (the default) and Postgres (``pip install tailwatch[postgres]``).

SQL is written once with ``?`` placeholders; the Postgres connection turns them into ``%s``.
Rows come back as dicts. ``transaction()`` groups writes; outside one, every statement commits.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from importlib import resources
from typing import Any

from tailwatch import util

_POSTGRES_TYPES = [(re.compile(r"INTEGER PRIMARY KEY AUTOINCREMENT"), "BIGSERIAL PRIMARY KEY"),
                   (re.compile(r"\bREAL\b"), "DOUBLE PRECISION")]


class Database:
    def __init__(self, url: str) -> None:
        self.url = url
        self.kind = "sqlite" if url.startswith("sqlite") else "postgres"
        self._local = threading.local()
        self._lock = threading.RLock()
        if self.kind == "sqlite":
            path = url.removeprefix("sqlite:///") or ":memory:"
            self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL" if path != ":memory:" else "PRAGMA journal_mode=MEMORY")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=10000")
        else:
            try:
                import psycopg
                from psycopg.rows import dict_row
            except ImportError as error:  # pragma: no cover - optional extra
                raise RuntimeError("Postgres needs the extra: pip install 'tailwatch[postgres]'") from error
            self._conn = psycopg.connect(url, autocommit=True, row_factory=dict_row)
        self._depth = 0

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.kind == "postgres" else sql

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(self._sql(sql), tuple(params))
            return cursor

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            cursor = self.execute(sql, params)
            rows = cursor.fetchall() if cursor.description else []
            return [dict(r) for r in rows]

    def one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = self.one(sql, params)
        return default if row is None else next(iter(row.values()), default)

    def insert(self, table: str, row: dict[str, Any], *, returning: str = "id") -> Any:
        columns = list(row)
        sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})"
        if returning:
            sql += f" RETURNING {returning}"
        found = self.one(sql, [row[c] for c in columns]) if returning else self.execute(sql, [row[c] for c in columns])
        return found[returning] if returning and found else None

    def update(self, table: str, where: dict[str, Any], values: dict[str, Any]) -> int:
        sets = ", ".join(f"{k} = ?" for k in values)
        conds = " AND ".join(f"{k} = ?" for k in where)
        cursor = self.execute(f"UPDATE {table} SET {sets} WHERE {conds}", [*values.values(), *where.values()])
        return int(cursor.rowcount or 0)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            outer = self._depth == 0
            if outer:
                self._conn.execute("BEGIN")
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                if outer:
                    self._conn.execute("ROLLBACK")
                raise
            self._depth -= 1
            if outer:
                self._conn.execute("COMMIT")

    def migrate(self) -> list[str]:
        """Apply every bundled migration not applied yet. Returns the versions applied."""
        with self._lock:
            exists = self._table_exists("schema_migrations")
            done = {r["version"] for r in self.query("SELECT version FROM schema_migrations")} if exists else set()
            applied: list[str] = []
            files = sorted(p for p in resources.files("tailwatch.store.migrations").iterdir() if p.name.endswith(".sql"))
            for path in files:
                version = path.name.split("_", 1)[0]
                if version in done:
                    continue
                script = path.read_text(encoding="utf-8")
                if self.kind == "postgres":
                    for pattern, replacement in _POSTGRES_TYPES:
                        script = pattern.sub(replacement, script)
                with self.transaction():
                    for statement in _statements(script):
                        self._conn.execute(statement)
                    self.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)", [version, util.iso()])
                applied.append(version)
            return applied

    def _table_exists(self, name: str) -> bool:
        if self.kind == "sqlite":
            return bool(self.one("SELECT name FROM sqlite_master WHERE type='table' AND name=?", [name]))
        return bool(self.scalar("SELECT to_regclass(?) IS NOT NULL AS t", [name], False))

    def close(self) -> None:
        self._conn.close()


def _statements(script: str) -> list[str]:
    lines = [line for line in script.splitlines() if not line.strip().startswith("--")]
    cleaned = "\n".join(re.sub(r"--.*$", "", line) for line in lines)
    return [s.strip() for s in cleaned.split(";") if s.strip()]
