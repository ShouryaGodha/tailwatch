"""Cards: what the digest may carry (current signal verdicts), the editor's reviews, the
digests with their delivery state, the receipts of what was posted, and labels."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tailwatch import util
from tailwatch.store.checks import CURRENT, _verdict
from tailwatch.store.db import Database

REVIEW_STATES = ("pending", "verified", "fixing", "held", "dropped", "superseded")


def _review(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    for key in ("answer", "floors", "decision"):
        row[key] = util.loads(row.get(key), {})
    return row


class CardsMixin:
    db: Database
    config: Any

    # -- the queue ----------------------------------------------------------------------------
    def waiting_cards(self, *, since: str, channel: str) -> list[dict[str, Any]]:
        """Current signal verdicts (not none, eligible) of people still tracked, judged since
        ``since``, that this channel has not carried. With the person, the source kind and the
        observation time."""
        rows = self.db.query(
            "SELECT v.*, s.kind AS kind, s.url AS source_url, o.observed_at AS observed_at, o.person_snapshot AS snapshot, "
            "p.name AS person_name, p.organization AS person_org, p.title AS person_title, p.links AS person_links, "
            "p.tags AS person_tags, p.status AS person_status FROM verdicts v JOIN (" + CURRENT + ") cur ON cur.id = v.id "
            "JOIN observations o ON o.id = v.observation_id JOIN sources s ON s.id = o.source_id JOIN people p ON p.id = v.person_id "
            "WHERE v.signal != 'none' AND v.eligible = 1 AND p.status = 'tracked' AND o.observed_at >= ? "
            "AND NOT EXISTS (SELECT 1 FROM posts x WHERE x.verdict_id = v.id AND x.channel = ?) ORDER BY o.observed_at, v.id",
            [since, channel])
        for row in rows:
            _verdict(row)
            row["snapshot"] = util.loads(row.get("snapshot"), {})
            row["person_links"] = util.loads(row.get("person_links"), {})
            row["person_tags"] = util.loads(row.get("person_tags"), [])
        return rows

    def posted_moves(self, person_id: int, *, since: str, channel: str = "digest") -> list[dict[str, Any]]:
        """What a channel's digests carried for the person (a shadow trial's receipts are its own)."""
        rows = self.db.query("SELECT p.*, v.signal AS signal, v.now AS now FROM posts p JOIN verdicts v ON v.id = p.verdict_id "
                             "WHERE p.person_id = ? AND p.at >= ? AND p.channel = ? ORDER BY p.at DESC", [person_id, since, channel])
        for row in rows:
            row["now"] = util.loads(row.get("now"), {})
        return rows

    def add_post(self, verdict_id: int, person_id: int, *, channel: str, digest_id: int | None, move_key: str,
                 by: str = "digest") -> None:
        if self.db.one("SELECT 1 AS x FROM posts WHERE verdict_id = ? AND channel = ?", [verdict_id, channel]):
            return
        self.db.insert("posts", {"verdict_id": verdict_id, "person_id": person_id, "digest_id": digest_id, "channel": channel,
                                 "move_key": move_key, "by_whom": by, "at": util.iso()}, returning="")

    def was_posted(self, verdict_id: int) -> bool:
        return bool(self.db.one("SELECT 1 AS x FROM posts WHERE verdict_id = ?", [verdict_id]))

    # -- digests (durable delivery) -----------------------------------------------------------
    def digest(self, channel: str, day: str) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM digests WHERE channel = ? AND day = ?", [channel, day])
        if row:
            row["payload"] = util.loads(row["payload"], {})
        return row

    def prepare_digest(self, channel: str, day: str, *, text: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """The day's digest row for a channel, created once. A prepared (never sent) row takes the
        new text; a sending or sent row is returned untouched."""
        existing = self.digest(channel, day)
        if existing and existing["state"] != "prepared":
            return existing
        if existing:
            self.db.update("digests", {"id": existing["id"]}, {"text": text, "payload": util.dumps(dict(payload)), "updated_at": util.iso()})
        else:
            self.db.insert("digests", {"channel": channel, "day": day, "state": "prepared", "message_key": util.sha(f"{channel}:{day}")[:32],
                                       "text": text, "payload": util.dumps(dict(payload)), "created_at": util.iso(), "updated_at": util.iso()})
        return self.digest(channel, day)  # type: ignore[return-value]

    def digest_state(self, digest_id: int, state: str, *, ts: str = "") -> None:
        values: dict[str, Any] = {"state": state, "updated_at": util.iso()}
        if ts:
            values["ts"] = ts
        self.db.update("digests", {"id": digest_id}, values)

    def digests(self, *, limit: int = 14) -> list[dict[str, Any]]:
        return self.db.query("SELECT id, channel, day, state, ts, created_at FROM digests ORDER BY id DESC LIMIT ?", [limit])

    # -- the editor's reviews -----------------------------------------------------------------
    def review(self, review_id: int) -> dict[str, Any] | None:
        return _review(self.db.one("SELECT * FROM card_reviews WHERE id = ?", [review_id]))

    def review_for(self, verdict_id: int) -> dict[str, Any] | None:
        return _review(self.db.one("SELECT * FROM card_reviews WHERE verdict_id = ?", [verdict_id]))

    def open_review(self, verdict: Mapping[str, Any]) -> dict[str, Any]:
        found = self.review_for(int(verdict["id"]))
        if found:
            return found
        now = util.iso()
        review_id = self.db.insert("card_reviews", {"person_id": verdict["person_id"], "verdict_id": verdict["id"],
                                                    "observation_id": verdict["observation_id"], "state": "pending",
                                                    "created_at": now, "updated_at": now})
        return self.review(int(review_id))  # type: ignore[return-value]

    def set_review(self, review_id: int, *, state: str | None = None, answer: Any = None, floors: Any = None,
                   decision: Any = None, note: str | None = None, rounds: int | None = None,
                   expect: str | None = None) -> bool:
        """Update a review; with ``expect`` the change applies only if the state is still that one
        (an optimistic check, so two workers never both decide one card). Returns whether it applied."""
        values: dict[str, Any] = {"updated_at": util.iso()}
        if state is not None:
            if state not in REVIEW_STATES:
                raise ValueError(f"unknown review state {state}")
            values["state"] = state
        for key, value in (("answer", answer), ("floors", floors), ("decision", decision)):
            if value is not None:
                values[key] = util.dumps(value)
        if note is not None:
            values["note"] = util.clip(note, 2000)
        if rounds is not None:
            values["rounds"] = rounds
        where: dict[str, Any] = {"id": review_id}
        if expect is not None:
            where["state"] = expect
        return self.db.update("card_reviews", where, values) > 0

    def reviews(self, *, state: str = "", limit: int = 50, since: str = "") -> list[dict[str, Any]]:
        sql = ("SELECT r.*, p.name AS person_name, v.signal AS signal, v.summary AS summary FROM card_reviews r "
               "JOIN people p ON p.id = r.person_id JOIN verdicts v ON v.id = r.verdict_id WHERE 1=1")
        params: list[Any] = []
        if state:
            sql += " AND r.state = ?"
            params.append(state)
        if since:
            sql += " AND r.updated_at >= ?"
            params.append(since)
        rows = self.db.query(sql + " ORDER BY r.id DESC LIMIT ?", [*params, limit])
        return [_review(r) for r in rows]  # type: ignore[misc]

    def review_counts(self, *, since: str) -> dict[str, int]:
        rows = self.db.query("SELECT state, COUNT(*) AS n FROM card_reviews WHERE updated_at >= ? GROUP BY state", [since])
        return {r["state"]: int(r["n"]) for r in rows}

    # -- labels (the holdout set) -------------------------------------------------------------
    def add_label(self, verdict_id: int, label: str, *, holdout: bool = False, note: str = "", by: str = "") -> int:
        if label not in ("right", "wrong", "unsure"):
            raise ValueError("label is right, wrong or unsure")
        return int(self.db.insert("card_labels", {"verdict_id": verdict_id, "label": label, "holdout": 1 if holdout else 0,
                                                  "note": note, "by_whom": by, "at": util.iso()}))

    def labels(self, *, holdout_only: bool = False) -> list[dict[str, Any]]:
        sql = ("SELECT l.*, r.state AS review_state, r.answer AS answer FROM card_labels l "
               "LEFT JOIN card_reviews r ON r.verdict_id = l.verdict_id")
        if holdout_only:
            sql += " WHERE l.holdout = 1"
        rows = self.db.query(sql + " ORDER BY l.id")
        for row in rows:
            row["answer"] = util.loads(row.get("answer"), {})
        return rows

    def human_verdict(self, observation_id: int, person_id: int, signal: str, *, reason: str, by: str,
                      judged_by: str = "human") -> int:
        """A person's (or the desk's) correction: a new current verdict on the check."""
        current = self.current_verdict(observation_id) or {}  # type: ignore[attr-defined]
        return self.save_verdict(observation_id, person_id, {**current, "signal": signal, "reason": reason,  # type: ignore[attr-defined]
                                                             "confidence": 1.0}, judged_by=judged_by, by_whom=by)
