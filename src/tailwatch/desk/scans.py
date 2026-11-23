"""The scans: what the custodian looks for on a rota. Each scan returns the issues it found
(kind, subject, the playbook that fixes it when one is allowed, its arguments, a note) and
``run`` writes them once per (kind, subject) through ``store.open_issue``, which keeps the
30-day cooldown before a closed issue is opened again.

A scan runs at most once per its cooldown. More than ``FLOOD`` findings at once is a bug, not
data: nothing of that scan is written and the result says so.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import timedelta
from typing import Any

from tailwatch import util
from tailwatch.desk import playbooks

SCAN_KEY = "CUSTODIAN:SCAN:"
FLOOD = 200
CANDIDATE_WAIT_DAYS = 7
SETTINGS_GROWTH_MIN = 100
COOLDOWN_HOURS = {"broken_sources": 24, "no_active_source": 6, "shared_links": 6, "parked_checks": 6,
                  "settings_growth": 24, "candidates_waiting": 24}
KINDS = tuple(COOLDOWN_HOURS)

Finding = dict[str, Any]


def due(store: Any, kind: str) -> bool:
    last = util.parse(store.get_setting(f"{SCAN_KEY}{kind}", ""))
    return last is None or util.now() - last >= timedelta(hours=COOLDOWN_HOURS[kind])


def mark(store: Any, kind: str) -> None:
    store.set_setting(f"{SCAN_KEY}{kind}", util.iso(), history=False)


def broken_sources(store: Any, config: Any, env: Mapping[str, str], checkers: Mapping[str, Any]) -> list[Finding]:
    """Broken or private sources of a kind a checker can probe, not probed in 30 days."""
    out = []
    for source in store.sources_down(sorted(checkers), limit=FLOOD + 1):
        last = util.parse(store.get_setting(f"{playbooks.PROBED_KEY}{source['id']}", ""))
        if last is not None and util.now() - last < timedelta(days=playbooks.PROBE_COOLDOWN_DAYS):
            continue
        out.append({"kind": "broken_source", "subject_key": f"source:{source['id']}", "playbook": "reopen_source",
                    "args": {"source_id": source["id"], "person_id": source["person_id"]},
                    "note": f"the {source['kind']} source of {source['person_name']} is {source['status']}: probe it once"})
    return out


def no_active_source(store: Any, config: Any, env: Mapping[str, str], checkers: Mapping[str, Any]) -> list[Finding]:
    out = []
    for person in store.people_without_active_source(limit=FLOOD + 1):
        links = ", ".join(sorted(person["links"])) or "no link"
        out.append({"kind": "no_active_source", "subject_key": f"person:{person['id']}", "playbook": "fill_links",
                    "args": {"person_id": person["id"]},
                    "note": f"{person['name']} ({person['organization'] or '?'}) is tracked with no active source ({links})"})
    return out


def shared_links(store: Any, config: Any, env: Mapping[str, str], checkers: Mapping[str, Any]) -> list[Finding]:
    """Two tracked people holding one link key. An account key is proof enough for the merge
    playbook; a shared page (a lab's homepage) is not, and a person decides."""
    out = []
    for pair in store.people_sharing_links():
        note = (f"{pair['keep_name']} (#{pair['keep_id']}) and {pair['drop_name']} (#{pair['drop_id']}) both hold "
                f"{pair['proof']}")
        out.append({"kind": "shared_link", "subject_key": f"people:{pair['keep_id']}+{pair['drop_id']}",
                    "playbook": "merge_people" if pair["identity"] else "",
                    "args": {"keep_id": pair["keep_id"], "drop_id": pair["drop_id"], "proof": pair["proof"],
                             "person_id": pair["keep_id"]},
                    "note": note + ("" if pair["identity"] else "; not an account, so a person decides")})
    return out


def parked_checks(store: Any, config: Any, env: Mapping[str, str], checkers: Mapping[str, Any]) -> list[Finding]:
    version = playbooks.judge_prompt_version(config)
    out = []
    for check in store.parked_checks(limit=FLOOD + 1):
        if store.get_setting(f"{playbooks.UNPARKED_KEY}{check['id']}") == version:
            continue
        out.append({"kind": "parked_check", "subject_key": f"observation:{check['id']}", "playbook": "unpark_event",
                    "args": {"observation_id": check["id"], "person_id": check["person_id"]},
                    "note": f"a check of {check['person_name']} failed judging {check['judge_attempts']} times: "
                            f"{util.clip(check['judge_error'], 160)}"})
    return out


def settings_growth(store: Any, config: Any, env: Mapping[str, str], checkers: Mapping[str, Any]) -> list[Finding]:
    stale = playbooks.stale_keys(store, list(playbooks.CLEAN_ALLOW))
    if len(stale) < SETTINGS_GROWTH_MIN:
        return []
    return [{"kind": "settings_growth", "subject_key": "settings:per-day-keys", "playbook": "clean_settings",
             "args": {"days": playbooks.CLEAN_AFTER_DAYS},
             "note": f"{len(stale)} per-day keys are older than {playbooks.CLEAN_AFTER_DAYS} days"}]


def candidates_waiting(store: Any, config: Any, env: Mapping[str, str], checkers: Mapping[str, Any]) -> list[Finding]:
    waiting = store.candidates_waiting(before=util.ago(days=CANDIDATE_WAIT_DAYS))
    if not waiting:
        return []
    ids = ", ".join(f"#C{c['id']}" for c in waiting[:10])
    return [{"kind": "candidates_waiting", "subject_key": "candidates:needs_review", "playbook": "", "args": {},
             "note": f"{len(waiting)} report(s) have waited for review over {CANDIDATE_WAIT_DAYS} days: {ids}"}]


SCANS: dict[str, Callable[..., list[Finding]]] = {
    "broken_sources": broken_sources, "no_active_source": no_active_source, "shared_links": shared_links,
    "parked_checks": parked_checks, "settings_growth": settings_growth, "candidates_waiting": candidates_waiting,
}


def run(store: Any, config: Any, env: Mapping[str, str], kinds: tuple[str, ...] | None = None, *, force: bool = False,
        dry_run: bool = False, checkers: Mapping[str, Any] | None = None, log: Any = None) -> dict[str, Any]:
    """The due scans (or ``kinds``; ``force`` ignores the rota), their findings written as issues."""
    out: dict[str, Any] = {"ran": [], "found": {}, "opened": {}, "flood": [], "failed": {}}
    probe = playbooks.checkers_for(config, env, checkers)
    for kind in kinds or KINDS:
        if kind not in SCANS:
            raise ValueError(f"no scan named {kind}; one of {', '.join(KINDS)}")
        if not force and not due(store, kind):
            continue
        try:
            found = SCANS[kind](store, config, env, probe)
        except Exception as error:  # one scan's failure never stops the rota
            out["failed"][kind] = f"{type(error).__name__}: {util.clip(error, 200)}"
            if log:
                log(f"scan {kind} failed: {out['failed'][kind]}")
            continue
        out["ran"].append(kind)
        out["found"][kind] = len(found)
        if dry_run:
            out.setdefault("would_open", {})[kind] = found[:20]
            continue
        mark(store, kind)
        if len(found) > FLOOD:
            out["flood"].append(kind)
            if log:
                log(f"scan {kind}: {len(found)} findings at once, more than {FLOOD}; nothing written (a flood is a bug)")
            continue
        opened = 0
        for finding in found:
            try:
                _issue_id, created = store.open_issue(finding["kind"], finding["subject_key"], playbook=finding["playbook"],
                                                      args=finding["args"], found_by=f"scan:{kind}", note=finding["note"])
            except RuntimeError as error:  # the store's own flood guard: too many issues open already
                out["flood"].append(f"{kind}: {error}")
                break
            opened += 1 if created else 0
        out["opened"][kind] = opened
    return out
