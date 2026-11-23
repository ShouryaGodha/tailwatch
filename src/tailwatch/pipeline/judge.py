"""The judge: one strict model call per check.

A check is one read of one source and the events it produced. The judge reads the events
together with everything held on the person (the newest read of each source, the last two weeks
of changes with the answers already given, the person's earlier signals, the team's intel) and
answers with one of the pack's signals or none. Nothing in code decides what a change means;
code only:

- records a check made only of the pack's gate labels (a flag alone) as none, with no model call;
- turns a signal dated longer ago than the pack allows into none with the flag ``old_move``;
- refuses ids the person's history does not hold (one retry, then the unknown ids are dropped);
- marks a source ``wrong_person`` when the judge says the profile is someone else's;
- lets the roster follow a judged profile read (``roster_kinds``), never back to an organization
  a trusted report said was left;
- parks a check the model could not answer three times, and stops after three provider failures
  in a row so the rest waits for the next pass.

The judge runs under a lock, so passes and watchers that finish together take turns.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from tailwatch import util
from tailwatch.agent import loop, routines
from tailwatch.agent.types import ModelError
from tailwatch.pipeline import packs
from tailwatch.store import LockBusy

MAX_ATTEMPTS = 3
PROVIDER_FAILURE_STOP = 3
RECENT_DAYS = 14
PRIOR_DAYS = 365
LOCK_WAIT_S = 20 * 60
ROSTER_KINDS = ("linkedin",)
READ_TEXT_CAP = 6000


def is_feed(kind: str) -> bool:
    return kind.startswith("feed:")


def trusted_kind(kind: str, config: Any) -> bool:
    if not is_feed(kind) or config is None:
        return False
    return kind.removeprefix("feed:") in set(config.priority.trusted_feeds)


def describe(kind: str, content: Mapping[str, Any], checkers: Mapping[str, Any]) -> str:
    checker = checkers.get(kind)
    if checker is not None:
        try:
            return util.clip(checker.describe(content), READ_TEXT_CAP)
        except Exception:
            pass
    return util.clip(util.dumps(dict(content)), READ_TEXT_CAP)


def judge_input(store: Any, check: Mapping[str, Any], config: Any, checkers: Mapping[str, Any]) -> dict[str, Any]:
    person_id = int(check["person_id"])
    person = store.person(person_id) or {}
    snapshot = check.get("person_snapshot") or {}
    previous = store.last_ok_observation(check["source_id"], before_id=check["id"])
    reads = []
    for read in store.latest_reads(person_id):
        if is_feed(read["kind"]):
            continue
        reads.append({"source": read["kind"], "url": read["url"], "read_at": read["observed_at"],
                      "text": describe(read["kind"], read["content"], checkers)})
    since_recent = util.ago(days=RECENT_DAYS)
    recent = [{"seen": e["observed_at"][:10], "source": e["kind"], "change": e["label"],
               "after": util.clip(e["after_text"], 300), "answer": e.get("signal") or "not judged yet",
               "summary": e.get("summary") or ""}
              for e in store.recent_events(person_id, since=since_recent, exclude_observation=check["id"])]
    prior = [{"id": v["id"], "signal": v["signal"], "summary": v["summary"], "now": v["now"], "seen": v["created_at"][:10],
              "delivered": bool(v.get("posted")), "source": v["kind"], "trusted": trusted_kind(v["kind"], config),
              "judged_by": v["judged_by"]}
             for v in store.prior_alerts(person_id, since=util.ago(days=PRIOR_DAYS))]
    intel = [{"id": i["id"], "said": i["text"], "by": i["said_by"], "on": i["created_at"][:10]} for i in store.intel_for(person)]
    return {
        "person": {"id": person_id, "name": snapshot.get("name") or person.get("name"),
                   "organization": snapshot.get("organization") or person.get("organization"),
                   "title": snapshot.get("title") or person.get("title"), "tags": person.get("tags") or []},
        "intel": intel,
        "change": {"source": check["kind"], "url": check.get("url"), "seen": check["observed_at"],
                   "first_read": bool(check.get("first_read")), "trusted_report": trusted_kind(check["kind"], config),
                   "previous_read": describe(check["kind"], previous["content"], checkers) if previous else "",
                   "changes": [{"label": e["label"], "before": e["before_text"], "after": e["after_text"],
                                "content_date": e["content_date"]} for e in check["events"]]},
        "reads": reads,
        "recent": recent,
        "prior_alerts": prior,
        "today": util.iso()[:10],
    }


def gated(check: Mapping[str, Any], pack: packs.Pack) -> bool:
    labels = {e["label"] for e in check.get("events") or []}
    return bool(labels) and bool(pack.gate_labels) and labels <= set(pack.gate_labels)


def old_signal(answer: Mapping[str, Any], pack: packs.Pack) -> bool:
    signal = pack.signal(str(answer.get("signal") or ""))
    if signal is None or not signal.old_after_days:
        return False
    date = str(answer.get("signal_date") or "")
    if len(date) == 7:
        date += "-28"
    parsed = util.parse(date + "T00:00:00Z") if len(date) == 10 else None
    return parsed is not None and parsed < util.now() - timedelta(days=signal.old_after_days)


def unknown_ids(answer: Mapping[str, Any], prior_ids: set[int]) -> list[int]:
    ids = [int(answer.get("same_as") or 0), *[int(i) for i in answer.get("supersedes") or []]]
    return [i for i in ids if i and i not in prior_ids]


def ask(store: Any, config: Any, env: Mapping[str, str], pack: packs.Pack, seed: dict[str, Any], subject: str) -> tuple[dict[str, Any], str]:
    spec = routines.spec_for("judge", config, instructions=packs.judge_instructions(pack, config),
                             schema=packs.judge_schema(pack), final_call=False, max_tokens=4000)
    prior_ids = {int(p["id"]) for p in seed["prior_alerts"]}
    run = routines.run_routine(store, spec, seed, subject=subject, env=env)
    answer = dict(run.answer or {})
    bad = unknown_ids(answer, prior_ids)
    if bad:
        retry = dict(seed, note=f"ids {bad} are not in prior_alerts; use only ids listed there, or 0 and []")
        run = routines.run_routine(store, spec, retry, subject=subject, env=env)
        answer = dict(run.answer or {})
        if int(answer.get("same_as") or 0) not in prior_ids:
            answer["same_as"] = 0
        answer["supersedes"] = [i for i in answer.get("supersedes") or [] if int(i) in prior_ids]
    return answer, run.model


def roster_may_follow(store: Any, person_id: int, answer: Mapping[str, Any], config: Any) -> bool:
    """A judged profile read moves the roster, except back to an organization a trusted report
    (within 60 days) said the person left."""
    now_org = str((answer.get("now") or {}).get("organization") or "").strip()
    if not now_org:
        return False
    for alert in store.prior_alerts(person_id, since=util.ago(days=60)):
        if trusted_kind(alert["kind"], config) and alert["signal"] != "none":
            reported = str((alert["now"] or {}).get("organization") or "").strip().lower()
            if reported and reported != now_org.lower():
                return False
    return True


def save(store: Any, check: Mapping[str, Any], answer: dict[str, Any], *, pack: packs.Pack, config: Any, model: str,
         judged_by: str = "model") -> int:
    person_id = int(check["person_id"])
    flags = list(answer.get("flags") or [])
    if old_signal(answer, pack):
        answer = {**answer, "signal": "none", "eligible": False}
        flags.append("old_move")
    if answer.get("wrong_person"):
        answer = {**answer, "signal": "none"}
        if not is_feed(check["kind"]):
            store.set_source_status(check["source_id"], "wrong_person", util.clip(answer.get("reason"), 300))
            store.open_issue("wrong_link", f"source:{check['source_id']}", playbook="fill_links",
                             args={"person_id": person_id, "kind": check["kind"]}, found_by="judge",
                             note="the judge read this profile as someone else's")
    answer["flags"] = sorted(set(flags))
    verdict_id = store.save_verdict(check["id"], person_id, answer, judged_by=judged_by, model=model,
                                    prompt_version=pack.prompt_version,
                                    guidance_hash=util.sha(util.dumps([i["id"] for i in store.intel_for(store.person(person_id) or {})]))[:12])
    now = answer.get("now") or {}
    if judged_by == "model" and check["kind"] in ROSTER_KINDS and roster_may_follow(store, person_id, answer, config):
        store.update_person(person_id, organization=now.get("organization") or "", title=now.get("title") or "",
                            since=now.get("since") or "")
    if judged_by == "model" and trusted_kind(check["kind"], config) and answer.get("signal") != "none" and now.get("organization"):
        store.update_person(person_id, organization=now["organization"], title=now.get("title") or "", since=now.get("since") or "")
    return verdict_id


def judge_pending(store: Any, config: Any, env: Mapping[str, str], *, checkers: Mapping[str, Any] | None = None,
                  limit: int = 200, wait: bool = True, log: Any = print) -> dict[str, Any]:
    """Judge every waiting check, oldest first. Returns counts."""
    from tailwatch.connectors.registry import checker_classes

    pack = packs.load(config.signals)
    checkers = checkers if checkers is not None else {k: cls() for k, cls in checker_classes().items()}
    counts = {"judged": 0, "gated": 0, "signals": 0, "parked": 0, "failed": 0, "stopped": ""}
    try:
        with store.lock("judge", ttl_s=3600, wait_s=LOCK_WAIT_S if wait else 0):
            provider_failures = 0
            for check in store.pending_checks(limit):
                if gated(check, pack):
                    store.save_verdict(check["id"], check["person_id"], {"signal": "none", "reason": "a flag alone is not a signal",
                                                                         "flags": ["flag_only"], "confidence": 1.0},
                                       judged_by="gate", prompt_version=pack.prompt_version)
                    counts["gated"] += 1
                    continue
                seed = judge_input(store, check, config, checkers)
                try:
                    answer, model = ask(store, config, env, pack, seed, subject=f"check:{check['id']}")
                except (loop.LoopError, ModelError) as error:
                    cause = error.__cause__ if isinstance(error.__cause__, ModelError) else error
                    if isinstance(cause, ModelError) and (cause.outage or cause.retryable or cause.status in (401, 403, 429)):
                        provider_failures += 1
                        counts["failed"] += 1
                        if provider_failures >= PROVIDER_FAILURE_STOP:
                            counts["stopped"] = f"the model provider failed {provider_failures} times in a row: {cause}"
                            break
                        continue
                    provider_failures = 0
                    if store.judge_failed(check["id"], str(error), park_after=MAX_ATTEMPTS):
                        counts["parked"] += 1
                    else:
                        counts["failed"] += 1
                    continue
                provider_failures = 0
                save(store, check, answer, pack=pack, config=config, model=model)
                counts["judged"] += 1
                if answer.get("signal") != "none":
                    counts["signals"] += 1
    except LockBusy:
        counts["stopped"] = "another run held the judge"
    if counts["stopped"] and log:
        log(f"judge stopped: {counts['stopped']}")
    return counts
