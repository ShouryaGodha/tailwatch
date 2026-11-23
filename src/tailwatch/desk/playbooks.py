"""The playbooks: the only data fixes that run without a person. Each is idempotent, has a dry
run, and answers ``{changed, what}``; a refusal says ``refused`` (the issue closes as won't fix)
and a daily cap adds ``retry_later`` (the issue waits for another day, the attempt uncounted).

Caps, in code: three playbook runs a person a day; ``fill_links`` ten a day; a broken source
probed once in 30 days; a parked check unparked once per judge prompt version; ``clean_settings``
deletes only under its allow-list and never under the frozen deny-list, whatever it is asked.
Removing a person, a status change and a merge without proof are not playbooks: a person
decides those.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Mapping
from datetime import timedelta
from typing import Any

from tailwatch import util

PER_PERSON_PER_DAY = 3
FILL_LINKS_PER_DAY = 10
PROBE_COOLDOWN_DAYS = 30
CLEAN_AFTER_DAYS = 90
CLEAN_MIN_DAYS = 30
COUNT_KEY = "CUSTODIAN:COUNT:"
PROBED_KEY = "CUSTODIAN:PROBED:"
UNPARKED_KEY = "CUSTODIAN:UNPARKED:"
# Per-day and per-message keys that only grow. A module that writes such keys puts them under
# ``DAY:`` and the custodian cleans them after 90 days.
CLEAN_ALLOW = (COUNT_KEY, PROBED_KEY, "DAY:")
# Never deleted, whoever asks: switches, the team list, learned rules, floors, markers.
CLEAN_DENY = ("DESK:", UNPARKED_KEY, "CUSTODIAN:SCAN:", "RULES", "FLOORS", "ALIASES", "WATCH", "JOB")
FROZEN_SUFFIXES = (":RULES", ":FLOORS", ":ALIASES")


class PlaybookError(ValueError):
    pass


def _day(config: Any) -> str:
    return util.local_day(config.schedule.timezone if config is not None else "UTC")


def _count(store: Any, key: str) -> int:
    try:
        return int(store.get_setting(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _bump(store: Any, key: str) -> None:
    store.set_setting(key, _count(store, key) + 1, history=False)


def denied(key: str) -> bool:
    return any(key.startswith(d) or d in key for d in CLEAN_DENY) or key.endswith(FROZEN_SUFFIXES)


def allowed(key: str) -> bool:
    return any(key.startswith(a) for a in CLEAN_ALLOW) and not denied(key)


def person_of(store: Any, name: str, args: Mapping[str, Any]) -> int | None:
    """The person a playbook run is about, for the per-person cap."""
    if args.get("person_id") not in (None, ""):
        return int(args["person_id"])
    if args.get("source_id"):
        source = store.source(int(args["source_id"]))
        return int(source["person_id"]) if source else None
    if args.get("review_id"):
        review = store.review(int(args["review_id"]))
        return int(review["person_id"]) if review else None
    if args.get("keep_id"):
        return int(args["keep_id"])
    return None


def run(name: str, store: Any, config: Any, env: Mapping[str, str], args: Mapping[str, Any], *, dry_run: bool = False,
        checkers: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """One playbook by name. Raises PlaybookError for an unknown name or a missing subject."""
    if name not in KNOWN:
        raise PlaybookError(f"no playbook named {name}; one of {', '.join(sorted(KNOWN))}")
    args = dict(args or {})
    person_id = person_of(store, name, args) if name != "clean_settings" else None
    count_key = f"{COUNT_KEY}person:{person_id}:{_day(config)}"
    if person_id is not None and _count(store, count_key) >= PER_PERSON_PER_DAY:
        return {"changed": False, "refused": f"person {person_id} had {PER_PERSON_PER_DAY} playbook runs today",
                "retry_later": True}
    try:
        result = KNOWN[name](store, config, env, args, dry_run=dry_run, checkers=checkers)
    except KeyError as error:
        raise PlaybookError(f"{name} needs {error}") from error
    if not dry_run and person_id is not None and not result.get("refused"):
        _bump(store, count_key)
    return result


def checkers_for(config: Any, env: Mapping[str, str], given: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if given is not None:
        return given
    from tailwatch.connectors.registry import enabled_checkers

    return enabled_checkers(config, env)


def reopen_source(store: Any, config: Any, env: Mapping[str, str], args: dict[str, Any], *, dry_run: bool,
                  checkers: Mapping[str, Any] | None) -> dict[str, Any]:
    """A broken or private source probed once with its real checker; a good read makes it active again."""
    from tailwatch.pipeline import runner

    source = store.source(int(args["source_id"]))
    if source is None:
        raise PlaybookError(f"no source {args['source_id']}")
    if source["status"] not in ("broken", "private"):
        return {"changed": False, "what": f"source {source['id']} is {source['status']}, not broken"}
    key = f"{PROBED_KEY}{source['id']}"
    last = util.parse(store.get_setting(key, ""))
    if last is not None and util.now() - last < timedelta(days=PROBE_COOLDOWN_DAYS):
        return {"changed": False, "refused": f"source {source['id']} was probed on {util.iso(last)[:10]}; "
                                             f"once in {PROBE_COOLDOWN_DAYS} days"}
    checker = checkers_for(config, env, checkers).get(source["kind"])
    if checker is None:
        return {"changed": False, "refused": f"no {source['kind']} checker is enabled to probe with"}
    if dry_run:
        return {"changed": True, "dry_run": True, "what": f"would probe source {source['id']} ({source['kind']}) once"}
    previous = store.last_ok_observation(source["id"])
    failure = ""
    try:
        read = checker.read(source["url"], previous["content"] if previous else None,
                            runner.context_for(store, config, env, source["kind"]))
    except Exception as error:
        read = None
        failure = f"{type(error).__name__}: {util.clip(error, 150)}"
    store.set_setting(key, util.iso(), history=False)
    if read is None or read.status != "ok":
        why = failure if read is None else f"{read.status} {util.clip(read.error, 150)}"
        return {"changed": False, "what": f"source {source['id']} still answers {why}"}
    store.reopen_source(source["id"], f"reopened by the custodian on {util.iso()[:10]}: the page answers again")
    return {"changed": True, "what": f"source {source['id']} ({source['kind']}) is active again; the next pass reads it"}


def mark_wrong_person(store: Any, config: Any, env: Mapping[str, str], args: dict[str, Any], *, dry_run: bool,
                      checkers: Mapping[str, Any] | None) -> dict[str, Any]:
    """A source shown to be someone else's: marked wrong_person, and an issue to find the right link."""
    source = store.source(int(args["source_id"]))
    if source is None:
        raise PlaybookError(f"no source {args['source_id']}")
    if source["status"] == "wrong_person":
        return {"changed": False, "what": f"source {source['id']} is already marked wrong person"}
    if dry_run:
        return {"changed": True, "dry_run": True, "what": f"would mark source {source['id']} wrong person"}
    reason = util.clip(args.get("reason") or "the profile is someone else's", 300)
    store.set_source_status(source["id"], "wrong_person", f"custodian: {reason}")
    issue_id, _ = store.open_issue("wrong_link", f"source:{source['id']}", playbook="fill_links",
                                   args={"person_id": source["person_id"], "kind": source["kind"]}, found_by="custodian",
                                   note=f"the {source['kind']} source was someone else's; find the right one")
    return {"changed": True, "what": f"source {source['id']} ({source['kind']}) marked wrong person", "issue": f"#I{issue_id}"}


def fill_links(store: Any, config: Any, env: Mapping[str, str], args: dict[str, Any], *, dry_run: bool,
               checkers: Mapping[str, Any] | None) -> dict[str, Any]:
    """The enricher on one tracked person, in fill_links mode; ten a day."""
    key = f"{COUNT_KEY}fill_links:{_day(config)}"
    if _count(store, key) >= FILL_LINKS_PER_DAY:
        return {"changed": False, "refused": f"{FILL_LINKS_PER_DAY} link enrichments ran today", "retry_later": True,
                "cap": "playbook"}
    person = store.person(int(args["person_id"]))
    if person is None:
        raise PlaybookError(f"no person {args['person_id']}")
    if person["status"] != "tracked":
        return {"changed": False, "what": f"{person['name']} is {person['status']}, not tracked"}
    if dry_run:
        return {"changed": True, "dry_run": True, "what": f"would look for links of {person['name']}"}
    try:
        agent = importlib.import_module("tailwatch.enrich.agent")
    except ImportError:
        return {"changed": False, "refused": "the enricher is not installed", "retry_later": True, "cap": "playbook"}
    _bump(store, key)
    result = agent.enrich_person(store, config, env, person_id=person["id"], mode="fill_links") or {}
    added = result.get("links_added", result.get("added", 0)) if isinstance(result, Mapping) else 0
    count = len(added) if isinstance(added, (list, dict)) else int(added or 0)
    if isinstance(result, Mapping) and result.get("error"):
        return {"changed": False, "error": util.clip(result["error"], 300)}
    return {"changed": count > 0, "what": f"{count} link(s) added for {person['name']}",
            "result": {k: v for k, v in result.items() if k != "trace"} if isinstance(result, Mapping) else {}}


def merge_people(store: Any, config: Any, env: Mapping[str, str], args: dict[str, Any], *, dry_run: bool,
                 checkers: Mapping[str, Any] | None) -> dict[str, Any]:
    """Two rows shown to be one person (both hold the same account key): merged under the
    judge's lock, keeping the row that holds LinkedIn, else the older id."""
    from tailwatch.store import LockBusy
    from tailwatch.store.custodian import IDENTITY_KINDS

    keep, drop, proof = int(args["keep_id"]), int(args["drop_id"]), str(args.get("proof") or "")
    if proof.split(":", 1)[0] not in IDENTITY_KINDS:
        return {"changed": False, "refused": "a merge needs proof: one account key (linkedin, github, x ...) both rows hold"}
    rows = {r["id"]: r for r in (store.person(keep), store.person(drop)) if r}
    if keep not in rows or drop not in rows:
        raise PlaybookError("both people must exist")
    if rows[keep]["status"] != "tracked" or rows[drop]["status"] != "tracked":
        return {"changed": False, "what": "one of the two is no longer tracked"}
    if proof not in store.link_keys_of(keep) or proof not in store.link_keys_of(drop):
        return {"changed": False, "refused": f"{proof} is not held by both #{keep} and #{drop}"}
    if not rows[keep]["links"].get("linkedin") and rows[drop]["links"].get("linkedin"):
        keep, drop = drop, keep
    if dry_run:
        return {"changed": True, "dry_run": True, "what": f"would merge #{drop} into #{keep} ({proof})"}
    try:
        with store.lock("judge", ttl_s=600, wait_s=0):
            store.merge_people(keep, drop, by="custodian")
    except LockBusy:
        return {"changed": False, "refused": "the judge is running; the merge waits", "retry_later": True}
    return {"changed": True, "what": f"merged #{drop} ({rows[drop]['name']}) into #{keep} ({rows[keep]['name']}): both hold {proof}"}


def unpark_event(store: Any, config: Any, env: Mapping[str, str], args: dict[str, Any], *, dry_run: bool,
                 checkers: Mapping[str, Any] | None) -> dict[str, Any]:
    """A check the judge failed three times gets one more try per judge prompt version."""
    observation_id = int(args["observation_id"])
    observation = store.observation(observation_id)
    if observation is None:
        raise PlaybookError(f"no check {observation_id}")
    if observation["judge_state"] != "parked":
        return {"changed": False, "what": f"check {observation_id} is {observation['judge_state']}, not parked"}
    version = judge_prompt_version(config)
    key = f"{UNPARKED_KEY}{observation_id}"
    if store.get_setting(key) == version:
        return {"changed": False, "refused": f"check {observation_id} was unparked once under {version}"}
    if dry_run:
        return {"changed": True, "dry_run": True, "what": f"would give check {observation_id} one more try"}
    store.unpark(observation_id)
    store.set_setting(key, version, history=False)
    return {"changed": True, "what": f"check {observation_id} waits for the judge again (prompt {version})"}


def judge_prompt_version(config: Any) -> str:
    from tailwatch.pipeline import packs

    return packs.load(config.signals if config is not None else "career-moves").prompt_version


def clean_settings(store: Any, config: Any, env: Mapping[str, str], args: dict[str, Any], *, dry_run: bool,
                   checkers: Mapping[str, Any] | None) -> dict[str, Any]:
    """Per-day and per-message keys older than 90 days under the allow-list; the deny-list wins."""
    days = int(args.get("days") or CLEAN_AFTER_DAYS)
    if days < CLEAN_MIN_DAYS:
        return {"changed": False, "refused": f"keys younger than {CLEAN_MIN_DAYS} days are never cleaned"}
    prefix = str(args.get("prefix") or "")
    prefixes = [prefix] if prefix else list(CLEAN_ALLOW)
    refused = [p for p in prefixes if not allowed(p)]
    keys = stale_keys(store, [p for p in prefixes if p not in refused], days=days)
    if not dry_run:
        for key in keys:
            store.delete_setting(key)
    return {"changed": bool(keys) and not dry_run, "dry_run": dry_run,
            "what": f"{'would delete' if dry_run else 'deleted'} {len(keys)} key(s) older than {days} days",
            **({"refused_prefixes": refused} if refused else {})}


def stale_keys(store: Any, prefixes: list[str], *, days: int = CLEAN_AFTER_DAYS) -> list[str]:
    before = util.ago(days=days)
    out: list[str] = []
    for prefix in prefixes:
        out.extend(r["key"] for r in store.settings_like(prefix) if r["updated_at"] < before and allowed(r["key"]))
    return sorted(set(out))


def recheck_person(store: Any, config: Any, env: Mapping[str, str], args: dict[str, Any], *, dry_run: bool,
                   checkers: Mapping[str, Any] | None) -> dict[str, Any]:
    """The channel's recheck: every active source of the person now, judged."""
    from tailwatch.desk.tools_channel import recheck

    person_id = int(args["person_id"])
    if store.person(person_id) is None:
        raise PlaybookError(f"no person {person_id}")
    if dry_run:
        return {"changed": True, "dry_run": True, "what": f"would recheck person {person_id}"}
    result = recheck(store, config, env, person_id, checkers=checkers)
    if result.get("error"):
        return {"changed": False, "error": result["error"]}
    return {"changed": bool(result["new_checks"]), "what": f"rechecked {result['person']}: {result['new_checks']} new check(s)",
            "result": {k: result[k] for k in ("checked", "answers", "new_checks")}}


def fix_card_fields(store: Any, config: Any, env: Mapping[str, str], args: dict[str, Any], *, dry_run: bool,
                    checkers: Mapping[str, Any] | None) -> dict[str, Any]:
    """A card right in substance with a wrong organization or title: the roster line is corrected
    only when the quoted words are in the latest read of the card's source."""
    review = store.review(int(args["review_id"]))
    if review is None:
        raise PlaybookError(f"no review {args['review_id']}")
    organization, title, quote = (" ".join(str(args.get(k) or "").split()) for k in ("organization", "title", "quote"))
    if not (organization or title):
        return {"changed": False, "refused": "no organization or title to set"}
    if not quote:
        return {"changed": False, "refused": "a verbatim quote from the read is required"}
    observation = store.observation(int(review["observation_id"]))
    latest = store.last_ok_observation(observation["source_id"]) if observation else None
    if latest is None or not quote_in(quote, text_of(latest["content"])):
        return {"changed": False, "refused": "the quote is not in the latest read of the card's source"}
    person = store.person(int(review["person_id"]))
    if person is None:
        raise PlaybookError(f"no person {review['person_id']}")
    new_org, new_title = organization or person["organization"], title or person["title"]
    if (new_org, new_title) == (person["organization"], person["title"]):
        return {"changed": False, "what": "the roster already says that"}
    what = (f"{person['name']}: {person['title'] or '?'} at {person['organization'] or '?'} becomes "
            f"{new_title or '?'} at {new_org or '?'}")
    if dry_run:
        return {"changed": True, "dry_run": True, "what": f"would set {what}"}
    store.update_person(person["id"], organization=new_org, title=new_title, by="custodian")
    return {"changed": True, "what": what}


def text_of(content: Any) -> str:
    """Every string in a read, joined: what a quote is looked for in."""
    if isinstance(content, Mapping):
        return " ".join(text_of(v) for v in content.values())
    if isinstance(content, (list, tuple)):
        return " ".join(text_of(v) for v in content)
    return "" if content is None else str(content)


def quote_in(quote: str, text: str) -> bool:
    def norm(value: str) -> str:
        return " ".join(value.casefold().replace("’", "'").split())

    return bool(norm(quote)) and norm(quote) in norm(text)


Playbook = Callable[..., dict[str, Any]]
KNOWN: dict[str, Playbook] = {
    "reopen_source": reopen_source, "mark_wrong_person": mark_wrong_person, "fill_links": fill_links,
    "merge_people": merge_people, "unpark_event": unpark_event, "clean_settings": clean_settings,
    "recheck_person": recheck_person, "fix_card_fields": fix_card_fields,
}


def describe() -> dict[str, str]:
    return {name: (fn.__doc__ or "").strip().split("\n")[0] for name, fn in sorted(KNOWN.items())}
