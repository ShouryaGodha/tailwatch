"""The desk's core tools, offered in every desk conversation: reading the system (docs, schema,
status, query, the run log), one person (find, history, why), loading a skill, and carrying out
a person's decision by id.

``decide`` is the one door for decisions: the id's kind picks the module that owns it (a card
review, a proposal or an engineer task, a candidate, an intel entry, an issue). Those modules
are imported when a decision arrives, so the desk works while one of them is not installed.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from tailwatch import util
from tailwatch.agent.registry import P, Toolbox, ToolContext
from tailwatch.desk import ids

TOOLS = Toolbox("core")

PAUSED_KEY = "DESK:PAUSED"
PERSON_MATCHES = 20
TABLE_NOTES = {
    "people": ("the watchlist: one row per person, status tracked, left or ignored",
               "a tracked person with no active source, or an organization no read supports"),
    "person_links": ("one spelling of every link, so nobody is listed twice", "a person whose links JSON names a key held here by another"),
    "removals": ("people taken off the list, remembered so an import never adds them back", "a removal with no reason"),
    "sources": ("one source (a profile, a page, a feed) of one person, with its status and when it is due",
                "active sources never checked or long overdue, a fail streak of 5 or more, broken pages that answer again"),
    "observations": ("one row per read of a source; judge_state says whether the judge has seen it",
                     "checks pending for hours, or parked (the judge failed three times)"),
    "events": ("one row per difference between two reads; never changes", "many events from one source in a day (a flapping page)"),
    "verdicts": ("the judge's answer per check, append-only; the newest per observation is current",
                 "low-confidence signals, or a person's corrections piling up on one signal"),
    "card_reviews": ("the editor's record of one card: pending, verified, fixing, held, dropped, superseded",
                     "cards held for days with no decision, or fixing with no open issue"),
    "card_labels": ("right, wrong or unsure on a card, by a person; the holdout set", "no labels in a week"),
    "digests": ("one digest per channel and day, with its delivery state", "a digest left in sending"),
    "posts": ("one row per card a digest carried", "two posts of one move for one person within 30 days"),
    "candidates": ("reports about people not on the list, and their enrichment", "needs_review for over a week"),
    "intel": ("what the team heard: where to look, never evidence", "active entries long past review_at"),
    "issues": ("data problems for the custodian, one per kind and subject", "failed issues, or hundreds open at once"),
    "proposals": ("what the maintainer proposes and a person applies", "proposals expiring with no decision"),
    "runs": ("the run log: one row per routine run or chat message, with its real cost", "failed or capped runs, a cost spike"),
    "jobs": ("one receipt per daily job and day", "a job failed three times, or running for hours"),
    "settings": ("versioned switches, learned rules, floors and markers", "per-day keys that only grow"),
    "settings_history": ("every version of every setting", ""),
}


def _int(value: Any) -> int | None:
    try:
        return int(str(value).strip().lstrip("#"))
    except (TypeError, ValueError):
        return None


def _brief_person(row: Mapping[str, Any]) -> dict[str, Any]:
    return {"id": row["id"], "name": row["name"], "organization": row.get("organization") or "", "title": row.get("title") or "",
            "status": row.get("status"), "links": row.get("links") or {}}


def resolve_person(store: Any, query: Any) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """(person, None) for one match by id, link or name; (None, error) for none or several, so
    the model asks rather than guesses."""
    text = str(query or "").strip()
    if not text:
        return None, {"error": "say which person (a name, a link or an id)"}
    number = _int(text)
    if number is not None:
        found = store.person(number)
        return (found, None) if found else (None, {"error": f"no person with id {number}"})
    rows = store.find_people(text, limit=PERSON_MATCHES + 1)
    if not rows:
        return None, {"error": f"nobody on the list matches '{text}'"}
    exact = [r for r in rows if r["name"].casefold() == text.casefold()]
    if len(rows) == 1 or len(exact) == 1:
        return (exact or rows)[0], None
    return None, {"error": f"'{text}' matches {len(rows)} people; ask which one", "ambiguous": True,
                  "matches": [_brief_person(r) for r in rows[:PERSON_MATCHES]]}


def by_of(ctx: ToolContext) -> str:
    return ctx.user or ctx.mode or "desk"


def paused(store: Any) -> Any:
    value = store.get_setting(PAUSED_KEY)
    return value or None


# -- skills ---------------------------------------------------------------------------------------
@TOOLS.tool("use_skill", "Read one skill's instructions (its body and learned rules); from then on its tools are offered "
            "in this conversation. The skills are listed under Skills in your instructions.",
            skill_name=P("string", "the skill's name"))
def use_skill(ctx: ToolContext, skill_name: str) -> dict[str, Any]:
    from tailwatch.agent import prompts, skills
    from tailwatch.desk import chat

    wanted = str(skill_name or "").strip().lower()
    found = skills.available(ctx.env)
    skill = found.get(wanted)
    if skill is None:
        return {"error": f"no skill named '{wanted}'", "skills": sorted(found)}
    rules = learned_rules(ctx.store, skill.rules_key)
    try:
        body = skills.body_for(skill, rules, chat.prompt_values(ctx.config))
    except prompts.PromptError:  # a placeholder nothing fills: the body as written, rules after it
        body = skills.body_for(replace(skill, body=skill.body.replace("{{", "{ ").replace("}}", " }")), rules, {})
    loaded = ctx.extra.get("loaded")
    if isinstance(loaded, list) and skill.name not in loaded:
        loaded.append(skill.name)
    tools = sorted(t.name for t in chat.tool_table().values() if t.owner == skill.name)
    return {"skill": skill.name, "body": body, "tools": tools, "writes": list(skill.writes)}


def learned_rules(store: Any, key: str) -> list[str]:
    """The learned rules under a skill's settings key: a list of sentences (or of objects with
    ``text``). A bad value is no rules."""
    if not key:
        return []
    try:
        raw = store.get_setting(key, [])
    except Exception:
        return []
    if isinstance(raw, dict):
        raw = raw.get("rules") or []
    out = []
    for rule in raw if isinstance(raw, list) else []:
        text = rule.get("text") or rule.get("rule") if isinstance(rule, dict) else rule
        if str(text or "").strip():
            out.append(str(text).strip())
    return out


def rename_param(box: Toolbox, tool_name: str, old: str, new: str) -> None:
    """Give a tool's parameter a name the ``Toolbox.tool`` keywords cannot carry (``name``)."""
    for index, tool in enumerate(box.tools):
        if tool.name != tool_name:
            continue
        schema = dict(tool.input_schema)
        schema["properties"] = {(new if k == old else k): v for k, v in schema["properties"].items()}
        schema["required"] = [new if k == old else k for k in schema["required"]]
        handler: Callable[..., Any] = tool.handler

        def renamed(ctx: ToolContext, _handler: Callable[..., Any] = handler, **args: Any) -> Any:
            if new in args:
                args[old] = args.pop(new)
            return _handler(ctx, **args)

        box.tools[index] = replace(tool, input_schema=schema, handler=renamed)


rename_param(TOOLS, "use_skill", "skill_name", "name")


# -- the system -----------------------------------------------------------------------------------
@TOOLS.tool("docs", "One section of the project's documentation (docs/*.md) by its title or a few words of it. An empty "
            "or unknown title answers with the titles.", evidence=True, section=P("string", "a title or a few words"))
def docs(ctx: ToolContext, section: str) -> dict[str, Any]:
    from tailwatch.desk import docs as desk_docs

    return desk_docs.section(section)


@TOOLS.tool("schema", "One table's live columns, what it is for and what unhealthy looks like. An empty name lists the tables.",
            table=P("string", "a table name, or '' for the list"))
def schema(ctx: ToolContext, table: str) -> dict[str, Any]:
    name = str(table or "").strip().lower()
    if not name:
        return {"tables": {t: notes[0] for t, notes in TABLE_NOTES.items()}}
    if name not in TABLE_NOTES:
        return {"error": f"'{table}' is not a table the desk reads", "tables": list(TABLE_NOTES)}
    purpose, unhealthy = TABLE_NOTES[name]
    return {"table": name, "for": purpose, "unhealthy": unhealthy, "columns": ctx.store.table_columns(name)}


@TOOLS.tool("status", "The system now: people by status, coverage, cards waiting for the digest, held reviews, open issues, "
            "proposals waiting, today's spend, and whether the desk is paused.")
def status(ctx: ToolContext) -> dict[str, Any]:
    return status_of(ctx.store, ctx.config)


def status_of(store: Any, config: Any) -> dict[str, Any]:
    from tailwatch.pipeline import coverage

    zone = config.schedule.timezone if config is not None else "UTC"
    cover = coverage.report(store)
    out: dict[str, Any] = {"now": util.iso(), "people": store.count_people(),
                           "coverage": {"no_active_source": cover["no_active_source"], "awaiting_judge": cover["awaiting_judge"],
                                        "parked": cover["parked"],
                                        "sources": {k["kind"]: {"active": k["active"], "overdue": k["overdue"], "failing": k["failing"]}
                                                    for k in cover["kinds"]}}}
    try:
        from tailwatch.pipeline.digest import CHANNEL

        window = float(config.digest.window_hours) if config is not None else 26.0
        out["cards_waiting"] = len(store.waiting_cards(since=util.ago(hours=window), channel=CHANNEL))
    except Exception as error:  # the digest module is optional here
        out["cards_waiting"] = f"unknown: {str(error)[:120]}"
    held = store.reviews(state="held", limit=20)
    out["reviews"] = {state: len(store.reviews(state=state, limit=1000)) for state in ("pending", "fixing", "held")}
    out["held_cards"] = [{"id": f"#R{r['id']}", "person": r["person_name"], "signal": r["signal"], "note": util.clip(r["note"], 160)}
                         for r in held]
    out["issues_open"] = len(store.issues(state="open", limit=1000))
    out["proposals_waiting"] = [{"id": f"#P{p['id']}", "setting": p["setting"], "skill": p["skill"],
                                 "rationale": util.clip(p["rationale"], 160)} for p in store.proposals(limit=20)]
    out["spend_today_usd"] = round(store.spend_today(zone=zone), 4)
    out["desk_paused"] = paused(store)
    return out


@TOOLS.tool("query", "One read-only SQL SELECT over the tables schema('') lists, for counts and lists the other tools do not "
            "give. At most 200 rows come back: aggregate and LIMIT.", sql=P("string", "one SELECT"))
def query(ctx: ToolContext, sql: str) -> dict[str, Any]:
    try:
        rows = ctx.store.read_only_query(sql, limit=200)
    except ValueError as error:
        return {"error": str(error)}
    except Exception as error:
        return {"error": f"the query failed: {str(error)[:300]}"}
    return {"rows": rows, "count": len(rows), "capped": len(rows) >= 200}


# -- people ---------------------------------------------------------------------------------------
@TOOLS.tool("find_person", "Look a person up by name, organization words, a link or an id. At most 20 matches; ambiguity is "
            "returned, never resolved.", query=P("string", "a name, link or id"))
def find_person(ctx: ToolContext, query: str) -> dict[str, Any]:
    rows = ctx.store.find_people(str(query or ""), limit=PERSON_MATCHES + 1)
    return {"matches": [_brief_person(r) for r in rows[:PERSON_MATCHES]], "more_than_20": len(rows) > PERSON_MATCHES,
            "ambiguous": len(rows) > 1}


@TOOLS.tool("person_history", "One person in full: the roster row, their sources with status and when each was checked, "
            "the recent changes with the judge's answers, and the verdicts.", evidence=True,
            person_id=P("integer", "the person's id"))
def person_history(ctx: ToolContext, person_id: int) -> dict[str, Any]:
    store = ctx.store
    person = store.person(int(person_id))
    if person is None:
        return {"error": f"no person with id {person_id}"}
    sources = [{k: s.get(k) for k in ("id", "kind", "url", "status", "status_note", "last_checked_at", "next_due_at", "fail_streak")}
               for s in store.sources_of(person["id"])]
    events = [{"event_id": e["id"], "seen": e["observed_at"][:16], "source": e["kind"], "change": e["label"],
               "before": util.clip(e["before_text"], 300), "after": util.clip(e["after_text"], 300),
               "answer": e.get("signal") or "not judged yet"} for e in store.recent_events(person["id"], since="0000")[:20]]
    verdicts = [{"verdict_id": v["id"], "signal": v["signal"], "confidence": v["confidence"], "summary": v["summary"],
                 "reason": util.clip(v["reason"], 300), "judged_by": v["judged_by"], "by_whom": v["by_whom"],
                 "at": v["created_at"][:16], "source": v["kind"]} for v in store.verdicts_of(person["id"], limit=15)]
    return {"person": {k: person.get(k) for k in ("id", "name", "organization", "title", "since", "status", "status_reason",
                                                  "links", "tags", "notes", "added_because", "created_at")},
            "sources": sources, "events": events, "verdicts": verdicts}


@TOOLS.tool("why", "The evidence behind a person's newest card: the before and after text of each change, the judge's "
            "answer, reason and confidence, the editor's review and whether a digest carried it.", evidence=True,
            person=P("string", "a name, link or id"))
def why(ctx: ToolContext, person: str) -> dict[str, Any]:
    store = ctx.store
    found, error = resolve_person(store, person)
    if error:
        return error
    assert found is not None
    signals = store.verdicts_of(found["id"], limit=1, signals_only=True)
    if not signals:
        return {"person": found["name"], "card": None, "note": "no signal has been judged for this person"}
    verdict = signals[0]
    review = store.review_for(verdict["id"])
    current = store.is_current(verdict["id"])
    return {"person": found["name"], "person_id": found["id"],
            "card": {"verdict_id": verdict["id"], "signal": verdict["signal"], "confidence": verdict["confidence"],
                     "summary": verdict["summary"], "context": verdict["context"], "reason": verdict["reason"],
                     "now": verdict["now"], "source": verdict["kind"], "seen": verdict["observed_at"], "judged_by": verdict["judged_by"],
                     "model": verdict["model"], "prompt_version": verdict["prompt_version"], "current": current,
                     "posted": store.was_posted(verdict["id"])},
            "changes": [{"label": e["label"], "before": util.clip(e["before_text"], 1500), "after": util.clip(e["after_text"], 1500),
                         "content_date": e["content_date"]} for e in store.events_of(verdict["observation_id"])],
            "review": {"id": f"#R{review['id']}", "state": review["state"], "note": review["note"], "floors": review["floors"]}
            if review else None}


# -- the run log ----------------------------------------------------------------------------------
@TOOLS.tool("runs", "The run log, newest first: every routine run and chat message with its state, tokens and cost. "
            "routine '' is every routine.", routine=P("string", "judge, verify_card, desk ... or ''", required=False))
def runs(ctx: ToolContext, routine: str = "") -> dict[str, Any]:
    rows = ctx.store.runs(routine=str(routine or ""), limit=40)
    return {"runs": rows, "spend_today_usd": round(ctx.store.spend_today(), 4)}


@TOOLS.tool("run", "One run of the log in full: its tools and their results, tokens, cost, answer or error.", evidence=True,
            run_id=P("integer", "the run's id"))
def run(ctx: ToolContext, run_id: int) -> dict[str, Any]:
    row = ctx.store.run(int(run_id))
    return row if row else {"error": f"no run {run_id}"}


# -- decisions ------------------------------------------------------------------------------------
ACTIONS = {"R": ("post", "post_now", "drop", "fix", "hold"), "P": ("apply", "reject", "revert", "close"),
           "T": ("apply", "reject", "revert", "close"), "C": ("add", "dismiss", "retry"), "G": ("keep", "drop"),
           "I": ("close", "retry")}
_KIND_WORDS = {"review": "R", "card": "R", "proposal": "P", "task": "T", "candidate": "C", "intel": "G", "issue": "I"}


@TOOLS.tool("decide", "Carry out a person's decision on one id. kind R (a card review): post, post_now, drop, fix, hold. "
            "P (a proposal) or T (an engineer task): apply, reject, revert, close. C (a candidate): add (links: the profile "
            "links they gave), dismiss, retry. G (intel): keep, drop. I (an issue): close, retry. The note is kept.",
            writes=True, kind=P("string", "R, P, T, C, G or I", enum=("R", "P", "T", "C", "G", "I")),
            id=P("integer", "the number of the id"), action=P("string", "what the person decided"),
            note=P("string", "the person's reason or instruction, in their words", required=False),
            links=P("array", "for C add: the links the person gave", required=False))
def decide(ctx: ToolContext, kind: str, id: int, action: str, note: str = "", links: list[str] | None = None) -> dict[str, Any]:
    letter = _KIND_WORDS.get(str(kind).strip().lower(), str(kind).strip().upper().lstrip("#"))
    action = str(action or "").strip().lower().replace(" ", "_")
    if letter not in ACTIONS:
        return {"error": "kind is R, P, T, C, G or I"}
    if action not in ACTIONS[letter]:
        return {"error": f"a {ids.KINDS[letter]} takes {', '.join(ACTIONS[letter])}, not {action or 'nothing'}"}
    number = int(id)
    message = str(ctx.extra.get("message") or "")
    if message:
        unclear = ids.check(message, ctx.root_ids, letter, number)
        if unclear:
            return {"error": unclear, "ask": True}
    by = by_of(ctx)
    store, config, env = ctx.store, ctx.config, ctx.env
    if letter == "R":
        verify = _lazy("tailwatch.desk.verify")
        if isinstance(verify, dict):
            return verify
        return verify.decide(store, config, env, number, action, note=note, by=by)
    if letter in ("P", "T"):
        maintainer = _lazy("tailwatch.desk.maintainer")
        if isinstance(maintainer, dict):
            return maintainer
        return maintainer.decide(store, config, env, number, action, note=note, by=by)
    if letter == "C":
        candidates = _lazy("tailwatch.enrich.candidates")
        if isinstance(candidates, dict):
            return candidates
        return candidates.decide(store, config, env, number, action, links=list(links or []), note=note, by=by)
    if letter == "G":
        from tailwatch.desk import tools_intel

        return tools_intel.confirm(store, number, keep=action == "keep", by=by, note=note)
    return decide_issue(store, number, action, by=by, note=note)


def decide_issue(store: Any, issue_id: int, action: str, *, by: str, note: str = "") -> dict[str, Any]:
    issue = store.issue(issue_id)
    if issue is None:
        return {"error": f"no issue #I{issue_id}"}
    if action == "close":
        if issue["state"] in ("fixed", "wontfix"):
            return {"issue": f"#I{issue_id}", "state": issue["state"], "note": "already closed"}
        store.close_issue(issue_id, "wontfix", result={**issue["result"], "closed_by": by}, note=note or f"closed by {by}")
        return {"issue": f"#I{issue_id}", "state": "wontfix"}
    if issue["state"] in ("open", "running"):
        return {"issue": f"#I{issue_id}", "state": issue["state"], "note": "already waiting for the custodian"}
    store.set_issue(issue_id, state="open", attempts=0, closed_at=None, note=util.clip(note or f"retry asked by {by}", 1000))
    return {"issue": f"#I{issue_id}", "state": "open", "note": "the custodian runs it on its next tick"}


def _lazy(path: str) -> Any:
    import importlib

    try:
        return importlib.import_module(path)
    except ImportError as error:
        return {"error": f"{path} is not installed yet ({error}); nothing was decided"}
