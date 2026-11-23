"""The watchlist: people, their links (one spelling each), sources, removals and merges."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from tailwatch import links as link_rules
from tailwatch import util
from tailwatch.store.db import Database

PERSON_FIELDS = ("name", "organization", "title", "since", "headline", "location", "notes")


def _person(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    row["links"] = util.loads(row.get("links"), {})
    row["tags"] = util.loads(row.get("tags"), [])
    return row


class PeopleMixin:
    db: Database
    config: Any

    def person(self, person_id: int) -> dict[str, Any] | None:
        return _person(self.db.one("SELECT * FROM people WHERE id = ?", [person_id]))

    def people(self, *, status: str | None = "tracked", limit: int = 100000) -> list[dict[str, Any]]:
        if status:
            rows = self.db.query("SELECT * FROM people WHERE status = ? ORDER BY id LIMIT ?", [status, limit])
        else:
            rows = self.db.query("SELECT * FROM people ORDER BY id LIMIT ?", [limit])
        return [_person(r) for r in rows]  # type: ignore[misc]

    def count_people(self) -> dict[str, int]:
        rows = self.db.query("SELECT status, COUNT(*) AS n FROM people GROUP BY status")
        return {r["status"]: int(r["n"]) for r in rows}

    def person_by_key(self, key: str) -> dict[str, Any] | None:
        kind = key.split(":", 1)[0]
        row = self.db.one("SELECT person_id FROM person_links WHERE kind = ? AND link_key = ?", [kind, key])
        return self.person(int(row["person_id"])) if row else None

    def person_by_link(self, kind: str, value: str) -> dict[str, Any] | None:
        key = link_rules.key_of(kind, value)
        return self.person_by_key(key) if key else None

    def find_people(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """By id, a link, or words of the name, organization or title."""
        query = query.strip().lstrip("#")
        if query.isdigit():
            found = self.person(int(query))
            return [found] if found else []
        if "/" in query or query.startswith("@"):
            kind = link_rules.guess_kind(query)
            found = self.person_by_link(kind, query)
            if found:
                return [found]
        like = f"%{query.lower()}%"
        rows = self.db.query("SELECT * FROM people WHERE LOWER(name) LIKE ? OR LOWER(organization) LIKE ? "
                             "OR LOWER(title) LIKE ? ORDER BY CASE WHEN LOWER(name) LIKE ? THEN 0 ELSE 1 END, id LIMIT ?",
                             [like, like, like, like, limit])
        return [_person(r) for r in rows]  # type: ignore[misc]

    def removed_keys(self) -> set[str]:
        keys: set[str] = set()
        for row in self.db.query("SELECT link_keys FROM removals"):
            keys.update(util.loads(row["link_keys"], []))
        return keys

    def add_person(self, name: str, *, organization: str = "", title: str = "", links: Mapping[str, str] | None = None,
                   tags: Iterable[str] = (), notes: str = "", added_because: str = "", force: bool = False,
                   since: str = "") -> tuple[int, bool]:
        """Add a person, or return the one already listed on any of these links.
        Returns (id, created). A person removed earlier is not added back unless ``force``."""
        normalized = link_rules.normalize_all(dict(links or {}))
        keys = {kind: link_rules.key_of(kind, url) for kind, url in normalized.items()}
        with self.db.transaction():
            for kind, key in keys.items():
                existing = self.db.one("SELECT person_id FROM person_links WHERE kind = ? AND link_key = ?", [kind, key])
                if existing:
                    return int(existing["person_id"]), False
            if not force and set(keys.values()) & self.removed_keys():
                raise PermissionError(f"{name} was removed from the list earlier; add with force to bring them back")
            now = util.iso()
            person_id = self.db.insert("people", {
                "name": name.strip(), "organization": organization.strip(), "title": title.strip(), "since": since,
                "links": util.dumps(normalized), "tags": util.dumps(sorted(set(tags))), "notes": notes,
                "added_because": added_because, "status": "tracked", "created_at": now, "updated_at": now})
            for kind, url in normalized.items():
                self.db.insert("person_links", {"person_id": person_id, "kind": kind, "link_key": keys[kind], "url": url},
                               returning="")
        return int(person_id), True

    def add_links(self, person_id: int, links: Mapping[str, str], *, overwrite: bool = False) -> dict[str, str]:
        """Fill a person's empty link kinds (or overwrite). A link another person holds is skipped.
        Returns what was added."""
        person = self.person(person_id)
        if person is None:
            raise KeyError(f"no person {person_id}")
        current = dict(person["links"])
        added: dict[str, str] = {}
        with self.db.transaction():
            for kind, url in link_rules.normalize_all(dict(links)).items():
                if current.get(kind) and not overwrite:
                    continue
                key = link_rules.key_of(kind, url)
                holder = self.db.one("SELECT person_id FROM person_links WHERE kind = ? AND link_key = ?", [kind, key])
                if holder and int(holder["person_id"]) != person_id:
                    continue
                if current.get(kind):
                    self.db.execute("DELETE FROM person_links WHERE person_id = ? AND kind = ?", [person_id, kind])
                if not holder:
                    self.db.insert("person_links", {"person_id": person_id, "kind": kind, "link_key": key, "url": url},
                                   returning="")
                current[kind] = url
                added[kind] = url
            if added:
                self.db.update("people", {"id": person_id}, {"links": util.dumps(current), "updated_at": util.iso()})
        return added

    def update_person(self, person_id: int, *, by: str = "", **fields: Any) -> None:
        """Change roster fields (organization, title, since, headline, location, notes, tags)."""
        values = {k: v for k, v in fields.items() if k in (*PERSON_FIELDS, "tags", "links_enriched_at")}
        if "tags" in values:
            values["tags"] = util.dumps(sorted(set(values["tags"])))
        if not values:
            return
        values["updated_at"] = util.iso()
        self.db.update("people", {"id": person_id}, values)

    def set_status(self, person_id: int, status: str, *, reason: str = "", by: str = "") -> None:
        if status not in ("tracked", "left", "ignored"):
            raise ValueError("status is tracked, left or ignored")
        self.db.update("people", {"id": person_id}, {"status": status, "status_reason": reason, "status_by": by,
                                                     "status_at": util.iso(), "updated_at": util.iso()})

    def remove_person(self, person_id: int, *, reason: str, by: str = "") -> None:
        person = self.person(person_id)
        if person is None:
            raise KeyError(f"no person {person_id}")
        keys = [r["link_key"] for r in self.db.query("SELECT link_key FROM person_links WHERE person_id = ?", [person_id])]
        with self.db.transaction():
            self.set_status(person_id, "ignored", reason=reason, by=by)
            self.db.insert("removals", {"person_id": person_id, "name": person["name"], "link_keys": util.dumps(keys),
                                        "reason": reason, "by_whom": by, "at": util.iso()})

    # -- sources ------------------------------------------------------------------------------
    def source(self, source_id: int) -> dict[str, Any] | None:
        return self.db.one("SELECT * FROM sources WHERE id = ?", [source_id])

    def sources_of(self, person_id: int) -> list[dict[str, Any]]:
        return self.db.query("SELECT * FROM sources WHERE person_id = ? ORDER BY kind, id", [person_id])

    def ensure_source(self, person_id: int, kind: str, url: str, *, cadence_hours: float, status: str = "active") -> int:
        row = self.db.one("SELECT id FROM sources WHERE person_id = ? AND kind = ? AND url = ?", [person_id, kind, url])
        if row:
            return int(row["id"])
        return int(self.db.insert("sources", {"person_id": person_id, "kind": kind, "url": url, "status": status,
                                              "cadence_hours": cadence_hours, "next_due_at": util.iso(),
                                              "created_at": util.iso()}))

    def set_source_status(self, source_id: int, status: str, note: str = "") -> None:
        self.db.update("sources", {"id": source_id}, {"status": status, "status_note": util.clip(note, 500)})

    def sync_sources(self, checkers: Mapping[str, Any]) -> dict[str, int]:
        """Give every link of every tracked person whose kind has an enabled checker a source row.
        ``checkers`` maps kind -> checker (with ``cadence_hours``)."""
        created = 0
        for person in self.people(status="tracked"):
            for kind, url in person["links"].items():
                checker = checkers.get(kind)
                if checker is None:
                    continue
                exists = self.db.one("SELECT id FROM sources WHERE person_id = ? AND kind = ? AND url = ?",
                                     [person["id"], kind, url])
                if not exists:
                    self.ensure_source(person["id"], kind, url, cadence_hours=checker.cadence_hours)
                    created += 1
        return {"created": created}

    def merge_people(self, keep: int, drop: int, *, by: str = "") -> dict[str, Any]:
        """Move everything of ``drop`` onto ``keep``; ``drop`` is marked ignored, nothing deleted."""
        if keep == drop:
            raise ValueError("cannot merge a person into themselves")
        kept, dropped = self.person(keep), self.person(drop)
        if kept is None or dropped is None:
            raise KeyError("both people must exist")
        with self.db.transaction():
            for row in self.db.query("SELECT * FROM sources WHERE person_id = ?", [drop]):
                twin = self.db.one("SELECT id FROM sources WHERE person_id = ? AND kind = ? AND url = ?", [keep, row["kind"], row["url"]])
                if twin:
                    self.db.execute("UPDATE observations SET source_id = ?, person_id = ? WHERE source_id = ?", [twin["id"], keep, row["id"]])
                    self.db.execute("UPDATE events SET source_id = ?, person_id = ? WHERE source_id = ?", [twin["id"], keep, row["id"]])
                    self.set_source_status(row["id"], "paused", f"merged into person {keep}")
                else:
                    self.db.update("sources", {"id": row["id"]}, {"person_id": keep})
            for table in ("observations", "events", "verdicts", "card_reviews", "posts"):
                self.db.execute(f"UPDATE {table} SET person_id = ? WHERE person_id = ?", [keep, drop])
            for row in self.db.query("SELECT * FROM person_links WHERE person_id = ?", [drop]):
                if not self.db.one("SELECT 1 AS x FROM person_links WHERE person_id = ? AND kind = ?", [keep, row["kind"]]):
                    self.db.update("person_links", {"kind": row["kind"], "link_key": row["link_key"]}, {"person_id": keep})
            merged_links = {**dropped["links"], **kept["links"]}
            self.db.update("people", {"id": keep}, {"links": util.dumps(merged_links), "updated_at": util.iso()})
            self.set_status(drop, "ignored", reason=f"merged into #{keep}", by=by)
        return {"kept": keep, "dropped": drop}
