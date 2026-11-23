"""Store methods owned by the maintenance area: what the maintainer reads of the day (the run log
per routine, its frequent errors, the newest answer of a routine, the issues and candidates that
moved, the cards a person decided) and the engineer's tasks (proposals of setting CODE, skill
engineer). Add queries here, never raw SQL elsewhere."""

from __future__ import annotations

from typing import Any

from tailwatch import util
from tailwatch.store.db import Database

TASK_SETTING = "CODE"
TASK_SKILL = "engineer"


def _loads(row: dict[str, Any], *keys: str, default: Any = None) -> dict[str, Any]:
    for key in keys:
        if key in row:
            row[key] = util.loads(row.get(key), {} if default is None else default)
    return row


class MaintenanceMixin:
    db: Database
    config: Any

    # -- the run log, summed for the maintainer ------------------------------------------------
    def runs_by_routine(self, *, since: str) -> list[dict[str, Any]]:
        """Per routine and end state: how many runs, model calls and dollars (model plus tools)."""
        return self.db.query(
            "SELECT routine, state, COUNT(*) AS n, COALESCE(SUM(model_calls), 0) AS model_calls, "
            "COALESCE(SUM(cost_usd + tool_usd), 0) AS usd, MIN(priced) AS priced FROM runs "
            "WHERE started_at >= ? GROUP BY routine, state ORDER BY routine, state", [since])

    def run_errors(self, *, since: str, limit: int = 20) -> list[dict[str, Any]]:
        """The most frequent error texts of runs that failed or were capped."""
        return self.db.query(
            "SELECT routine, stopped, error, COUNT(*) AS n FROM runs WHERE started_at >= ? AND state IN ('failed', 'capped') "
            "GROUP BY routine, stopped, error ORDER BY n DESC, routine LIMIT ?", [since, limit])

    def last_answer(self, routine: str, *, since: str) -> Any:
        """The newest finished answer of a routine (the ranker's order for the digest), parsed."""
        row = self.db.one("SELECT answer FROM runs WHERE routine = ? AND started_at >= ? AND state = 'done' AND answer != '' "
                          "ORDER BY id DESC LIMIT 1", [routine, since])
        return util.loads(row["answer"], row["answer"]) if row else None

    def spend_by_routine(self, *, since: str) -> dict[str, float]:
        rows = self.db.query("SELECT routine, COALESCE(SUM(cost_usd + tool_usd), 0) AS usd FROM runs WHERE started_at >= ? "
                             "GROUP BY routine ORDER BY routine", [since])
        return {r["routine"]: round(float(r["usd"] or 0), 4) for r in rows}

    # -- what moved since yesterday ------------------------------------------------------------
    def issues_since(self, *, since: str, limit: int = 500) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM issues WHERE updated_at >= ? ORDER BY id DESC LIMIT ?", [since, limit])
        return [_loads(r, "args", "result") for r in rows]

    def issues_opened(self, *, found_by: str, since: str) -> int:
        return int(self.db.scalar("SELECT COUNT(*) AS n FROM issues WHERE found_by = ? AND created_at >= ?",
                                  [found_by, since], 0) or 0)

    def candidates_since(self, *, since: str, limit: int = 300) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT id, feed, state, reason, person_id, updated_at FROM candidates WHERE updated_at >= ? "
                             "ORDER BY id DESC LIMIT ?", [since, limit])
        return rows

    def decided_reviews(self, *, since: str, limit: int = 100) -> list[dict[str, Any]]:
        """Cards with a decision (a person's or the desk's) since ``since``."""
        rows = self.db.query(
            "SELECT r.*, p.name AS person_name, v.signal AS signal, v.summary AS summary FROM card_reviews r "
            "JOIN people p ON p.id = r.person_id JOIN verdicts v ON v.id = r.verdict_id "
            "WHERE r.updated_at >= ? AND r.decision NOT IN ('', '{}') ORDER BY r.id DESC LIMIT ?", [since, limit])
        return [_loads(r, "answer", "floors", "decision") for r in rows]

    def review_answers(self, *, since: str, limit: int = 2000) -> list[dict[str, Any]]:
        """The editor's answers since ``since``: where the learned rules it used are recorded."""
        rows = self.db.query("SELECT id, answer FROM card_reviews WHERE updated_at >= ? ORDER BY id DESC LIMIT ?", [since, limit])
        return [_loads(r, "answer") for r in rows]

    def intel_due(self, *, at: str, limit: int = 20) -> list[dict[str, Any]]:
        """Active intel past its review date: the note asks whether it still holds."""
        return self.db.query("SELECT * FROM intel WHERE state = 'active' AND review_at <= ? ORDER BY review_at LIMIT ?",
                             [at, limit])

    # -- proposals: counts and the maintainer's own bookkeeping --------------------------------
    def proposals_by_since(self, *, by: str, since: str) -> int:
        return int(self.db.scalar("SELECT COUNT(*) AS n FROM proposals WHERE by_whom = ? AND created_at >= ?",
                                  [by, since], 0) or 0)

    def recent_proposals(self, *, since: str, limit: int = 50) -> list[dict[str, Any]]:
        """Proposals created or decided since ``since``, with their fate."""
        rows = self.db.query("SELECT * FROM proposals WHERE created_at >= ? OR decided_at >= ? ORDER BY id DESC LIMIT ?",
                             [since, since, limit])
        return [_loads(r, "value") | {"evidence": util.loads(r.get("evidence"), [])} for r in rows]

    def applied_since(self, *, since: str) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM proposals WHERE state = 'applied' AND decided_at >= ? ORDER BY id", [since])
        return [_loads(r, "value") | {"evidence": util.loads(r.get("evidence"), [])} for r in rows]

    def decide_proposal(self, proposal_id: int, state: str, *, by: str, note: str = "", expect: str = "",
                        **values: Any) -> bool:
        """Record a decision; with ``expect`` only when the proposal is still in that state."""
        changes: dict[str, Any] = {"state": state, "decided_by": by, "decided_at": util.iso(), **values}
        if note:
            changes["note"] = util.clip(note, 2000)
        if "value" in changes:
            changes["value"] = util.dumps(changes["value"])
        where: dict[str, Any] = {"id": proposal_id}
        if expect:
            where["state"] = expect
        return self.db.update("proposals", where, changes) > 0

    # -- the engineer's tasks ------------------------------------------------------------------
    def code_tasks(self, *, states: tuple[str, ...] = (), limit: int = 100) -> list[dict[str, Any]]:
        sql = "SELECT * FROM proposals WHERE setting = ? AND skill = ?"
        params: list[Any] = [TASK_SETTING, TASK_SKILL]
        if states:
            sql += f" AND state IN ({', '.join('?' for _ in states)})"
            params.extend(states)
        rows = self.db.query(sql + " ORDER BY id DESC LIMIT ?", [*params, limit])
        return [_loads(r, "value") | {"evidence": util.loads(r.get("evidence"), [])} for r in rows]

    def claim_task(self, task_id: int, *, from_states: tuple[str, ...] = ("proposed", "failed")) -> bool:
        """Move a task to running when it is still waiting; False when another run has it."""
        placeholders = ", ".join("?" for _ in from_states)
        cursor = self.db.execute(f"UPDATE proposals SET state = 'running' WHERE id = ? AND setting = ? AND skill = ? "
                                 f"AND state IN ({placeholders})", [task_id, TASK_SETTING, TASK_SKILL, *from_states])
        return int(cursor.rowcount or 0) > 0
