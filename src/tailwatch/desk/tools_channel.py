"""The digest channel's bot: why a card says what it says, coverage, the recent alerts, a recheck
of one person on request, a label on a card, and a status change in two steps.

A status change is proposed first (``propose_status`` writes nothing to the list: it keeps the
proposal in the thread's memory and returns the question to ask) and applied only after a yes
in a later message of the same thread (``apply_status``, a writing tool). A proposal older than
a day is dropped.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tailwatch import util
from tailwatch.agent.registry import P, ToolContext, Toolbox

TOOLS = Toolbox("channel")

STATUSES = ("tracked", "left", "ignored")
LABELS = ("right", "wrong", "unsure")
PENDING_HOURS = 24


@TOOLS.tool("coverage", "The coverage report: who is tracked, who has no active source, what is overdue or never checked. "
            "Post the report as it is when asked for it.")
def coverage(ctx: ToolContext) -> dict[str, Any]:
    from tailwatch.pipeline import coverage as cover

    data = cover.report(ctx.store)
    return {"report": cover.render(data), "no_active_source": data["no_active_source"]}


@TOOLS.tool("recent_alerts", "The current signals judged in the last `days` days, newest first: person, signal, summary, "
            "confidence, source, verdict id.", days=P("integer", "1 to 90"))
def recent_alerts(ctx: ToolContext, days: int) -> dict[str, Any]:
    days = max(1, min(int(days or 7), 90))
    rows = ctx.store.recent_signals(since=util.ago(days=days), limit=60)
    return {"days": days, "alerts": [{"verdict_id": r["id"], "person_id": r["person_id"], "person": r["person_name"],
                                      "organization": r["person_org"], "signal": r["signal"], "summary": r["summary"],
                                      "confidence": r["confidence"], "source": r["kind"], "seen": r["observed_at"][:10],
                                      "judged_by": r["judged_by"]} for r in rows]}


@TOOLS.tool("recheck_person", "Check this person's active sources now, record what is found and judge the new checks. "
            "Nothing is posted: a signal goes into the next digest. Returns the checks, the answers and whether the "
            "digest will carry one.", writes=True, memo_errors=True, person=P("string", "a name, link or id"))
def recheck_person(ctx: ToolContext, person: str) -> dict[str, Any]:
    from tailwatch.desk.tools_core import resolve_person

    found, error = resolve_person(ctx.store, person)
    if error:
        return error
    assert found is not None
    return recheck(ctx.store, ctx.config, ctx.env, int(found["id"]), checkers=ctx.extra.get("checkers"),
                   budget=ctx.extra.get("budget"))


def recheck(store: Any, config: Any, env: Mapping[str, str], person_id: int, *, checkers: Mapping[str, Any] | None = None,
            budget: Any = None) -> dict[str, Any]:
    """Read every active source of one person now (whatever its due time) and judge what
    changed, as a pass would. Shared with the custodian's ``recheck_person`` playbook."""
    from tailwatch.pipeline import judge, runner

    person = store.person(person_id)
    if person is None:
        return {"error": f"no person with id {person_id}"}
    if checkers is None:
        from tailwatch.connectors.registry import enabled_checkers

        checkers = enabled_checkers(config, env)
    for kind, url in (person.get("links") or {}).items():
        checker = checkers.get(kind)
        if checker is not None:
            store.ensure_source(person_id, kind, url, cadence_hours=checker.cadence_hours)
    waiting = {c["id"] for c in store.pending_checks(1000) if c["person_id"] == person_id}
    checked, not_active, usd = [], [], 0.0
    for source in store.sources_of(person_id):
        if source["kind"].startswith("feed:"):
            continue
        checker = checkers.get(source["kind"])
        if source["status"] != "active" or checker is None:
            not_active.append({"kind": source["kind"], "status": source["status"] if checker else "no checker enabled"})
            continue
        outcome = runner.check_source(store, checker, source, runner.context_for(store, config, env, source["kind"]))
        usd += float(outcome.get("usd") or 0.0)
        checked.append({"kind": source["kind"], "status": outcome["status"], "changes": outcome["events"],
                        "error": util.clip(outcome.get("error"), 200)})
    if budget is not None and usd:
        budget.charge(usd)
    new = [c["id"] for c in store.pending_checks(1000) if c["person_id"] == person_id and c["id"] not in waiting]
    counts: dict[str, Any] = {}
    if new:
        counts = judge.judge_pending(store, config, env, checkers=checkers, wait=False, log=None)
    answers = []
    for observation_id in new:
        verdict = store.current_verdict(observation_id)
        if verdict:
            answers.append({"verdict_id": verdict["id"], "signal": verdict["signal"], "summary": verdict["summary"],
                            "confidence": verdict["confidence"]})
    out: dict[str, Any] = {"person": person["name"], "person_id": person_id, "checked": checked, "not_active": not_active,
                           "new_checks": len(new), "answers": answers, "usd": round(usd, 6),
                           "in_next_digest": any(a["signal"] != "none" for a in answers)}
    if counts.get("stopped"):
        out["judging"] = f"not judged now ({counts['stopped']}); the next scheduled pass judges them"
    return out


# -- a status change in two steps ---------------------------------------------------------------
def _thread(ctx: ToolContext) -> str:
    from tailwatch.desk import chat

    return chat.thread_key(ctx.channel, ctx.thread, ctx.user)


def pending_status(store: Any, key: str) -> dict[str, Any] | None:
    """The newest status proposal in a thread that was neither applied nor dropped."""
    settled: set[str] = set()
    for line in reversed(store.thread_lines(key)):
        if line.get("settled"):
            settled.add(str(line["settled"]))
        pending = line.get("pending")
        if isinstance(pending, dict) and pending.get("kind") == "status":
            return None if str(pending.get("token")) in settled else pending
    return None


@TOOLS.tool("propose_status", "Propose marking a person tracked, left or ignored. Writes nothing to the list: keeps the "
            "proposal in this thread and returns the question to ask. Ask it and stop.", mutating=True,
            person=P("string", "a name, link or id"), status=P("string", "the new status", enum=STATUSES),
            reason=P("string", "why, in the person's words"))
def propose_status(ctx: ToolContext, person: str, status: str, reason: str) -> dict[str, Any]:
    from tailwatch.desk.tools_core import resolve_person

    found, error = resolve_person(ctx.store, person)
    if error:
        return error
    assert found is not None
    if status not in STATUSES:
        return {"error": f"status is {', '.join(STATUSES)}"}
    reason = " ".join(str(reason or "").split())
    if not reason:
        return {"error": "a status change needs a reason"}
    if found["status"] == status:
        return {"proposed": False, "note": f"{found['name']} is already {status}"}
    token = str(ctx.extra.get("message_key") or util.iso())
    pending = {"kind": "status", "token": token, "person_id": found["id"], "name": found["name"], "status": status,
               "was": found["status"], "reason": util.clip(reason, 300), "by": ctx.user, "at": util.iso()}
    ask = (f"Mark {found['name']} (id {found['id']}, {found['organization'] or 'no organization on record'}) as {status}, "
           f"currently {found['status']}, because {pending['reason'].rstrip(' .!?;:')}? Reply yes in this thread to confirm.")
    ctx.store.add_thread_line(_thread(ctx), {"who": "bot", "text": ask, "pending": pending, "at": util.iso()})
    return {"proposed": True, "written_to_list": False, "ask": ask}


@TOOLS.tool("apply_status", "Apply the status change proposed in this thread. Only when the person's message is a clear yes "
            "to the pending proposal. person, when given, must be the one proposed.", writes=True,
            person=P("string", "the person, to check it is the one proposed", required=False))
def apply_status(ctx: ToolContext, person: str = "") -> dict[str, Any]:
    key = _thread(ctx)
    pending = pending_status(ctx.store, key)
    if pending is None:
        return {"applied": False, "note": "there is no pending status change in this thread"}
    if str(pending.get("token")) == str(ctx.extra.get("message_key") or ""):
        return {"error": "the person has not answered the proposal yet; ask and wait"}
    wanted = str(person or "").strip().lstrip("#").casefold()
    if wanted and wanted != str(pending["person_id"]) and wanted not in pending["name"].casefold():
        return {"error": f"the pending proposal is about {pending['name']}, not {person}"}
    if (pending.get("at") or "") < util.ago(hours=PENDING_HOURS):
        ctx.store.add_thread_line(key, {"who": "bot", "text": "(the proposal expired)", "settled": pending["token"], "at": util.iso()})
        return {"applied": False, "note": "the proposal is more than a day old and was dropped; propose it again"}
    ctx.store.set_status(int(pending["person_id"]), pending["status"], reason=pending["reason"], by=ctx.user or pending.get("by") or "")
    ctx.store.add_thread_line(key, {"who": "bot", "text": f"(applied: {pending['name']} is {pending['status']})",
                                    "settled": pending["token"], "at": util.iso()})
    return {"applied": True, "person_id": pending["person_id"], "person": pending["name"], "status": pending["status"],
            "was": pending["was"]}


@TOOLS.tool("label_card", "Record whether a card was right, wrong or unsure, as a person said it. The label feeds the "
            "scoring set; it changes no verdict.", writes=True, verdict_id=P("integer", "the card's verdict id"),
            label=P("string", "right, wrong or unsure", enum=LABELS),
            note=P("string", "the person's words", required=False))
def label_card(ctx: ToolContext, verdict_id: int, label: str, note: str = "") -> dict[str, Any]:
    verdict = ctx.store.verdict(int(verdict_id))
    if verdict is None:
        return {"error": f"no verdict with id {verdict_id}; why(person) gives the card's verdict id"}
    if label not in LABELS:
        return {"error": "label is right, wrong or unsure"}
    label_id = ctx.store.add_label(int(verdict_id), label, note=util.clip(note, 500), by=ctx.user or "channel")
    return {"label_id": label_id, "verdict_id": int(verdict_id), "label": label}
