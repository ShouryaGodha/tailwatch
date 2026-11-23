"""Checks: observations (one per read of a source), events (one per difference) and verdicts
(one judgement per check, append-only)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from tailwatch import util
from tailwatch.store.db import Database

CURRENT = "SELECT observation_id, MAX(id) AS id FROM verdicts GROUP BY observation_id"


def _verdict(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    row["now"] = util.loads(row.get("now"), {})
    row["flags"] = util.loads(row.get("flags"), [])
    return row


def _observation(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    row["content"] = util.loads(row.get("content"), {})
    row["person_snapshot"] = util.loads(row.get("person_snapshot"), {})
    return row


class ChecksMixin:
    db: Database
    config: Any

    # -- what is due --------------------------------------------------------------------------
    def due_sources(self, kind: str, *, before: str, limit: int) -> list[dict[str, Any]]:
        """Active sources of tracked people of one kind, due before ``before``, never-checked first."""
        return self.db.query(
            "SELECT s.* FROM sources s JOIN people p ON p.id = s.person_id "
            "WHERE s.kind = ? AND s.status = 'active' AND p.status = 'tracked' AND (s.next_due_at IS NULL OR s.next_due_at <= ?) "
            "ORDER BY CASE WHEN s.last_checked_at IS NULL THEN 0 ELSE 1 END, s.next_due_at, s.id LIMIT ?",
            [kind, before, limit])

    def count_active(self, kind: str) -> int:
        return int(self.db.scalar("SELECT COUNT(*) AS n FROM sources s JOIN people p ON p.id = s.person_id "
                                  "WHERE s.kind = ? AND s.status = 'active' AND p.status = 'tracked'", [kind], 0) or 0)

    # -- observations and events --------------------------------------------------------------
    def observation(self, observation_id: int) -> dict[str, Any] | None:
        return _observation(self.db.one("SELECT * FROM observations WHERE id = ?", [observation_id]))

    def last_ok_observation(self, source_id: int, *, before_id: int | None = None) -> dict[str, Any] | None:
        sql = "SELECT * FROM observations WHERE source_id = ? AND fetch_status = 'ok'"
        params: list[Any] = [source_id]
        if before_id is not None:
            sql += " AND id < ?"
            params.append(before_id)
        return _observation(self.db.one(sql + " ORDER BY id DESC LIMIT 1", params))

    def record_check(self, source: Mapping[str, Any], *, status: str, content: Mapping[str, Any] | None = None,
                     content_hash: str = "", error: str = "", credits: float = 0.0, first_read: bool = False,
                     changes: Iterable[Mapping[str, Any]] = (), previous_id: int | None = None,
                     person_snapshot: Mapping[str, Any] | None = None, source_update: Mapping[str, Any] | None = None) -> int:
        """Write one check: the observation, its events (``changes``: label, before, after,
        content_date) and the source's bookkeeping, in one transaction. A check with events waits
        for the judge (judge_state pending)."""
        changes = list(changes)
        now = util.iso()
        with self.db.transaction():
            observation_id = self.db.insert("observations", {
                "source_id": source["id"], "person_id": source["person_id"], "observed_at": now, "fetch_status": status,
                "content_hash": content_hash, "content": util.dumps(dict(content or {})),
                "person_snapshot": util.dumps(dict(person_snapshot or {})), "error": util.clip(error, 1000),
                "credits": credits, "first_read": 1 if first_read else 0,
                "judge_state": "pending" if changes else "none"})
            for change in changes:
                self.db.insert("events", {
                    "person_id": source["person_id"], "source_id": source["id"], "observation_id": observation_id,
                    "previous_observation_id": previous_id, "label": str(change.get("label") or "changed"),
                    "before_text": util.clip(change.get("before"), 4000), "after_text": util.clip(change.get("after"), 4000),
                    "content_date": str(change.get("content_date") or ""), "observed_at": now})
            values = {"last_checked_at": now, **dict(source_update or {})}
            if status == "ok":
                values.setdefault("last_observation_id", observation_id)
            self.db.update("sources", {"id": source["id"]}, values)
        return int(observation_id)

    def events_of(self, observation_id: int) -> list[dict[str, Any]]:
        return self.db.query("SELECT * FROM events WHERE observation_id = ? ORDER BY id", [observation_id])

    def latest_reads(self, person_id: int) -> list[dict[str, Any]]:
        """The newest successful read of each of the person's sources, with the source's kind."""
        rows = self.db.query(
            "SELECT o.*, s.kind AS kind, s.url AS url FROM observations o JOIN sources s ON s.id = o.source_id "
            "JOIN (SELECT source_id, MAX(id) AS id FROM observations WHERE person_id = ? AND fetch_status = 'ok' "
            "GROUP BY source_id) newest ON newest.id = o.id ORDER BY s.kind", [person_id])
        return [_observation(r) for r in rows]  # type: ignore[misc]

    def recent_events(self, person_id: int, *, since: str, exclude_observation: int | None = None) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT e.*, s.kind AS kind, v.signal AS signal, v.summary AS summary FROM events e "
            "JOIN sources s ON s.id = e.source_id LEFT JOIN (" + CURRENT + ") cur ON cur.observation_id = e.observation_id "
            "LEFT JOIN verdicts v ON v.id = cur.id WHERE e.person_id = ? AND e.observed_at >= ? ORDER BY e.id DESC LIMIT 40",
            [person_id, since])
        return [r for r in rows if r["observation_id"] != exclude_observation]

    # -- the judge's queue --------------------------------------------------------------------
    def pending_checks(self, limit: int) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT o.*, s.kind AS kind, s.url AS url FROM observations o JOIN sources s ON s.id = o.source_id "
            "JOIN people p ON p.id = o.person_id WHERE o.judge_state = 'pending' AND p.status != 'ignored' "
            "ORDER BY o.observed_at, o.id LIMIT ?", [limit])
        out = []
        for row in rows:
            row = _observation(row)
            row["events"] = self.events_of(row["id"])  # type: ignore[index]
            out.append(row)
        return out

    def count_pending(self, *, older_than: str | None = None) -> int:
        sql = "SELECT COUNT(*) AS n FROM observations WHERE judge_state = 'pending'"
        params: list[Any] = []
        if older_than:
            sql += " AND observed_at <= ?"
            params.append(older_than)
        return int(self.db.scalar(sql, params, 0) or 0)

    def count_parked(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) AS n FROM observations WHERE judge_state = 'parked'", [], 0) or 0)

    def judge_failed(self, observation_id: int, error: str, *, park_after: int) -> bool:
        """Count a failed attempt; returns True when the check is now parked."""
        row = self.db.one("SELECT judge_attempts FROM observations WHERE id = ?", [observation_id])
        attempts = int(row["judge_attempts"] if row else 0) + 1
        parked = attempts >= park_after
        self.db.update("observations", {"id": observation_id}, {"judge_attempts": attempts, "judge_error": util.clip(error, 500),
                                                                "judge_state": "parked" if parked else "pending"})
        return parked

    def unpark(self, observation_id: int) -> None:
        self.db.update("observations", {"id": observation_id}, {"judge_state": "pending", "judge_attempts": 0, "judge_error": ""})

    def save_verdict(self, observation_id: int, person_id: int, verdict: Mapping[str, Any], *, judged_by: str = "model",
                     model: str = "", prompt_version: str = "", guidance_hash: str = "", by_whom: str = "") -> int:
        now = verdict.get("now") or {}
        with self.db.transaction():
            verdict_id = self.db.insert("verdicts", {
                "observation_id": observation_id, "person_id": person_id, "signal": str(verdict.get("signal") or "none"),
                "confidence": float(verdict.get("confidence") or 0), "summary": util.clip(verdict.get("summary"), 400),
                "context": util.clip(verdict.get("context"), 400), "reason": util.clip(verdict.get("reason"), 2000),
                "now": util.dumps(now), "category": str(verdict.get("category") or ""),
                "priority": int(verdict.get("priority") or 0), "eligible": 0 if verdict.get("eligible") is False else 1,
                "flags": util.dumps(list(verdict.get("flags") or [])), "same_as": int(verdict.get("same_as") or 0),
                "supersedes": int(verdict.get("supersedes") or 0), "signal_date": str(verdict.get("signal_date") or ""),
                "wrong_person": 1 if verdict.get("wrong_person") else 0, "judged_by": judged_by, "model": model,
                "prompt_version": prompt_version, "guidance_hash": guidance_hash, "by_whom": by_whom, "created_at": util.iso()})
            self.db.update("observations", {"id": observation_id}, {"judge_state": "judged"})
        return int(verdict_id)

    def verdict(self, verdict_id: int) -> dict[str, Any] | None:
        return _verdict(self.db.one("SELECT * FROM verdicts WHERE id = ?", [verdict_id]))

    def current_verdict(self, observation_id: int) -> dict[str, Any] | None:
        return _verdict(self.db.one("SELECT * FROM verdicts WHERE observation_id = ? ORDER BY id DESC LIMIT 1", [observation_id]))

    def is_current(self, verdict_id: int) -> bool:
        row = self.db.one("SELECT observation_id FROM verdicts WHERE id = ?", [verdict_id])
        if row is None:
            return False
        newest = self.db.scalar("SELECT MAX(id) AS id FROM verdicts WHERE observation_id = ?", [row["observation_id"]])
        return int(newest or 0) == verdict_id

    def prior_alerts(self, person_id: int, *, since: str) -> list[dict[str, Any]]:
        """The person's earlier signals (current verdicts that are not none), newest first, with
        the source kind and whether a digest carried them: the judge's authority on duplicates."""
        rows = self.db.query(
            "SELECT v.*, s.kind AS kind, (SELECT COUNT(*) FROM posts p WHERE p.verdict_id = v.id) AS posted "
            "FROM verdicts v JOIN (" + CURRENT + ") cur ON cur.id = v.id JOIN observations o ON o.id = v.observation_id "
            "JOIN sources s ON s.id = o.source_id WHERE v.person_id = ? AND v.signal != 'none' AND v.created_at >= ? "
            "ORDER BY v.id DESC LIMIT 20", [person_id, since])
        return [_verdict(r) for r in rows]  # type: ignore[misc]

    def verdicts_of(self, person_id: int, *, limit: int = 50, signals_only: bool = False) -> list[dict[str, Any]]:
        sql = ("SELECT v.*, s.kind AS kind, o.observed_at AS observed_at FROM verdicts v JOIN observations o ON o.id = v.observation_id "
               "JOIN sources s ON s.id = o.source_id WHERE v.person_id = ?")
        if signals_only:
            sql += " AND v.signal != 'none'"
        rows = self.db.query(sql + " ORDER BY v.id DESC LIMIT ?", [person_id, limit])
        return [_verdict(r) for r in rows]  # type: ignore[misc]

    def judge_samples(self, *, since: str, limit: int = 15) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT v.*, p.name AS person_name, p.organization AS organization FROM verdicts v JOIN people p ON p.id = v.person_id "
            "WHERE v.judged_by = 'model' AND v.created_at >= ? ORDER BY CASE WHEN v.signal != 'none' THEN 0 ELSE 1 END, v.id DESC LIMIT ?",
            [since, limit])
        for row in rows:
            _verdict(row)
            row["events"] = [{"label": e["label"], "before": util.clip(e["before_text"], 300), "after": util.clip(e["after_text"], 300)}
                             for e in self.events_of(row["observation_id"])]
        return rows

    def watch_stats(self, *, since: str) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT s.kind AS kind, o.fetch_status AS status, COUNT(*) AS n, MAX(o.error) AS sample_error FROM observations o "
            "JOIN sources s ON s.id = o.source_id WHERE o.observed_at >= ? GROUP BY s.kind, o.fetch_status ORDER BY s.kind", [since])
