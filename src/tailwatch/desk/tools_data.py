"""The data skill: the custodian's health, scans, issues and playbooks on request, and the three
changes a person makes to the list by hand (add, correct, remove), each recorded with who and
why."""

from __future__ import annotations

from typing import Any

from tailwatch import util
from tailwatch.agent.registry import P, ToolContext, Toolbox
from tailwatch.desk import custodian, scans
from tailwatch.desk.tools_core import rename_param

TOOLS = Toolbox("data")

ISSUE_STATES = ("open", "running", "fixed", "failed", "wontfix", "all")


def _brief(issue: dict[str, Any]) -> dict[str, Any]:
    return {"id": f"#I{issue['id']}", "kind": issue["kind"], "subject": issue["subject_key"], "state": issue["state"],
            "playbook": issue["playbook"] or None, "attempts": issue["attempts"], "found_by": issue["found_by"],
            "note": util.clip(issue["note"], 200), "review": f"#R{issue['review_id']}" if issue.get("review_id") else None,
            "updated": issue["updated_at"][:16]}


@TOOLS.tool("health", "The data's health: the coverage numbers, issues by state and kind, when each scan last ran, "
            "the playbooks and what each does, and whether the desk is paused.")
def health(ctx: ToolContext) -> dict[str, Any]:
    from tailwatch.pipeline import coverage

    cover = coverage.report(ctx.store)
    return {"coverage": coverage.render(cover), **custodian.health(ctx.store, ctx.config)}


@TOOLS.tool("scan", "Run one scan now, whatever its rota, and write what it finds as issues: broken_sources, "
            "no_active_source, shared_links, parked_checks, settings_growth, candidates_waiting, or all.", writes=True,
            kind=P("string", "the scan, or all", enum=(*scans.KINDS, "all")))
def scan(ctx: ToolContext, kind: str) -> dict[str, Any]:
    kinds = None if kind == "all" else (kind,)
    return scans.run(ctx.store, ctx.config, ctx.env, kinds, force=True, checkers=ctx.extra.get("checkers"))


@TOOLS.tool("issues", "The issues in one state (open by default), at most 50: #I id, kind, subject, playbook, attempts.",
            state=P("string", "the state", required=False, enum=ISSUE_STATES))
def issues(ctx: ToolContext, state: str = "open") -> dict[str, Any]:
    return {"state": state, "issues": [_brief(i) for i in ctx.store.issues(state=state or "open", limit=50)]}


@TOOLS.tool("issue", "One issue in full, with its arguments and the result of its last run.", evidence=True,
            issue_id=P("integer", "the #I number"))
def issue(ctx: ToolContext, issue_id: int) -> dict[str, Any]:
    found = ctx.store.issue(int(issue_id))
    if found is None:
        return {"error": f"no issue #I{issue_id}"}
    return {**_brief(found), "note": found["note"], "args": found["args"], "result": found["result"]}


@TOOLS.tool("run_playbook", "Run one issue's playbook now (dry_run shows what it would do). Only a playbook on the "
            "allowed list runs; anything else is for a person.", writes=True,
            issue_id=P("integer", "the #I number"), dry_run=P("boolean", "only say what it would do", required=False))
def run_playbook(ctx: ToolContext, issue_id: int, dry_run: bool = False) -> dict[str, Any]:
    found = ctx.store.issue(int(issue_id))
    if found is None:
        return {"error": f"no issue #I{issue_id}"}
    if found["state"] not in ("open", "failed"):
        return {"error": f"#I{issue_id} is {found['state']}; decide I{issue_id} retry opens it again"}
    result = custodian.run_issue(ctx.store, ctx.config, ctx.env, found, dry_run=bool(dry_run), checkers=ctx.extra.get("checkers"))
    return {"issue": f"#I{issue_id}", "playbook": found["playbook"], **result}


@TOOLS.tool("add_person", "Put a person on the list now with the links they gave (at least one: a profile the checks "
            "read), where they work and as what when known.", writes=True,
            name_=P("string", "the person's name"), links=P("array", "profile links (LinkedIn, GitHub, homepage ...)"),
            organization=P("string", "where they work, or ''", required=False),
            title=P("string", "their title, or ''", required=False))
def add_person(ctx: ToolContext, name_: str, links: list[str], organization: str = "", title: str = "") -> dict[str, Any]:
    from tailwatch import links as link_rules

    name = " ".join(str(name_ or "").split())
    if not name:
        return {"error": "a name is needed"}
    given: dict[str, str] = {}
    for url in links or []:
        url = str(url).strip()
        if url:
            given.setdefault(link_rules.guess_kind(url), url)
    if not link_rules.normalize_all(given):
        return {"error": "at least one link is needed: a profile the checks can read"}
    try:
        person_id, created = ctx.store.add_person(name, organization=organization, title=title, links=given,
                                                  added_because=f"desk: {ctx.user or 'local'}")
    except PermissionError as error:
        return {"error": str(error)}
    if not created:
        return {"person_id": person_id, "note": "already on the list (one of these links is theirs)"}
    from tailwatch.connectors.registry import enabled_checkers

    synced = ctx.store.sync_sources(ctx.extra.get("checkers") or enabled_checkers(ctx.config, ctx.env))
    return {"person_id": person_id, "created": True, "sources": synced.get("created", 0),
            "note": "added; the first read records only, changes start from the second"}


rename_param(TOOLS, "add_person", "name_", "name")


@TOOLS.tool("correct_person", "Correct where a person works and as what, when a person says our record is wrong. An empty "
            "value leaves that field alone. The reason and who said it are kept on the row.", writes=True,
            person_id=P("integer", "the person's id"), organization=P("string", "the organization, or ''"),
            title=P("string", "the title, or ''"), reason=P("string", "why, in the person's words"))
def correct_person(ctx: ToolContext, person_id: int, organization: str, title: str, reason: str) -> dict[str, Any]:
    store = ctx.store
    person = store.person(int(person_id))
    if person is None:
        return {"error": f"no person with id {person_id}"}
    reason = " ".join(str(reason or "").split())
    if not reason:
        return {"error": "a correction needs a reason"}
    organization, title = " ".join(str(organization or "").split()), " ".join(str(title or "").split())
    if not (organization or title):
        return {"error": "say the organization, the title or both"}
    new_org, new_title = organization or person["organization"], title or person["title"]
    if (new_org, new_title) == (person["organization"], person["title"]):
        return {"changed": False, "note": "the record already says that"}
    line = (f"{util.iso()[:10]} {ctx.user or 'local'}: {person['title'] or '?'} at {person['organization'] or '?'} -> "
            f"{new_title or '?'} at {new_org or '?'} ({util.clip(reason, 200)})")
    notes = "\n".join(x for x in (person["notes"], line) if x)
    store.update_person(person["id"], organization=new_org, title=new_title, notes=util.clip(notes, 4000), by=ctx.user)
    return {"changed": True, "person_id": person["id"], "organization": new_org, "title": new_title, "recorded": line}


@TOOLS.tool("remove_person", "Take a person off the list (ignored, with a removal record so an import never adds them "
            "back). Only when a person asks, with their reason.", writes=True,
            person_id=P("integer", "the person's id"), reason=P("string", "why, in the person's words"))
def remove_person(ctx: ToolContext, person_id: int, reason: str) -> dict[str, Any]:
    person = ctx.store.person(int(person_id))
    if person is None:
        return {"error": f"no person with id {person_id}"}
    reason = " ".join(str(reason or "").split())
    if not reason:
        return {"error": "a removal needs a reason"}
    if person["status"] == "ignored":
        return {"changed": False, "note": f"{person['name']} is already off the list"}
    ctx.store.remove_person(person["id"], reason=util.clip(reason, 300), by=ctx.user or "local")
    return {"changed": True, "person_id": person["id"], "name": person["name"], "status": "ignored"}
