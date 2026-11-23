"""Candidates: reports about people who are not on the list.

``watch_feeds`` keeps an item about nobody on the list whole, as a candidate row. Every tick,
``run`` takes the waiting rows through three steps:

1. **Identify.** One tool-less call (``identify_mention``, ``prompts/mention_subject.md``)
   names the subject, the handles in the post that are theirs, the organization left and
   joined, and ``others``: a round-up of several people becomes one candidate per person on the
   same item (``<item>#n``), identified already so the model is not asked twice.
2. **Filter.** Only reports touching an organization in ``priority.tiers`` go on (any tier;
   with no tiers configured everything passes); the rest are ``out_of_scope`` and still show in
   the digest's "seen elsewhere" line. A role in ``ENRICHMENT:OUT_OF_SCOPE_ROLES`` (empty by
   default) is dismissed before anything is paid for. Someone already on the list (a handle
   they hold, or their name with the organization) gets the report as an event instead, and a
   duplicate report of an open candidate is dismissed.
3. **Enrich and decide.** The enricher looks for proof. A verified profile link of a kind an
   enabled checker reads (LinkedIn first when on) adds the person (``added_because =
   "report: <feed> <item>"``) and records the report as their first event, so the judge reads
   it in the same tick. Anything less waits as ``needs_review`` for a person (``decide``).

Nobody is added twice, nobody removed comes back, and the money is capped: per person
(``budgets.enrich_per_person``) inside the enricher, per day (``budgets.enrich_daily``) here,
summed from the run log. When the day's cap is spent the job stops and says so once.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from tailwatch import links as link_rules
from tailwatch import util
from tailwatch.agent import loop, prompts, routines
from tailwatch.agent.types import ModelError
from tailwatch.connectors.base import FeedItem
from tailwatch.connectors.http import Http
from tailwatch.enrich import agent, names
from tailwatch.enrich import settings as tunables
from tailwatch.enrich.tools import Toolkit

ROUTINES = ("enrich_person", "identify_mention")
MAX_ATTEMPTS = 3
MAX_OTHERS = 8
PROVIDER_FAILURE_STOP = 3
DUPLICATE_DAYS = 30
CAP_TOPIC = "enrichment:daily_cap"
OPEN_STATES = ("identified", "enriching", "needs_review", "added")
PERSON_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["name", "handles", "left", "joined", "role"],
    "properties": {"name": {"type": "string"}, "handles": {"type": "array", "items": {"type": "string"}},
                   "left": {"type": "string"}, "joined": {"type": "string"}, "role": {"type": "string"}},
}
IDENTIFY_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["is_report", "name", "handles", "left", "joined", "role", "others", "reason"],
    "properties": {**PERSON_SCHEMA["properties"], "is_report": {"type": "boolean"},
                   "others": {"type": "array", "items": PERSON_SCHEMA}, "reason": {"type": "string"}},
}


def label(candidate_id: int) -> str:
    return f"#C{candidate_id}"


# -- identify ---------------------------------------------------------------------------------

def _identity(raw: Mapping[str, Any]) -> dict[str, Any]:
    def text(key: str) -> str:
        return " ".join(str(raw.get(key) or "").split())

    return {"is_report": bool(raw.get("is_report", True)), "name": text("name"),
            "handles": [str(h).strip() for h in raw.get("handles") or [] if str(h).strip()][:10],
            "left": text("left"), "joined": text("joined"), "role": text("role"),
            "others": [o for o in raw.get("others") or [] if isinstance(o, Mapping) and str(o.get("name") or "").strip()],
            "reason": text("reason")}


def identify(store: Any, config: Any, env: Mapping[str, str], candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Who the report is about: one strict call, no tools."""
    subject = candidate.get("subject") or {}
    spec = routines.spec_for("identify_mention", config, instructions=prompts.text("mention_subject.md"),
                             schema=IDENTIFY_SCHEMA, final_call=False, max_tokens=2000)
    seed = {"post": {"text": util.clip(candidate.get("text"), 6000), "url": candidate.get("url") or "",
                     "author": subject.get("author") or "", "published_at": subject.get("published_at") or "",
                     "feed": candidate.get("feed") or ""},
            "names_given": list(subject.get("names") or []), "links_in_post": dict(subject.get("mentions") or {})}
    run = routines.run_routine(store, spec, seed, subject=f"candidate:{candidate['id']}", env=env)
    return _identity(run.answer if isinstance(run.answer, Mapping) else {})


def split_round_up(store: Any, candidate: Mapping[str, Any], identity: Mapping[str, Any]) -> list[int]:
    """The other people a round-up names, each a candidate of its own on the same item."""
    created = []
    subject = dict(candidate.get("subject") or {})
    for n, other in enumerate(list(identity.get("others") or [])[:MAX_OTHERS], start=1):
        person = _identity({**dict(other), "is_report": True, "others": [],
                            "reason": f"named in a round-up with {identity.get('name') or '?'}"})
        child_id, new = store.add_candidate(candidate["feed"], f"{candidate['item_id']}#{n}", url=candidate.get("url") or "",
                                            text=candidate.get("text") or "",
                                            subject={**subject, "identified": person, "from_candidate": candidate["id"]})
        if new:
            created.append(child_id)
    return created


# -- filter -----------------------------------------------------------------------------------

def out_of_scope(config: Any, identity: Mapping[str, Any], aliases: Mapping[str, set[str]]) -> str:
    """Why the report touches no organization in the priority tiers, or '' when it does (or no
    tiers are configured)."""
    tier_orgs = [org for orgs in config.tier_orgs().values() for org in orgs]
    if not tier_orgs:
        return ""
    moved = [o for o in (identity.get("left"), identity.get("joined")) if o]
    if any(names.org_match(o, t, aliases) for o in moved for t in tier_orgs):
        return ""
    return f"{identity.get('left') or '?'} to {identity.get('joined') or '?'}: no organization in the priority tiers"


def role_out_of_scope(role: str, roles: list[str]) -> str:
    """The configured out-of-scope role the report's role names, or ''."""
    text = names.plain(role)
    for phrase in roles:
        if phrase and re.search(rf"(?<![a-z0-9]){re.escape(names.plain(phrase))}(?![a-z0-9])", text):
            return phrase
    return ""


def given_links(identity: Mapping[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for value in identity.get("handles") or []:
        out.setdefault(link_rules.guess_kind(value), value)
    return link_rules.normalize_all(out)


def already_listed(store: Any, identity: Mapping[str, Any], aliases: Mapping[str, set[str]]) -> dict[str, Any] | None:
    """The person on the list this report is about: one of their links, or their name with an
    organization the report names."""
    for kind, url in given_links(identity).items():
        person = store.person_by_link(kind, url)
        if person:
            return person
    name = identity.get("name") or ""
    orgs = [o for o in (identity.get("left"), identity.get("joined")) if o]
    if not name or not orgs:
        return None
    for person in store.find_people(name, limit=20):
        if names.compatible(name, person["name"], "variant") and \
                any(names.org_match(person.get("organization"), o, aliases) for o in orgs):
            return person
    return None


def duplicate_of(store: Any, candidate: Mapping[str, Any], identity: Mapping[str, Any],
                 aliases: Mapping[str, set[str]]) -> int | None:
    """An open candidate about the same person and move (another post about it)."""
    name = identity.get("name") or ""
    if not name:
        return None
    orgs = {names.plain(o) for o in (identity.get("left"), identity.get("joined")) if o}
    for other in store.recent_candidates(since=util.ago(days=DUPLICATE_DAYS), states=OPEN_STATES):
        if other["id"] == candidate["id"] or other["id"] > candidate["id"]:
            continue
        theirs = (other.get("subject") or {}).get("identified") or {}
        if not names.compatible(name, theirs.get("name"), "variant"):
            continue
        their_orgs = [o for o in (theirs.get("left"), theirs.get("joined")) if o]
        if not orgs or not their_orgs or any(names.org_match(a, b, aliases) for a in orgs for b in their_orgs):
            return int(other["id"])
    return None


# -- the add path -----------------------------------------------------------------------------

def item_of(candidate: Mapping[str, Any]) -> FeedItem:
    subject = candidate.get("subject") or {}
    return FeedItem(id=str(candidate.get("item_id") or ""), url=str(candidate.get("url") or ""), text=str(candidate.get("text") or ""),
                    author=str(subject.get("author") or ""), published_at=str(subject.get("published_at") or ""))


def record_on(store: Any, candidate: Mapping[str, Any], person: Mapping[str, Any]) -> int:
    """The report as an event of ``person``, for the judge to read."""
    from tailwatch.pipeline.watch import record_report

    return record_report(store, str(candidate.get("feed") or "report"), item_of(candidate), person, others=0)


def add_from(store: Any, candidate: Mapping[str, Any], identity: Mapping[str, Any], links: Mapping[str, str], *,
             organization: str = "", title: str = "") -> tuple[str, str, int | None]:
    """(state, reason, person id): the person joins the list with their report as the first event."""
    name = identity.get("name") or ""
    if not name:
        return "needs_review", "the report names nobody", None
    normalized = link_rules.normalize_all(dict(links))
    if not normalized:
        return "needs_review", "no profile link to add with", None
    org = identity.get("left") or identity.get("joined") or organization
    try:
        person_id, created = store.add_person(name, organization=org, title=identity.get("role") or title, links=normalized,
                                              added_because=f"report: {candidate.get('feed')} {candidate.get('item_id')}")
    except PermissionError:
        return "dismissed", "removed from the list earlier; not added back", None
    person = store.person(person_id) or {}
    if not created:
        if person.get("status") == "tracked":
            record_on(store, candidate, person)
        return "dismissed", f"already on the list as #{person_id}", person_id
    record_on(store, candidate, person)
    return "added", f"added with {', '.join(sorted(normalized))}", person_id


def _set(store: Any, candidate_id: int, state: str, reason: str, **extra: Any) -> None:
    store.update_candidate(candidate_id, state=state, reason=util.clip(reason, 500), **extra)


def _provider_failure(error: BaseException) -> bool:
    cause = error.__cause__ if isinstance(error.__cause__, ModelError) else error
    return isinstance(cause, ModelError) and bool(cause.outage or cause.retryable or cause.status in (401, 403, 429))


def spent_today(store: Any, config: Any) -> float:
    return store.spend_today(ROUTINES, zone=config.schedule.timezone)


def _cap_reached(store: Any, config: Any, env: Mapping[str, str], waiting: int) -> str:
    cap = float(config.budgets.enrich_daily)
    spent = spent_today(store, config)
    if spent < cap - 1e-9:
        return ""
    from tailwatch.desk.notices import say_once

    say_once(store, config, env, CAP_TOPIC,
             f"Enrichment stopped for today: ${spent:.2f} of the ${cap:.2f} daily budget is spent. "
             f"{waiting} report(s) wait for tomorrow.", tag="enrichment")
    return f"daily cap ${cap:.2f} reached"


# -- the job ----------------------------------------------------------------------------------

def run(store: Any, config: Any, env: Mapping[str, str], *, http: Http | None = None, limit: int = 20,
        log: Any = print) -> dict[str, Any]:
    """The every-tick job: identify, filter, enrich and decide the waiting candidates."""
    counts: dict[str, Any] = {"seen": 0, "identified": 0, "round_up": 0, "out_of_scope": 0, "dismissed": 0,
                              "listed_already": 0, "added": 0, "needs_review": 0, "failed": 0, "usd": 0.0, "stopped": ""}
    if not config.switches.candidates:
        return {**counts, "skipped": "candidates are switched off"}
    aliases = agent.aliases_for(store, config)
    roles = tunables.out_of_scope_roles(store)
    queue = store.candidates_to_work(limit=limit, max_attempts=MAX_ATTEMPTS)
    done: set[int] = set()
    provider_failures = 0
    while queue:
        row = queue.pop(0)
        if row["id"] in done:
            continue
        done.add(row["id"])
        counts["seen"] += 1
        stop = _cap_reached(store, config, env, waiting=len(queue) + 1)
        if stop:
            counts["stopped"] = stop
            break
        state = row["state"]
        try:
            subject = dict(row.get("subject") or {})
            identity = subject.get("identified")
            if not isinstance(identity, Mapping):
                identity = identify(store, config, env, row)
                counts["identified"] += 1
            identity = _identity(identity)
            if state == "new":
                subject["identified"] = identity
                if not identity["is_report"] or not identity["name"]:
                    _set(store, row["id"], "dismissed", identity["reason"] or "not a report about a named person", subject=subject)
                    counts["dismissed"] += 1
                    continue
                children = split_round_up(store, row, identity)
                counts["round_up"] += len(children)
                queue.extend(c for c in (store.candidate(i) for i in children) if c)
                _set(store, row["id"], "identified", identity["reason"], subject=subject)
                state = "identified"
                row = {**row, "subject": subject}
            if screen(store, config, row, identity, aliases, roles, counts):
                continue
            stop = _cap_reached(store, config, env, waiting=len(queue) + 1)
            if stop:
                counts["stopped"] = stop
                break
            _set(store, row["id"], "enriching", "the enricher is looking")
            state = "identified"
            seed = agent.seed_for_report(row, identity)
            toolkit = Toolkit(store, config, env, budget=loop.Budget(float(config.budgets.enrich_per_person)), http=http)
            found = agent.enrich_person(store, config, env, seed, toolkit=toolkit)
            counts["usd"] = round(counts["usd"] + found["usd"], 6)
            provider_failures = 0
            enrichment = {k: v for k, v in found.items() if k != "ledger"}
            if found["removed"]:
                _set(store, row["id"], "dismissed", f"#{found['removed'][0]} was removed from the list earlier",
                     enrichment=enrichment, person_id=found["removed"][0])
                counts["dismissed"] += 1
            elif found["outcome"] == "verified":
                links = dict(found["verified"])
                new_state, reason, person_id = add_from(store, row, identity, links, organization=found["organization"],
                                                        title=found["title"])
                _set(store, row["id"], new_state, reason, enrichment=enrichment, person_id=person_id)
                counts["added" if new_state == "added" else "needs_review" if new_state == "needs_review" else "dismissed"] += 1
                if log and new_state == "added":
                    log(f"candidate {label(row['id'])}: added {identity['name']} as #{person_id} ({reason})")
            else:
                extra = {"person_id": found["shared_with"][0]} if found["shared_with"] else {}
                _set(store, row["id"], "needs_review", found["reason"], enrichment=enrichment, **extra)
                counts["needs_review"] += 1
        except Exception as error:
            counts["failed"] += 1
            counts["usd"] = round(counts["usd"] + float(getattr(error, "usd", 0.0) or 0.0), 6)
            detail = f"{type(error).__name__}: {util.clip(str(error), 300)}"
            if log:
                log(f"candidate {label(row['id'])} failed: {detail}")
            if _provider_failure(error):
                store.update_candidate(row["id"], state=state, reason=detail)
                provider_failures += 1
                if provider_failures >= PROVIDER_FAILURE_STOP:
                    counts["stopped"] = f"the model provider failed {provider_failures} times in a row"
                    break
                continue
            attempts = store.candidate_failed(row["id"], detail, state=state)
            if attempts >= MAX_ATTEMPTS:
                _set(store, row["id"], "needs_review", f"{attempts} failed runs: {detail}")
    return counts


def screen(store: Any, config: Any, row: Mapping[str, Any], identity: Mapping[str, Any], aliases: Mapping[str, set[str]],
           roles: list[str], counts: dict[str, Any]) -> bool:
    """The free checks before anything is paid for. True when the row is settled."""
    why = out_of_scope(config, identity, aliases)
    if why:
        _set(store, row["id"], "out_of_scope", why)
        counts["out_of_scope"] += 1
        return True
    role = role_out_of_scope(identity.get("role") or "", roles)
    if role:
        _set(store, row["id"], "dismissed", f"role outside the pack's interest ({role}): {identity.get('role')}")
        counts["dismissed"] += 1
        return True
    person = already_listed(store, identity, aliases)
    if person is not None:
        if person["status"] == "ignored":
            _set(store, row["id"], "dismissed", f"#{person['id']} was removed from the list earlier", person_id=person["id"])
        else:
            if person["status"] == "tracked":
                record_on(store, row, person)
            _set(store, row["id"], "dismissed", f"already on the list as #{person['id']}; the report was added to their history",
                 person_id=person["id"])
        counts["listed_already"] += 1
        return True
    twin = duplicate_of(store, row, identity, aliases)
    if twin is not None:
        _set(store, row["id"], "dismissed", f"duplicate of {label(twin)}")
        counts["dismissed"] += 1
        return True
    return False


# -- a person's decision ----------------------------------------------------------------------

def _links_from(value: Any) -> dict[str, str]:
    if isinstance(value, Mapping):
        return {str(k): str(v) for k, v in value.items() if str(v or "").strip()}
    out: dict[str, str] = {}
    for item in value or []:
        text = str(item or "").strip()
        if text:
            out.setdefault(link_rules.guess_kind(text), text)
    return out


def decide(store: Any, config: Any, env: Mapping[str, str], candidate_id: int, action: str, *,
           links: dict[str, Any] | list[str] | None = None, note: str = "", by: str) -> dict[str, Any]:
    """Carry out a person's decision on a candidate: ``add`` (with the links they confirmed, or
    the ones the enricher verified), ``dismiss`` (with a reason) or ``retry`` (the enricher tries
    again on the next tick, with a fresh budget)."""
    candidate = store.candidate(int(candidate_id))
    if candidate is None:
        return {"error": f"no candidate {label(int(candidate_id))}"}
    tag = label(candidate["id"])
    enrichment = dict(candidate.get("enrichment") or {})
    decisions = [*list(enrichment.get("decisions") or []), {"action": action, "by": by, "note": note, "at": util.iso()}]
    enrichment["decisions"] = decisions[-20:]
    subject = candidate.get("subject") or {}
    identity = _identity(subject.get("identified") or {"name": (subject.get("names") or [""])[0]})
    if action == "add":
        if candidate["state"] == "added":
            return {"error": f"{tag} was added already, as #{candidate.get('person_id')}"}
        chosen = _links_from(links) or dict(enrichment.get("verified") or {})
        normalized = link_rules.normalize_all(chosen)
        if not normalized:
            return {"error": f"{tag} has no verified link; say the profile link to add with"}
        if not identity["name"]:
            return {"error": f"{tag} names nobody yet; enrich_again first, or dismiss it"}
        state, reason, person_id = add_from(store, candidate, identity, normalized,
                                            organization=str(enrichment.get("organization") or ""))
        if state != "added":
            return {"error": f"{tag}: {reason}", "person_id": person_id}
        store.update_candidate(candidate["id"], state="added", reason=util.clip(note or f"added by {by}", 500),
                               person_id=person_id, enrichment=enrichment)
        return {"candidate": tag, "state": "added", "person_id": person_id, "name": identity["name"], "links": normalized}
    if action == "dismiss":
        if not note.strip():
            return {"error": "a dismissal needs a reason, in the person's words"}
        if candidate["state"] == "added":
            return {"error": f"{tag} was added already, as #{candidate.get('person_id')}; remove the person instead"}
        store.update_candidate(candidate["id"], state="dismissed", reason=util.clip(note, 500), enrichment=enrichment)
        return {"candidate": tag, "state": "dismissed", "reason": note}
    if action == "retry":
        if candidate["state"] == "added":
            return {"error": f"{tag} was added already, as #{candidate.get('person_id')}"}
        state = "identified" if subject.get("identified") else "new"
        store.update_candidate(candidate["id"], state=state, attempts=0, enrichment=enrichment,
                               reason=util.clip(f"sent back by {by}" + (f": {note}" if note else ""), 500))
        return {"candidate": tag, "state": state, "note": "the enricher tries again on the next tick"}
    return {"error": f"unknown action {action!r}; add, dismiss or retry"}
