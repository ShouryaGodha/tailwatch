"""The desk's tables: candidates (reports about people not on the list), intel, issues and
proposals (engineer tasks included)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tailwatch import util
from tailwatch.store.db import Database

ISSUE_COOLDOWN_DAYS = 30
ISSUE_FLOOD = 200
PROPOSAL_DAYS = 7


def _issue(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row:
        row["args"] = util.loads(row.get("args"), {})
        row["result"] = util.loads(row.get("result"), {})
    return row


def _proposal(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row:
        row["value"] = util.loads(row.get("value"), {})
        row["evidence"] = util.loads(row.get("evidence"), [])
    return row


def _candidate(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row:
        row["subject"] = util.loads(row.get("subject"), {})
        row["enrichment"] = util.loads(row.get("enrichment"), {})
    return row


class DeskMixin:
    db: Database
    config: Any

    # -- candidates ---------------------------------------------------------------------------
    def add_candidate(self, feed: str, item_id: str, *, url: str = "", text: str = "", subject: Mapping[str, Any] | None = None,
                      state: str = "new") -> tuple[int, bool]:
        found = self.db.one("SELECT id FROM candidates WHERE feed = ? AND item_id = ?", [feed, item_id])
        if found:
            return int(found["id"]), False
        now = util.iso()
        return int(self.db.insert("candidates", {"feed": feed, "item_id": item_id, "url": url, "text": util.clip(text, 8000),
                                                 "subject": util.dumps(dict(subject or {})), "state": state,
                                                 "created_at": now, "updated_at": now})), True

    def candidate(self, candidate_id: int) -> dict[str, Any] | None:
        return _candidate(self.db.one("SELECT * FROM candidates WHERE id = ?", [candidate_id]))

    def candidates(self, *, states: tuple[str, ...] = (), limit: int = 50) -> list[dict[str, Any]]:
        sql = "SELECT * FROM candidates"
        params: list[Any] = []
        if states:
            sql += f" WHERE state IN ({', '.join('?' for _ in states)})"
            params.extend(states)
        rows = self.db.query(sql + " ORDER BY id DESC LIMIT ?", [*params, limit])
        return [_candidate(r) for r in rows]  # type: ignore[misc]

    def update_candidate(self, candidate_id: int, **values: Any) -> None:
        for key in ("subject", "enrichment"):
            if key in values:
                values[key] = util.dumps(values[key])
        values["updated_at"] = util.iso()
        self.db.update("candidates", {"id": candidate_id}, values)

    # -- intel --------------------------------------------------------------------------------
    def add_intel(self, text: str, *, scope_kind: str = "everyone", scope_value: str = "", said_by: str = "",
                  heard_where: str = "", review_days: int = 30) -> int:
        if scope_kind not in ("everyone", "organization", "person"):
            raise ValueError("scope is everyone, organization or person")
        return int(self.db.insert("intel", {"text": util.clip(text, 1000), "scope_kind": scope_kind, "scope_value": scope_value,
                                            "said_by": said_by, "heard_where": heard_where, "state": "active",
                                            "review_at": util.ahead(days=review_days), "created_at": util.iso()}))

    def intel(self, *, state: str = "active", limit: int = 100) -> list[dict[str, Any]]:
        return self.db.query("SELECT * FROM intel WHERE state = ? ORDER BY id DESC LIMIT ?", [state, limit])

    def intel_for(self, person: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Entries that speak about this person: by id, by organization words, or everyone."""
        org = str(person.get("organization") or "").lower()
        out = []
        for entry in self.intel():
            kind, value = entry["scope_kind"], str(entry["scope_value"] or "").lower()
            if kind == "everyone" or (kind == "person" and value == str(person.get("id"))) or \
                    (kind == "organization" and value and value in org):
                out.append(entry)
        return out

    def set_intel(self, intel_id: int, **values: Any) -> None:
        self.db.update("intel", {"id": intel_id}, values)

    # -- issues -------------------------------------------------------------------------------
    def open_issue(self, kind: str, subject_key: str, *, playbook: str = "", args: Mapping[str, Any] | None = None,
                   found_by: str = "", note: str = "", review_id: int | None = None) -> tuple[int, bool]:
        """One issue per (kind, subject). A fixed or failed one reopens only after the cooldown."""
        with self.db.transaction():
            row = _issue(self.db.one("SELECT * FROM issues WHERE kind = ? AND subject_key = ?", [kind, subject_key]))
            if row:
                if row["state"] in ("open", "running"):
                    return int(row["id"]), False
                if (row["closed_at"] or "") > util.ago(days=ISSUE_COOLDOWN_DAYS):
                    return int(row["id"]), False
                self.db.update("issues", {"id": row["id"]}, {"state": "open", "attempts": 0, "playbook": playbook,
                                                             "args": util.dumps(dict(args or {})), "note": util.clip(note, 1000),
                                                             "found_by": found_by, "updated_at": util.iso(), "closed_at": None})
                return int(row["id"]), True
            opened = int(self.db.scalar("SELECT COUNT(*) AS n FROM issues WHERE state = 'open'", [], 0) or 0)
            if opened >= ISSUE_FLOOD:
                raise RuntimeError(f"{opened} issues are open already; refusing more (a flood is a bug, not data)")
            now = util.iso()
            issue_id = self.db.insert("issues", {"kind": kind, "subject_key": subject_key, "playbook": playbook,
                                                 "args": util.dumps(dict(args or {})), "found_by": found_by,
                                                 "note": util.clip(note, 1000), "review_id": review_id, "state": "open",
                                                 "created_at": now, "updated_at": now})
            return int(issue_id), True

    def issue(self, issue_id: int) -> dict[str, Any] | None:
        return _issue(self.db.one("SELECT * FROM issues WHERE id = ?", [issue_id]))

    def issues(self, *, state: str = "open", limit: int = 50) -> list[dict[str, Any]]:
        if state == "all":
            rows = self.db.query("SELECT * FROM issues ORDER BY id DESC LIMIT ?", [limit])
        else:
            rows = self.db.query("SELECT * FROM issues WHERE state = ? ORDER BY id LIMIT ?", [state, limit])
        return [_issue(r) for r in rows]  # type: ignore[misc]

    def close_issue(self, issue_id: int, state: str, *, result: Any = None, note: str = "") -> None:
        values: dict[str, Any] = {"state": state, "updated_at": util.iso(), "result": util.dumps(result or {})}
        if note:
            values["note"] = util.clip(note, 1000)
        if state in ("fixed", "failed", "wontfix"):
            values["closed_at"] = util.iso()
        self.db.update("issues", {"id": issue_id}, values)

    def issue_attempt(self, issue_id: int) -> int:
        row = self.issue(issue_id) or {"attempts": 0}
        attempts = int(row["attempts"]) + 1
        self.db.update("issues", {"id": issue_id}, {"attempts": attempts, "state": "running", "updated_at": util.iso()})
        return attempts

    # -- proposals ----------------------------------------------------------------------------
    def add_proposal(self, setting: str, *, skill: str = "", op: str = "add", value: Any = None, rationale: str = "",
                     evidence: list[str] | None = None, urgent: bool = False, by: str = "maintainer", thread: str = "",
                     state: str = "proposed") -> int:
        return int(self.db.insert("proposals", {"skill": skill, "setting": setting, "op": op, "value": util.dumps(value or {}),
                                                "rationale": util.clip(rationale, 2000), "evidence": util.dumps(evidence or []),
                                                "urgent": 1 if urgent else 0, "by_whom": by, "thread": thread, "state": state,
                                                "created_at": util.iso(), "expires_at": util.ahead(days=PROPOSAL_DAYS)}))

    def proposal(self, proposal_id: int) -> dict[str, Any] | None:
        return _proposal(self.db.one("SELECT * FROM proposals WHERE id = ?", [proposal_id]))

    def proposals(self, *, state: str = "proposed", setting: str = "", limit: int = 50) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM proposals WHERE 1=1", []
        if state != "all":
            sql += " AND state = ?"
            params.append(state)
        if setting:
            sql += " AND setting = ?"
            params.append(setting)
        rows = self.db.query(sql + " ORDER BY id DESC LIMIT ?", [*params, limit])
        return [_proposal(r) for r in rows]  # type: ignore[misc]

    def set_proposal(self, proposal_id: int, **values: Any) -> None:
        if "value" in values:
            values["value"] = util.dumps(values["value"])
        self.db.update("proposals", {"id": proposal_id}, values)

    def expire_proposals(self) -> int:
        cursor = self.db.execute("UPDATE proposals SET state = 'expired' WHERE state = 'proposed' AND expires_at < ?", [util.iso()])
        return int(cursor.rowcount or 0)

    def proposals_today(self, *, by: str = "maintainer") -> int:
        return int(self.db.scalar("SELECT COUNT(*) AS n FROM proposals WHERE by_whom = ? AND created_at >= ?",
                                  [by, util.ago(hours=24)], 0) or 0)
