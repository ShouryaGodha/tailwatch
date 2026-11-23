"""Source passes: check every due source of every tracked person, record what changed.

The rules (each learned from a real failure of the system this reproduces):

- A pass checks sources due within ``DUE_GRACE_HOURS`` of its start, never-checked first, so a
  daily source checked a few minutes into today's pass is due again at tomorrow's.
- The first read of a source records only. Changes start from the second read.
- ``gone`` (or ``private``) once changes nothing: it can be a glitch at the provider. Twice in a
  row retires the source and writes an ``account gone`` event, judged like any other change.
- An error is retried after six hours. Five failed reads in a row move the source to a weekly
  retry with the reason on its row; one good read puts it back on its cadence.
- A read the checker's guard rejects (it looks like a change but is a failed read) is an error.
- A pass whose first ``OUTAGE_SAMPLE`` checks all fail with the same provider outage (no
  credits, every key refused) stops at once and leaves the rest due, instead of failing check by
  check; the caller reports it once.
- Every check reports what it cost; the pass returns the total.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tailwatch import util
from tailwatch.connectors.base import CheckContext, Read
from tailwatch.connectors.http import Http, HttpError

DUE_GRACE_HOURS = 12
RETRY_HOURS = 6
FAILING_AFTER = 5
FAILING_RETRY_HOURS = 168
OUTAGE_SAMPLE = 15
DEFAULT_LIMIT = 5000


def content_hash(content: Mapping[str, Any]) -> str:
    return util.sha(util.dumps(dict(content)))


def context_for(store: Any, config: Any, env: Mapping[str, str], kind: str, http: Http | None = None) -> CheckContext:
    options = config.connector(kind).options if config is not None else {}
    return CheckContext(http=http or Http(), env=env, options=options,
                        cache_get=lambda p, k, age: store.cache_get(p, k, max_age_hours=age),
                        cache_put=store.cache_put)


def check_source(store: Any, checker: Any, source: Mapping[str, Any], ctx: CheckContext) -> dict[str, Any]:
    """Read one source and record the check. Returns {status, events, usd, credits, outage}."""
    person = store.person(source["person_id"]) or {}
    snapshot = {"name": person.get("name"), "organization": person.get("organization"), "title": person.get("title")}
    previous = store.last_ok_observation(source["id"])
    before = previous["content"] if previous else None
    try:
        read: Read = checker.read(source["url"], before, ctx)
    except HttpError as error:
        read = Read.failed(str(error))
    except Exception as error:  # a checker bug must not stop the pass
        read = Read.failed(f"{type(error).__name__}: {error}")
    if read.status == "ok" and before is not None:
        reason = checker.guard(before, read.content)
        if reason:  # a rejected read still cost what it cost
            read = Read(status="error", error=f"failed read: {reason}", credits=read.credits, usd=read.usd)
    now = util.now()
    cadence = float(source.get("cadence_hours") or checker.cadence_hours)
    events: list[dict[str, Any]] = []
    update: dict[str, Any] = {}
    status = read.status
    if status == "ok":
        first = previous is None
        if not first and content_hash(read.content) != previous["content_hash"]:
            events = [c.as_dict() for c in checker.diff(before or {}, read.content)]
        update = {"next_due_at": util.iso(now + _hours(cadence)), "fail_streak": 0, "gone_streak": 0, "status_note": ""}
        store.record_check(source, status="ok", content=read.content, content_hash=content_hash(read.content),
                           credits=read.credits, first_read=first, changes=events,
                           previous_id=previous["id"] if previous else None, person_snapshot=snapshot, source_update=update)
    elif status in ("gone", "private"):
        streak = int(source.get("gone_streak") or 0) + 1
        if streak >= 2:
            retired = "broken" if status == "gone" else "private"
            events = [{"label": "account gone" if status == "gone" else "account made private",
                       "before": checker.describe(before) if before else "", "after": read.error or status}]
            update = {"gone_streak": streak, "status": retired, "status_note": util.clip(read.error or status, 300)}
        else:
            update = {"gone_streak": streak, "next_due_at": util.iso(now + _hours(RETRY_HOURS))}
        store.record_check(source, status=status, error=read.error, credits=read.credits, changes=events,
                           previous_id=previous["id"] if previous else None, person_snapshot=snapshot, source_update=update)
    else:
        streak = int(source.get("fail_streak") or 0) + 1
        update = {"fail_streak": streak, "next_due_at": util.iso(now + _hours(RETRY_HOURS))}
        if streak >= FAILING_AFTER:
            since = source.get("status_note") if str(source.get("status_note") or "").startswith("failing since") else \
                f"failing since {util.iso(now)[:10]}: {util.clip(read.error, 200)}"
            update.update({"next_due_at": util.iso(now + _hours(FAILING_RETRY_HOURS)), "status_note": since})
        store.record_check(source, status="error", error=read.error, credits=read.credits, person_snapshot=snapshot,
                           source_update=update)
    return {"status": status, "events": len(events), "usd": read.usd, "credits": read.credits, "outage": read.outage,
            "error": read.error}


def _hours(value: float) -> Any:
    from datetime import timedelta

    return timedelta(hours=value)


def run_kind(store: Any, config: Any, env: Mapping[str, str], kind: str, checker: Any, *, limit: int | None = None,
             http: Http | None = None) -> dict[str, Any]:
    ctx = context_for(store, config, env, kind, http)
    due = store.due_sources(kind, before=util.ahead(hours=DUE_GRACE_HOURS), limit=limit or DEFAULT_LIMIT)
    result = {"kind": kind, "due": len(due), "checked": 0, "ok": 0, "errors": 0, "gone": 0, "events": 0, "usd": 0.0,
              "credits": 0.0, "outage": ""}
    outages: list[str] = []
    for source in due:
        outcome = check_source(store, checker, source, ctx)
        result["checked"] += 1
        result["events"] += outcome["events"]
        result["usd"] = round(result["usd"] + outcome["usd"], 6)
        result["credits"] += outcome["credits"]
        result["ok" if outcome["status"] == "ok" else "gone" if outcome["status"] in ("gone", "private") else "errors"] += 1
        if len(outages) < OUTAGE_SAMPLE:
            outages.append(outcome["outage"])
            if len(outages) == OUTAGE_SAMPLE and _one_outage(outages):
                result["outage"] = f"{kind}: {outages[0]} on the first {OUTAGE_SAMPLE} checks; the rest stay due"
                break
    if not result["outage"] and 0 < len(outages) < OUTAGE_SAMPLE and _one_outage(outages):
        result["outage"] = f"{kind}: {outages[0]}"
    return result


def _one_outage(outages: list[str]) -> bool:
    return bool(outages[0]) and len(set(outages)) == 1


def run_pass(store: Any, config: Any, env: Mapping[str, str], *, kinds: list[str] | None = None, limit: int | None = None,
             http: Http | None = None, judge: bool = True, log: Any = print) -> dict[str, Any]:
    """Sync sources from the watchlist, check every due source of the enabled connectors, then
    judge what is waiting."""
    from tailwatch.connectors.registry import enabled_checkers

    checkers = enabled_checkers(config, env)
    store.sync_sources(checkers)
    results = []
    for kind, checker in checkers.items():
        if kinds and kind not in kinds:
            continue
        outcome = run_kind(store, config, env, kind, checker, limit=limit, http=http)
        results.append(outcome)
        if log and outcome["checked"]:
            log(f"{kind}: {outcome['checked']} checked, {outcome['events']} change(s), {outcome['errors']} error(s)"
                + (f", ${outcome['usd']:.2f}" if outcome["usd"] else "") + (f" — stopped: {outcome['outage']}" if outcome["outage"] else ""))
    summary: dict[str, Any] = {"kinds": results, "usd": round(sum(r["usd"] for r in results), 6),
                               "outages": [r["outage"] for r in results if r["outage"]]}
    if judge:
        from tailwatch.pipeline.judge import judge_pending

        summary["judge"] = judge_pending(store, config, env, checkers=checkers, log=log)
    return summary
