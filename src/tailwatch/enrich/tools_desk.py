"""The desk's enrichment skill: what waits for a person among the reports about people not on
the list, why the enricher could or could not prove a profile, and a person's decisions (add,
dismiss, try again) and changes to the enricher's tunables. Writing tools need a person on the
team list (the registry's one check)."""

from __future__ import annotations

import json
from typing import Any

from tailwatch.agent.registry import P, Toolbox, ToolContext
from tailwatch.enrich import candidates
from tailwatch.enrich import settings as tunables

TOOLS = Toolbox("enrichment")
STATES = ("needs_review", "new", "identified", "enriching", "out_of_scope", "added", "dismissed", "all")
LIST_CAP = 30
SETTING_NAMES = tuple(tunables.SHORT)


def _id(value: Any) -> int | None:
    text = str(value or "").strip().lstrip("#").lstrip("Cc")
    return int(text) if text.isdigit() else None


def _not_an_id(value: Any) -> dict[str, Any]:
    return {"error": f"not a candidate id: {value!r} (write #C12)"}


def _brief(row: dict[str, Any]) -> dict[str, Any]:
    who = (row.get("subject") or {}).get("identified") or {}
    found = row.get("enrichment") or {}
    return {"id": candidates.label(row["id"]), "name": who.get("name") or "", "left": who.get("left") or "",
            "joined": who.get("joined") or "", "role": who.get("role") or "", "state": row["state"], "why": row.get("reason") or "",
            "verified": found.get("verified") or {}, "usd": found.get("usd"), "person": f"#{row['person_id']}" if row.get("person_id") else "",
            "seen": str(row.get("created_at") or "")[:10]}


@TOOLS.tool("mentions", "Reports about people not on the list, in one state (needs_review by default), newest first: id, "
            "name, the move, the state and why, the links the enricher verified.",
            state=P("string", "which state; all for every state", required=False, enum=STATES))
def mentions(ctx: ToolContext, state: str = "needs_review") -> dict[str, Any]:
    states = () if state == "all" else (state,)
    rows = ctx.store.candidates(states=states, limit=LIST_CAP)
    return {"state": state, "count": len(rows), "candidates": [_brief(r) for r in rows],
            "totals": ctx.store.candidate_counts()}


@TOOLS.tool("mention", "One report in full: the post, who it is about, every link the enricher considered with its verdict, "
            "quote and the checks that lowered it, its concerns, the tools it called and the cost.", evidence=True,
            candidate_id=P("integer", "the candidate, #C12 is 12"))
def mention(ctx: ToolContext, candidate_id: Any) -> dict[str, Any]:
    number = _id(candidate_id)
    if number is None:
        return _not_an_id(candidate_id)
    row = ctx.store.candidate(number)
    if row is None:
        return {"error": f"no candidate {candidate_id}"}
    found = dict(row.get("enrichment") or {})
    return {**_brief(row), "feed": row["feed"], "item": row["item_id"], "url": row["url"], "text": row["text"],
            "identified": (row.get("subject") or {}).get("identified") or {}, "attempts": row["attempts"],
            "links": [{k: link.get(k) for k in ("kind", "url", "verdict", "claimed", "confidence", "quote", "quote_url", "notes",
                                                "reason", "held_by")} for link in found.get("links") or []],
            "outcome": found.get("outcome"), "outcome_reason": found.get("reason"), "summary": found.get("summary"),
            "concerns": found.get("concerns") or [], "tool_calls": found.get("tool_calls") or [],
            "stopped": found.get("stopped") or "", "decisions": found.get("decisions") or []}


@TOOLS.tool("add_mention", "Add a report's person to the list now. links: the profile URLs the person confirmed; [] to use "
            "the links the enricher verified. The report becomes their first event.", writes=True,
            candidate_id=P("integer", "the candidate, #C12 is 12"),
            links=P("array", "profile URLs the person confirmed", required=False))
def add_mention(ctx: ToolContext, candidate_id: Any, links: list[str] | None = None) -> dict[str, Any]:
    number = _id(candidate_id)
    if number is None:
        return _not_an_id(candidate_id)
    return candidates.decide(ctx.store, ctx.config, ctx.env, number, "add", links=list(links or []), by=ctx.user)


@TOOLS.tool("dismiss_mention", "Dismiss a report: not for the list. Keep the reason in the person's words.", writes=True,
            candidate_id=P("integer", "the candidate, #C12 is 12"), reason=P("string", "why, as the person said it"))
def dismiss_mention(ctx: ToolContext, candidate_id: Any, reason: str) -> dict[str, Any]:
    number = _id(candidate_id)
    if number is None:
        return _not_an_id(candidate_id)
    return candidates.decide(ctx.store, ctx.config, ctx.env, number, "dismiss", note=reason, by=ctx.user)


@TOOLS.tool("enrich_again", "Send a report back to the enricher: it tries again on the next tick with a fresh budget.",
            writes=True, candidate_id=P("integer", "the candidate, #C12 is 12"),
            note=P("string", "what to look for, when the person said", required=False))
def enrich_again(ctx: ToolContext, candidate_id: Any, note: str = "") -> dict[str, Any]:
    number = _id(candidate_id)
    if number is None:
        return _not_an_id(candidate_id)
    return candidates.decide(ctx.store, ctx.config, ctx.env, number, "retry", note=note, by=ctx.user)


@TOOLS.tool("settings_show", "The enricher's tunables now (floors, learned rules, organization aliases, out-of-scope roles, "
            "links per day) with their versions and last changes.")
def settings_show(ctx: ToolContext) -> dict[str, Any]:
    shown = tunables.show(ctx.store)
    return {"settings": {k.split(":", 1)[1].lower(): v for k, v in shown.items()},
            "limits": {"floors": tunables.FLOOR_RANGES, "name_level_min": tunables.NAME_LEVELS, "rules": tunables.MAX_RULES,
                       "links_per_day": tunables.LINKS_PER_DAY_RANGE}}


@TOOLS.tool("settings_change", "Change one enricher tunable as the person asked. key: floors, rules, aliases, "
            "out_of_scope_roles or links_per_day. value: JSON or text: floors and aliases take a partial object "
            "({\"confidence_min\": 0.85}, {\"gdm\": \"Google DeepMind\"}; an alias set to \"\" is removed); rules and roles "
            "take a sentence to add, '-' and the sentence to remove, or a JSON list to replace; links_per_day a number. "
            "Validated against the limits; a new version is kept under the person's name.", writes=True,
            key=P("string", "which tunable", enum=SETTING_NAMES), value=P("string", "the change"),
            note=P("string", "why, as the person said it"))
def settings_change(ctx: ToolContext, key: str, value: str, note: str) -> dict[str, Any]:
    try:
        parsed: Any = json.loads(value)
    except (TypeError, ValueError):
        parsed = value
    if isinstance(parsed, (int, float)) and tunables.key_of(key) != tunables.LINKS_PER_DAY:
        parsed = value
    try:
        changed = tunables.change(ctx.store, key, parsed, by=ctx.user, note=note)
    except tunables.SettingError as error:
        return {"error": str(error)}
    return {"changed": changed["key"], "version": changed["version"], "value": changed["value"]}


@TOOLS.tool("settings_revert", "Put back what a tunable was before the given version; the revert is a new version.",
            writes=True, key=P("string", "which tunable", enum=SETTING_NAMES), version=P("integer", "the version to undo"))
def settings_revert(ctx: ToolContext, key: str, version: int) -> dict[str, Any]:
    try:
        reverted = tunables.revert(ctx.store, key, int(version), by=ctx.user)
    except (tunables.SettingError, ValueError) as error:
        return {"error": str(error)}
    return {"reverted": reverted["key"], "version": reverted["version"], "value": reverted["value"]}
