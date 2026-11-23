"""The custodian, every scheduler tick: run the open issues that have an allowed playbook, move
the card review behind each, then the scans that are due on their rota.

Bounded in code: 30 issues a tick, three attempts an issue, the playbooks' own caps (three runs
a person a day, ten link enrichments a day). A fix that changed something sends its card's
review back to ``pending`` for the editor (three rounds at most); a fix that changed nothing
holds the card for a person. An issue no playbook can take is closed once the review behind it
is settled, or after a week when it has none. ``DESK:PAUSED`` stops it all.

Never by the custodian: removing a person, a status change, a merge without proof. Those are not
playbooks, so nothing here can do them.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from tailwatch import util
from tailwatch.desk import playbooks, scans

PAUSED_KEY = "DESK:PAUSED"
ISSUES_PER_TICK = 30
ISSUE_ATTEMPTS = 3
REVIEW_ROUNDS = 3
STALE_ISSUE_DAYS = 7
LIVE_REVIEW_STATES = ("pending", "fixing", "held")


def run(store: Any, config: Any, env: Mapping[str, str], *, checkers: Mapping[str, Any] | None = None,
        log: Any = None) -> dict[str, Any]:
    """The scheduler's job. Returns what was run, fixed, held and opened."""
    from tailwatch.store import LockBusy

    held = store.get_setting(PAUSED_KEY)
    if held:
        reason = held.get("reason") if isinstance(held, dict) else held
        return {"stopped": f"paused: {reason if isinstance(reason, str) else 'no reason given'}"}
    try:
        with store.lock("custodian", ttl_s=1800):
            drained = drain(store, config, env, checkers=checkers, log=log)
            scanned = scans.run(store, config, env, checkers=checkers, log=log)
    except LockBusy:
        return {"stopped": "another custodian run holds the lock"}
    return {"drained": drained, "scans": scanned}


def moved_on(store: Any, issue: Mapping[str, Any]) -> bool:
    """An issue no playbook can take is nobody's work once its review is settled, or after a week."""
    if issue.get("review_id"):
        review = store.review(int(issue["review_id"]))
        return review is None or review["state"] not in LIVE_REVIEW_STATES
    return (issue.get("created_at") or "") < util.ago(days=STALE_ISSUE_DAYS)


def drain(store: Any, config: Any, env: Mapping[str, str], *, limit: int = ISSUES_PER_TICK,
          checkers: Mapping[str, Any] | None = None, log: Any = None) -> dict[str, Any]:
    counts: dict[str, Any] = {"run": 0, "fixed": 0, "noop": 0, "failed": 0, "refused": 0, "deferred": 0, "closed": 0,
                              "reviews_reopened": 0, "reviews_held": 0, "items": []}
    capped: set[str] = set()  # playbooks at a daily cap this tick: their other issues wait too
    for issue in store.issues(state="open", limit=500):
        if counts["run"] >= limit:
            break
        name = str(issue.get("playbook") or "")
        if name not in playbooks.KNOWN:
            if moved_on(store, issue):
                store.close_issue(issue["id"], "wontfix", result={"why": "no playbook takes it and it has moved on"})
                counts["closed"] += 1
            continue
        if name in capped:
            continue
        if int(issue.get("attempts") or 0) >= ISSUE_ATTEMPTS:
            store.close_issue(issue["id"], "failed", result={**issue["result"], "why": f"{ISSUE_ATTEMPTS} attempts"})
            counts["failed"] += 1
            continue
        counts["run"] += 1
        result = run_issue(store, config, env, issue, checkers=checkers)
        outcome = result.get("outcome")
        counts[{"error": "failed", "deferred": "deferred", "refused": "refused"}.get(outcome, "fixed" if result.get("changed") else "noop")] += 1
        if outcome == "deferred" and result.get("cap") == "playbook":
            capped.add(name)
        counts["items"].append({"issue": f"#I{issue['id']}", "playbook": name, "changed": bool(result.get("changed")),
                                "what": util.clip(result.get("what") or result.get("refused") or result.get("error") or "", 160)})
        review = result.get("review")
        if review == "pending":
            counts["reviews_reopened"] += 1
        elif review == "held":
            counts["reviews_held"] += 1
    if log and counts["run"]:
        log(f"custodian: {counts['run']} playbook(s) run, {counts['fixed']} fixed, {counts['noop']} changed nothing, "
            f"{counts['failed']} failed, {counts['refused']} refused, {counts['deferred']} deferred")
    return counts


def run_issue(store: Any, config: Any, env: Mapping[str, str], issue: Mapping[str, Any], *, dry_run: bool = False,
              checkers: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Run one issue's playbook and record it: the issue's state and result, and the review
    behind it. Shared by the tick and the desk's ``run_playbook``. ``outcome`` is fixed, error,
    deferred or refused; ``review`` is the review's new state when it moved."""
    name = str(issue.get("playbook") or "")
    if name not in playbooks.KNOWN:
        return {"outcome": "refused", "changed": False,
                "refused": f"#I{issue['id']} has no allowed playbook ({name or 'none'}); a person decides"}
    args = dict(issue.get("args") or {})
    if dry_run:
        try:
            return {"outcome": "dry_run", **playbooks.run(name, store, config, env, args, dry_run=True, checkers=checkers)}
        except playbooks.PlaybookError as error:
            return {"outcome": "error", "changed": False, "error": str(error)}
    attempt = store.issue_attempt(issue["id"])
    try:
        result = playbooks.run(name, store, config, env, args, checkers=checkers)
    except Exception as error:  # one playbook's failure never stops the tick
        result = {"changed": False, "error": f"{type(error).__name__}: {util.clip(error, 300)}"}
    if result.get("error"):
        if attempt >= ISSUE_ATTEMPTS:
            store.close_issue(issue["id"], "failed", result=result)
        else:
            store.set_issue(issue["id"], state="open", result=result)
        return {"outcome": "error", **result}
    if result.get("retry_later"):
        store.set_issue(issue["id"], state="open", attempts=max(0, attempt - 1), result=result)
        return {"outcome": "deferred", **result}
    outcome = "refused" if result.get("refused") else "fixed"
    store.close_issue(issue["id"], "wontfix" if outcome == "refused" else "fixed", result=result)
    moved = settle_review(store, issue, changed=bool(result.get("changed")) and outcome == "fixed",
                          what=str(result.get("what") or result.get("refused") or ""))
    return {"outcome": outcome, **result, **({"review": moved} if moved else {})}


def settle_review(store: Any, issue: Mapping[str, Any], *, changed: bool, what: str) -> str:
    """The card behind a fixed issue: back to the editor when the fix changed something and the
    card has rounds left, else held for a person. Returns the new state, or ''."""
    if not issue.get("review_id"):
        return ""
    review = store.review(int(issue["review_id"]))
    if review is None or review["state"] != "fixing":
        return ""
    rounds = int(review.get("rounds") or 0)
    if changed and rounds < REVIEW_ROUNDS:
        moved = store.set_review(review["id"], state="pending", rounds=rounds + 1, expect="fixing",
                                 note=util.clip(f"custodian fixed #I{issue['id']}: {what}; check it again", 2000))
        return "pending" if moved else ""
    why = "the fix changed nothing" if not changed else f"{REVIEW_ROUNDS} fix rounds already"
    moved = store.set_review(review["id"], state="held", expect="fixing",
                             note=util.clip(f"held: {why} (#I{issue['id']}: {what})", 2000))
    return "held" if moved else ""


def health(store: Any, config: Any) -> dict[str, Any]:
    """What the data skill's ``health`` shows: issue counts, the scans' last runs, the playbooks."""
    last = {}
    for kind in scans.KINDS:
        when = util.parse(store.get_setting(f"{scans.SCAN_KEY}{kind}", ""))
        due_in = None
        if when is not None:
            due_in = max(0.0, (when + timedelta(hours=scans.COOLDOWN_HOURS[kind]) - util.now()).total_seconds() / 3600)
        last[kind] = {"last_run": util.iso(when)[:16] if when else None,
                      "due_in_hours": round(due_in, 1) if due_in is not None else 0.0}
    return {"issues": store.issue_counts(), "scans": last, "playbooks": playbooks.describe(),
            "paused": store.get_setting(PAUSED_KEY) or None}
