"""Settings (versioned), the run log, job receipts, locks, the response cache, notices and
thread memory: the bookkeeping every part of tailwatch shares."""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from tailwatch import util
from tailwatch.store.db import Database


class LockBusy(RuntimeError):
    pass


class CoreMixin:
    db: Database
    config: Any

    # -- settings -----------------------------------------------------------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.db.one("SELECT value FROM settings WHERE key = ?", [key])
        return default if row is None else util.loads(row["value"], default)

    def setting_row(self, key: str) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM settings WHERE key = ?", [key])
        if row:
            row["value"] = util.loads(row["value"])
        return row

    def set_setting(self, key: str, value: Any, *, by: str = "", note: str = "", history: bool = True) -> int:
        """Write a setting; every change of a versioned setting is kept in settings_history."""
        text = util.dumps(value)
        with self.db.transaction():
            row = self.db.one("SELECT version, value FROM settings WHERE key = ?", [key])
            if row and row["value"] == text:
                return int(row["version"])
            version = int(row["version"]) + 1 if row else 1
            if row:
                self.db.update("settings", {"key": key}, {"value": text, "version": version, "updated_by": by,
                                                          "updated_at": util.iso()})
            else:
                self.db.insert("settings", {"key": key, "value": text, "version": version, "updated_by": by,
                                            "updated_at": util.iso()}, returning="")
            if history:
                self.db.insert("settings_history", {"key": key, "value": text, "version": version, "by_whom": by,
                                                    "note": note, "at": util.iso()})
        return version

    def delete_setting(self, key: str) -> None:
        self.db.execute("DELETE FROM settings WHERE key = ?", [key])

    def settings_like(self, prefix: str) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM settings WHERE key LIKE ? ORDER BY key", [prefix + "%"])
        for row in rows:
            row["value"] = util.loads(row["value"])
        return rows

    def setting_history(self, key: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM settings_history WHERE key = ? ORDER BY version DESC LIMIT ?", [key, limit])
        for row in rows:
            row["value"] = util.loads(row["value"])
        return rows

    def revert_setting(self, key: str, version: int, *, by: str = "") -> int:
        """Put back the value the setting had before ``version``."""
        before = self.db.one("SELECT value FROM settings_history WHERE key = ? AND version < ? ORDER BY version DESC LIMIT 1",
                             [key, version])
        if before is None:
            raise ValueError(f"{key} has no value before version {version}")
        return self.set_setting(key, util.loads(before["value"]), by=by, note=f"revert of version {version}")

    # -- the run log --------------------------------------------------------------------------
    def open_run(self, routine: str, *, subject: str = "", key: str | None = None, model: str = "") -> int:
        return self.db.insert("runs", {"routine": routine, "subject": subject, "run_key": key, "model": model,
                                       "state": "running", "started_at": util.iso()})

    def run_by_key(self, key: str) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM runs WHERE run_key = ?", [key])
        if row:
            row["checkpoint"] = util.loads(row["checkpoint"], {})
        return row

    def _run_values(self, run: Any) -> dict[str, Any]:
        return {"model": getattr(run, "model", "") or "", "model_calls": run.model_calls, "tokens_in": run.tokens_in,
                "tokens_cached": run.tokens_cached, "tokens_out": run.tokens_out, "cost_usd": run.cost_usd,
                "priced": 1 if run.priced else 0, "stopped": run.stopped or ""}

    def checkpoint_run(self, run_id: int, run: Any) -> None:
        self.db.update("runs", {"id": run_id}, {**self._run_values(run), "checkpoint": util.dumps(run.to_json())})

    def finish_run(self, run_id: int, run: Any | None, *, state: str = "done", answer: Any = None, error: str = "",
                   tool_usd: float = 0.0, keep_checkpoint: bool = False) -> None:
        values: dict[str, Any] = {"state": state, "error": util.clip(error, 2000), "tool_usd": round(tool_usd, 6),
                                  "finished_at": util.iso()}
        if run is not None:
            values.update(self._run_values(run))
            values["trace"] = util.dumps(getattr(run, "trace", [])[-60:])
            if not keep_checkpoint:
                values["checkpoint"] = "{}"
        if answer is not None:
            values["answer"] = util.clip(answer if isinstance(answer, str) else util.dumps(answer), 40000)
        self.db.update("runs", {"id": run_id}, values)

    def log_run(self, routine: str, run: Any | None, *, subject: str = "", state: str = "done", answer: Any = None,
                error: str = "", tool_usd: float = 0.0) -> int:
        run_id = self.open_run(routine, subject=subject)
        self.finish_run(run_id, run, state=state, answer=answer, error=error, tool_usd=tool_usd)
        return run_id

    def runs(self, *, routine: str = "", limit: int = 20, since: str = "") -> list[dict[str, Any]]:
        sql = "SELECT id, routine, subject, state, model, model_calls, tokens_in, tokens_cached, tokens_out, cost_usd, " \
              "tool_usd, priced, stopped, error, started_at, finished_at FROM runs WHERE 1=1"
        params: list[Any] = []
        if routine:
            sql += " AND routine = ?"
            params.append(routine)
        if since:
            sql += " AND started_at >= ?"
            params.append(since)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return self.db.query(sql, params)

    def run(self, run_id: int) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM runs WHERE id = ?", [run_id])
        if row:
            row["trace"] = util.loads(row["trace"], [])
            row.pop("checkpoint", None)
        return row

    def spend_since(self, since: str, routines: tuple[str, ...] = ()) -> float:
        sql = "SELECT COALESCE(SUM(cost_usd + tool_usd), 0) AS usd FROM runs WHERE started_at >= ?"
        params: list[Any] = [since]
        if routines:
            sql += f" AND routine IN ({', '.join('?' for _ in routines)})"
            params.extend(routines)
        return float(self.db.scalar(sql, params, 0.0) or 0.0)

    def spend_today(self, routines: tuple[str, ...] = (), zone: str = "UTC") -> float:
        day = util.local_time(zone).replace(hour=0, minute=0, second=0, microsecond=0)
        return self.spend_since(util.iso(day), routines)

    # -- job receipts -------------------------------------------------------------------------
    def job(self, job: str, day: str) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM jobs WHERE job = ? AND day = ?", [job, day])
        if row:
            row["result"] = util.loads(row["result"], {})
        return row

    def job_start(self, job: str, day: str) -> int:
        """Record a start (a new attempt); returns the attempt number."""
        with self.db.transaction():
            row = self.job(job, day)
            if row is None:
                self.db.insert("jobs", {"job": job, "day": day, "state": "running", "attempts": 1, "started_at": util.iso()},
                               returning="")
                return 1
            attempts = int(row["attempts"]) + 1
            self.db.update("jobs", {"job": job, "day": day}, {"state": "running", "attempts": attempts,
                                                               "started_at": util.iso(), "error": ""})
            return attempts

    def job_finish(self, job: str, day: str, *, state: str, result: Any = None, error: str = "") -> None:
        self.db.update("jobs", {"job": job, "day": day}, {"state": state, "finished_at": util.iso(),
                                                           "result": util.dumps(result or {}), "error": util.clip(error, 2000)})

    def jobs_on(self, day: str) -> list[dict[str, Any]]:
        return self.db.query("SELECT * FROM jobs WHERE day = ? ORDER BY started_at", [day])

    # -- locks --------------------------------------------------------------------------------
    @contextmanager
    def lock(self, name: str, *, ttl_s: float = 1800, wait_s: float = 0) -> Iterator[None]:
        owner = uuid.uuid4().hex
        deadline = time.monotonic() + wait_s
        while True:
            if self._try_lock(name, owner, ttl_s):
                break
            if time.monotonic() >= deadline:
                raise LockBusy(f"{name} is held by another run")
            time.sleep(min(2.0, max(0.05, deadline - time.monotonic())))
        try:
            yield
        finally:
            self.db.execute("DELETE FROM locks WHERE name = ? AND owner = ?", [name, owner])

    def _try_lock(self, name: str, owner: str, ttl_s: float) -> bool:
        with self.db.transaction():
            row = self.db.one("SELECT owner, expires_at FROM locks WHERE name = ?", [name])
            if row and row["expires_at"] > util.iso():
                return False
            if row:
                self.db.update("locks", {"name": name}, {"owner": owner, "expires_at": util.ahead(seconds=ttl_s)})
            else:
                self.db.insert("locks", {"name": name, "owner": owner, "expires_at": util.ahead(seconds=ttl_s)}, returning="")
            return True

    # -- the response cache ------------------------------------------------------------------
    def cache_get(self, provider: str, key: str, *, max_age_hours: float | None = None) -> Any:
        row = self.db.one("SELECT response, fetched_at FROM source_cache WHERE provider = ? AND cache_key = ?", [provider, key])
        if row is None:
            return None
        if max_age_hours is not None and row["fetched_at"] < util.ago(hours=max_age_hours):
            return None
        return util.loads(row["response"])

    def cache_put(self, provider: str, key: str, response: Any) -> None:
        text = util.dumps(response)
        with self.db.transaction():
            if self.db.one("SELECT 1 AS x FROM source_cache WHERE provider = ? AND cache_key = ?", [provider, key]):
                self.db.update("source_cache", {"provider": provider, "cache_key": key}, {"response": text, "fetched_at": util.iso()})
            else:
                self.db.insert("source_cache", {"provider": provider, "cache_key": key, "response": text,
                                                "fetched_at": util.iso()}, returning="")

    # -- notices and threads ------------------------------------------------------------------
    def notice_once(self, topic: str, day: str) -> bool:
        """True the first time a topic is said on a day."""
        if self.db.one("SELECT 1 AS x FROM notices WHERE topic = ? AND day = ?", [topic, day]):
            return False
        self.db.insert("notices", {"topic": topic, "day": day, "at": util.iso()}, returning="")
        return True

    def thread_lines(self, key: str) -> list[dict[str, Any]]:
        row = self.db.one("SELECT lines FROM threads WHERE thread_key = ?", [key])
        return util.loads(row["lines"], []) if row else []

    def add_thread_line(self, key: str, line: Mapping[str, Any], keep: int = 30) -> None:
        lines = ([*self.thread_lines(key), dict(line)])[-keep:]
        with self.db.transaction():
            if self.db.one("SELECT 1 AS x FROM threads WHERE thread_key = ?", [key]):
                self.db.update("threads", {"thread_key": key}, {"lines": util.dumps(lines), "updated_at": util.iso()})
            else:
                self.db.insert("threads", {"thread_key": key, "lines": util.dumps(lines), "updated_at": util.iso()}, returning="")

    # -- read-only SQL for the desk ----------------------------------------------------------
    READ_ONLY_TABLES = ("people", "person_links", "sources", "observations", "events", "verdicts", "card_reviews",
                        "card_labels", "digests", "posts", "candidates", "intel", "issues", "proposals", "runs", "jobs",
                        "settings", "settings_history", "removals")

    def read_only_query(self, sql: str, limit: int = 50) -> list[dict[str, Any]]:
        """One SELECT, no writes, no other statement. A guard, not a sandbox: the local user owns
        the database; this keeps the model from changing it by mistake."""
        text = sql.strip().rstrip(";")
        lowered = text.lower()
        if not lowered.startswith(("select", "with")) or ";" in text:
            raise ValueError("only one SELECT statement is allowed")
        for word in ("insert", "update", "delete", "drop", "alter", "create", "attach", "pragma", "replace", "vacuum", "grant"):
            if f" {word} " in f" {lowered} " or lowered.startswith(word):
                raise ValueError(f"{word} is not allowed in a read-only query")
        return self.db.query(f"SELECT * FROM ({text}) AS q LIMIT {int(limit)}")
