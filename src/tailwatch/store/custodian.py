"""The custodian's queries (scans and playbooks) and the few reads the desk's tools need that no
other area owns: a table's live columns, recent signals, one intel entry."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from tailwatch import links as link_rules
from tailwatch import util
from tailwatch.store.checks import CURRENT, _verdict
from tailwatch.store.db import Database

# Kinds whose link names one account: two people sharing one of these keys are one person.
IDENTITY_KINDS = ("linkedin", "github", "x", "bluesky", "semantic_scholar", "scholar", "openreview")


class CustodianMixin:
    db: Database
    config: Any

    # -- schema -------------------------------------------------------------------------------
    def table_columns(self, table: str) -> list[dict[str, Any]]:
        """The live columns of one table: name, type, nullable, default."""
        if not table.replace("_", "").isalnum():
            raise ValueError(f"not a table name: {table}")
        if self.db.kind == "sqlite":
            rows = self.db.query(f"PRAGMA table_info({table})")
            return [{"name": r["name"], "type": r["type"], "nullable": not r["notnull"], "default": r["dflt_value"]}
                    for r in rows]
        rows = self.db.query("SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns "
                             "WHERE table_name = ? ORDER BY ordinal_position", [table])
        return [{"name": r["column_name"], "type": r["data_type"], "nullable": r["is_nullable"] == "YES",
                 "default": r["column_default"]} for r in rows]

    # -- scans --------------------------------------------------------------------------------
    def sources_down(self, kinds: Iterable[str], *, limit: int = 50) -> list[dict[str, Any]]:
        """Broken or private sources of tracked people, of the given kinds, oldest check first."""
        kinds = tuple(kinds)
        if not kinds:
            return []
        marks = ", ".join("?" for _ in kinds)
        return self.db.query(
            "SELECT s.*, p.name AS person_name FROM sources s JOIN people p ON p.id = s.person_id "
            f"WHERE s.status IN ('broken', 'private') AND p.status = 'tracked' AND s.kind IN ({marks}) "
            "ORDER BY s.last_checked_at, s.id LIMIT ?", [*kinds, limit])

    def people_without_active_source(self, *, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT p.* FROM people p WHERE p.status = 'tracked' AND NOT EXISTS (SELECT 1 FROM sources s "
            "WHERE s.person_id = p.id AND s.status = 'active' AND s.kind NOT LIKE 'feed:%') ORDER BY p.id LIMIT ?", [limit])
        for row in rows:
            row["links"] = util.loads(row.get("links"), {})
        return rows

    def people_sharing_links(self) -> list[dict[str, Any]]:
        """Pairs of tracked people whose links name one account (the same link key), from the
        roster's links and from the sources each person is read through. ``proof`` is the
        shared key; ``identity`` says whether that key names one account (a lab homepage
        shared by two people does not)."""
        holders: dict[str, set[int]] = {}
        names: dict[int, str] = {}
        for row in self.db.query("SELECT id, name, links FROM people WHERE status = 'tracked'"):
            names[int(row["id"])] = row["name"]
            for kind, url in (util.loads(row["links"], {}) or {}).items():
                key = link_rules.key_of(kind, str(url))
                if key:
                    holders.setdefault(key, set()).add(int(row["id"]))
        for row in self.db.query("SELECT s.person_id, s.kind, s.url FROM sources s JOIN people p ON p.id = s.person_id "
                                 "WHERE p.status = 'tracked' AND s.status != 'paused' AND s.kind NOT LIKE 'feed:%'"):
            key = link_rules.key_of(row["kind"], row["url"])
            if key:
                holders.setdefault(key, set()).add(int(row["person_id"]))
        pairs: dict[tuple[int, int], dict[str, Any]] = {}
        for key, ids in sorted(holders.items()):
            if len(ids) < 2:
                continue
            ordered = sorted(ids)
            for drop in ordered[1:]:
                pair = (ordered[0], drop)
                identity = key.split(":", 1)[0] in IDENTITY_KINDS
                known = pairs.get(pair)
                if known is None or (identity and not known["identity"]):
                    pairs[pair] = {"keep_id": pair[0], "drop_id": drop, "keep_name": names.get(pair[0], ""),
                                   "drop_name": names.get(drop, ""), "proof": key, "identity": identity}
        return list(pairs.values())

    def link_keys_of(self, person_id: int) -> set[str]:
        row = self.db.one("SELECT links FROM people WHERE id = ?", [person_id])
        keys = {link_rules.key_of(k, str(v)) for k, v in (util.loads(row["links"], {}) if row else {}).items()}
        for source in self.db.query("SELECT kind, url FROM sources WHERE person_id = ?", [person_id]):
            keys.add(link_rules.key_of(source["kind"], source["url"]))
        keys |= {r["link_key"] for r in self.db.query("SELECT link_key FROM person_links WHERE person_id = ?", [person_id])}
        return {k for k in keys if k}

    def parked_checks(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return self.db.query("SELECT o.id, o.person_id, o.source_id, o.judge_attempts, o.judge_error, o.observed_at, "
                             "p.name AS person_name FROM observations o JOIN people p ON p.id = o.person_id "
                             "WHERE o.judge_state = 'parked' AND p.status != 'ignored' ORDER BY o.id LIMIT ?", [limit])

    def candidates_waiting(self, *, before: str, limit: int = 200) -> list[dict[str, Any]]:
        return self.db.query("SELECT id, feed, url, subject, updated_at FROM candidates WHERE state = 'needs_review' "
                             "AND updated_at < ? ORDER BY id LIMIT ?", [before, limit])

    # -- playbooks ----------------------------------------------------------------------------
    def reopen_source(self, source_id: int, note: str) -> None:
        """A retired source that answers again: active, streaks cleared, due now."""
        self.db.update("sources", {"id": source_id}, {"status": "active", "status_note": util.clip(note, 500),
                                                      "gone_streak": 0, "fail_streak": 0, "next_due_at": util.iso()})

    def set_issue(self, issue_id: int, **values: Any) -> None:
        """Bookkeeping of one issue (attempts, state, result) that close_issue does not cover."""
        if "result" in values:
            values["result"] = util.dumps(values["result"])
        if "args" in values:
            values["args"] = util.dumps(values["args"])
        values["updated_at"] = util.iso()
        self.db.update("issues", {"id": issue_id}, values)

    def issue_counts(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for row in self.db.query("SELECT state, kind, COUNT(*) AS n FROM issues GROUP BY state, kind ORDER BY state, kind"):
            out.setdefault(row["state"], {})[row["kind"]] = int(row["n"])
        return out

    # -- the channel's reads ------------------------------------------------------------------
    def recent_signals(self, *, since: str, limit: int = 60) -> list[dict[str, Any]]:
        """Current signal verdicts (not none) judged since ``since``, newest first, with the
        person and the source kind."""
        rows = self.db.query(
            "SELECT v.*, p.name AS person_name, p.organization AS person_org, s.kind AS kind, o.observed_at AS observed_at "
            "FROM verdicts v JOIN (" + CURRENT + ") cur ON cur.id = v.id JOIN people p ON p.id = v.person_id "
            "JOIN observations o ON o.id = v.observation_id JOIN sources s ON s.id = o.source_id "
            "WHERE v.signal != 'none' AND v.created_at >= ? ORDER BY v.id DESC LIMIT ?", [since, limit])
        return [_verdict(r) for r in rows]  # type: ignore[misc]

    def intel_entry(self, intel_id: int) -> dict[str, Any] | None:
        return self.db.one("SELECT * FROM intel WHERE id = ?", [intel_id])
