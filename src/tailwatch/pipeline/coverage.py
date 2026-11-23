"""Coverage: who is actually being watched. A first-class number, given on request."""

from __future__ import annotations

from typing import Any

from tailwatch import util


def report(store: Any) -> dict[str, Any]:
    db = store.db
    people = store.count_people()
    no_source = int(db.scalar(
        "SELECT COUNT(*) AS n FROM people p WHERE p.status = 'tracked' AND NOT EXISTS "
        "(SELECT 1 FROM sources s WHERE s.person_id = p.id AND s.status = 'active' AND s.kind NOT LIKE 'feed:%')", [], 0) or 0)
    day_ago, now = util.ago(hours=24), util.iso()
    kinds = []
    for row in db.query("SELECT DISTINCT kind FROM sources WHERE kind NOT LIKE 'feed:%' ORDER BY kind"):
        kind = row["kind"]
        stats = db.one(
            "SELECT SUM(CASE WHEN s.status = 'active' THEN 1 ELSE 0 END) AS active, "
            "SUM(CASE WHEN s.status != 'active' THEN 1 ELSE 0 END) AS inactive, "
            "SUM(CASE WHEN s.status = 'active' AND s.last_checked_at >= ? THEN 1 ELSE 0 END) AS checked_24h, "
            "SUM(CASE WHEN s.status = 'active' AND s.last_checked_at IS NULL THEN 1 ELSE 0 END) AS never, "
            "SUM(CASE WHEN s.status = 'active' AND s.next_due_at < ? AND s.last_checked_at IS NOT NULL THEN 1 ELSE 0 END) AS overdue, "
            "SUM(CASE WHEN s.status = 'active' AND s.fail_streak >= 5 THEN 1 ELSE 0 END) AS failing "
            "FROM sources s JOIN people p ON p.id = s.person_id WHERE s.kind = ? AND p.status = 'tracked'",
            [day_ago, util.ago(hours=12), kind]) or {}
        kinds.append({"kind": kind, **{k: int(v or 0) for k, v in stats.items()}})
        _ = now
    return {"at": util.iso(), "people": people, "no_active_source": no_source, "kinds": kinds,
            "awaiting_judge": store.count_pending(older_than=util.ago(hours=2)), "parked": store.count_parked()}


def render(data: dict[str, Any]) -> str:
    people = data["people"]
    lines = [f"Coverage · {data['at'][:16].replace('T', ' ')} UTC",
             f"{people.get('tracked', 0)} tracked · {people.get('left', 0)} left · {people.get('ignored', 0)} ignored",
             f"{data['no_active_source']} tracked people have no active source"]
    for k in data["kinds"]:
        line = (f"{k['kind']}: {k['active']} active, {k['inactive']} not active, {k['checked_24h']} checked in 24h, "
                f"{k['never']} never checked, {k['overdue']} overdue")
        if k["failing"]:
            line += f", {k['failing']} failing for 5+ reads (retried weekly)"
        lines.append(line)
    if data["awaiting_judge"]:
        lines.append(f"{data['awaiting_judge']} checks awaiting the judge")
    if data["parked"]:
        lines.append(f"{data['parked']} checks could not be judged (parked)")
    return "\n".join(lines)
