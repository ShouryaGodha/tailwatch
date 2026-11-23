"""Store methods owned by the enrich area. Add queries here, never raw SQL elsewhere.

The candidate rows themselves (add, read, update) are the desk's (``store.desk``); these are the
queries the candidate pipeline and the link enrichment need on top."""

from __future__ import annotations

from typing import Any

from tailwatch import util
from tailwatch.store.db import Database

WORK_STATES = ("new", "identified", "enriching")
STUCK_HOURS = 2  # an "enriching" row this old was left by a crashed run


class EnrichMixin:
    db: Database
    config: Any

    def candidates_to_work(self, *, limit: int = 20, max_attempts: int = 3) -> list[dict[str, Any]]:
        """Candidates waiting for the pipeline, oldest first: new, identified, and enriching rows
        a crashed run left behind."""
        from tailwatch.store.desk import _candidate

        rows = self.db.query(
            "SELECT * FROM candidates WHERE attempts < ? AND (state IN ('new', 'identified') "
            "OR (state = 'enriching' AND updated_at < ?)) ORDER BY id LIMIT ?",
            [max_attempts, util.ago(hours=STUCK_HOURS), limit])
        return [_candidate(r) for r in rows]  # type: ignore[misc]

    def candidate_failed(self, candidate_id: int, error: str, *, state: str) -> int:
        """Count a failed attempt and put the row back in ``state``. Returns the attempts."""
        with self.db.transaction():
            row = self.db.one("SELECT attempts FROM candidates WHERE id = ?", [candidate_id])
            attempts = int(row["attempts"]) + 1 if row else 1
            self.db.update("candidates", {"id": candidate_id}, {"attempts": attempts, "state": state,
                                                                "reason": util.clip(error, 500), "updated_at": util.iso()})
        return attempts

    def recent_candidates(self, *, since: str, states: tuple[str, ...] = (), limit: int = 500) -> list[dict[str, Any]]:
        from tailwatch.store.desk import _candidate

        sql, params = "SELECT * FROM candidates WHERE created_at >= ?", [since]
        if states:
            sql += f" AND state IN ({', '.join('?' for _ in states)})"
            params.extend(states)
        rows = self.db.query(sql + " ORDER BY id DESC LIMIT ?", [*params, limit])
        return [_candidate(r) for r in rows]  # type: ignore[misc]

    def candidate_counts(self, *, since: str = "") -> dict[str, int]:
        sql, params = "SELECT state, COUNT(*) AS n FROM candidates", []
        if since:
            sql += " WHERE updated_at >= ?"
            params.append(since)
        return {r["state"]: int(r["n"]) for r in self.db.query(sql + " GROUP BY state", params)}

    def newcomers_to_enrich(self, *, since: str, limit: int) -> list[dict[str, Any]]:
        """Tracked people added since ``since`` whose links were never looked at."""
        from tailwatch.store.people import _person

        rows = self.db.query("SELECT * FROM people WHERE status = 'tracked' AND links_enriched_at IS NULL "
                             "AND created_at >= ? ORDER BY created_at, id LIMIT ?", [since, limit])
        return [_person(r) for r in rows]  # type: ignore[misc]

    def thin_people(self, *, limit: int, exclude: tuple[int, ...] = ()) -> list[dict[str, Any]]:
        """Tracked people with exactly one link who were never looked at, oldest first."""
        from tailwatch.store.people import _person

        sql = ("SELECT p.* FROM people p WHERE p.status = 'tracked' AND p.links_enriched_at IS NULL "
               "AND (SELECT COUNT(*) FROM person_links l WHERE l.person_id = p.id) = 1")
        params: list[Any] = []
        if exclude:
            sql += f" AND p.id NOT IN ({', '.join('?' for _ in exclude)})"
            params.extend(exclude)
        rows = self.db.query(sql + " ORDER BY p.created_at, p.id LIMIT ?", [*params, limit])
        return [_person(r) for r in rows]  # type: ignore[misc]

    def mark_links_enriched(self, person_id: int) -> None:
        self.db.update("people", {"id": person_id}, {"links_enriched_at": util.iso(), "updated_at": util.iso()})
