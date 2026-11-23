"""Store methods owned by the review area (the editor, its floors and a person's decisions on a
card). Add queries here, never raw SQL elsewhere."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from tailwatch import links as link_rules
from tailwatch import util
from tailwatch.store.checks import _verdict
from tailwatch.store.db import Database

# The same columns ``waiting_cards`` gives the digest, plus the source's id and status, so a card
# reads the same in the editor, the floors and the digest.
CARD_COLUMNS = (
    "SELECT v.*, o.source_id AS source_id, s.kind AS kind, s.url AS source_url, s.status AS source_status, "
    "o.observed_at AS observed_at, o.person_snapshot AS snapshot, p.name AS person_name, p.organization AS person_org, "
    "p.title AS person_title, p.links AS person_links, p.tags AS person_tags, p.status AS person_status "
    "FROM verdicts v JOIN observations o ON o.id = v.observation_id JOIN sources s ON s.id = o.source_id "
    "JOIN people p ON p.id = v.person_id"
)


def _card(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    _verdict(row)
    row["snapshot"] = util.loads(row.get("snapshot"), {})
    row["person_links"] = util.loads(row.get("person_links"), {})
    row["person_tags"] = util.loads(row.get("person_tags"), [])
    return row


class ReviewMixin:
    db: Database
    config: Any

    def card_row(self, verdict_id: int) -> dict[str, Any] | None:
        """One verdict as a card row, whatever its age and whether or not it was posted."""
        return _card(self.db.one(CARD_COLUMNS + " WHERE v.id = ?", [verdict_id]))

    def posted_cards(self, person_id: int, *, since: str, channel: str = "digest") -> list[dict[str, Any]]:
        """The person's cards a channel carried since ``since``: the receipts the duplicate floor reads.
        The shadow trial's receipts are its own and never count as a post."""
        rows = self.db.query(
            "SELECT x.verdict_id AS verdict_id, x.move_key AS move_key, x.at AS at, x.by_whom AS by_whom, v.signal AS signal, "
            "v.summary AS summary, v.now AS now FROM posts x JOIN verdicts v ON v.id = x.verdict_id "
            "WHERE x.person_id = ? AND x.at >= ? AND x.channel = ? ORDER BY x.at DESC", [person_id, since, channel])
        for row in rows:
            row["now"] = util.loads(row.get("now"), {})
        return rows

    def posted_on(self, verdict_id: int, channel: str = "digest") -> bool:
        return bool(self.db.one("SELECT 1 AS x FROM posts WHERE verdict_id = ? AND channel = ?", [verdict_id, channel]))

    def posted_card_rows(self, *, since: str, channel: str = "digest", limit: int = 200) -> list[dict[str, Any]]:
        """Card rows a channel carried since ``since``, newest first (the replay's input)."""
        rows = self.db.query(CARD_COLUMNS + " WHERE v.id IN (SELECT verdict_id FROM posts WHERE channel = ? AND at >= ?) "
                             "ORDER BY v.id DESC LIMIT ?", [channel, since, limit])
        return [_card(r) for r in rows]  # type: ignore[misc]

    def link_holders(self, person_id: int, links: Iterable[tuple[str, str]]) -> dict[str, int]:
        """Each of these (kind, url) links that another listed person holds, as url -> their id:
        their own link on the list, or a source of theirs at the same url."""
        out: dict[str, int] = {}
        for kind, url in links:
            if not url:
                continue
            key = link_rules.key_of(kind, url)
            if key:
                row = self.db.one("SELECT l.person_id AS person_id FROM person_links l JOIN people p ON p.id = l.person_id "
                                  "WHERE l.kind = ? AND l.link_key = ? AND l.person_id != ? AND p.status != 'ignored'",
                                  [kind, key, person_id])
                if row:
                    out[url] = int(row["person_id"])
                    continue
            row = self.db.one("SELECT s.person_id AS person_id FROM sources s JOIN people p ON p.id = s.person_id "
                              "WHERE s.url = ? AND s.person_id != ? AND p.status != 'ignored' LIMIT 1", [url, person_id])
            if row:
                out[url] = int(row["person_id"])
        return out

    def latest_kind_read(self, person_id: int, kind: str) -> dict[str, Any] | None:
        """The newest successful read of any of the person's sources of one kind (id and time only)."""
        return self.db.one("SELECT o.id AS id, o.observed_at AS observed_at FROM observations o JOIN sources s ON s.id = o.source_id "
                           "WHERE o.person_id = ? AND s.kind = ? AND o.fetch_status = 'ok' ORDER BY o.id DESC LIMIT 1",
                           [person_id, kind])

    def live_reviews(self, states: tuple[str, ...] = ("pending", "fixing", "held"), *, limit: int = 1000) -> list[dict[str, Any]]:
        """Reviews in any of ``states``, oldest first."""
        from tailwatch.store.cards import _review

        rows = self.db.query(f"SELECT * FROM card_reviews WHERE state IN ({', '.join('?' for _ in states)}) ORDER BY id LIMIT ?",
                             [*states, limit])
        return [_review(r) for r in rows]  # type: ignore[misc]
