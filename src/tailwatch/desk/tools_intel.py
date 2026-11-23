"""The intel skill: what the team heard, kept dated and under the speaker's name, read by the
judge and the editor as a hint about where to look, never as evidence that anything changed.

The text is scanned before it is kept: a sentence of plain words, no link, no id, no Slack
mention, under 500 characters. Anything else is a fact for the data skill (``correct_person``)
or a decision for ``decide``.
"""

from __future__ import annotations

import re
from typing import Any

from tailwatch import util
from tailwatch.agent.registry import P, Toolbox, ToolContext

TOOLS = Toolbox("intel")

MAX_TEXT = 500
MIN_TEXT = 8
REVIEW_DAYS = 30
SCOPES = ("everyone", "organization", "person")
_LINK = re.compile(r"https?://|www\.|\b[\w-]+\.(?:com|org|net|io|ai|dev|app|co)/", re.IGNORECASE)
_MENTION = re.compile(r"<[@#!][^>]*>|\bU[A-Z0-9]{8,}\b")
_ID = re.compile(r"(?<![A-Za-z0-9])#?(?:[RIPGCT]\d+|PR\s*#?\d+)\b|#\d+\b", re.IGNORECASE)
_ORDER = re.compile(r"\b(?:always|never)\s+(?:post|send|drop|confirm|hold)\b|\bignore (?:the|your|all) (?:rules|instructions)\b",
                    re.IGNORECASE)


def scan(text: str) -> str:
    """Why an entry is refused, or ''."""
    clean = " ".join(str(text or "").split())
    if len(clean) < MIN_TEXT:
        return "say what you heard, in a sentence"
    if len(clean) >= MAX_TEXT:
        return f"keep it under {MAX_TEXT} characters"
    if _LINK.search(clean):
        return "no links in intel: say what you heard in words"
    if _MENTION.search(clean):
        return "no Slack mentions or user ids in intel: the speaker is recorded already"
    if _ID.search(clean):
        return "no ids in intel (#R12, #P3 ...): a decision on an id goes through decide"
    if _ORDER.search(clean):
        return "intel is what was heard, not an order about posting"
    return ""


@TOOLS.tool("remember", "Keep what a person heard as team intel, under their name, for the judge and the editor to read "
            "when they decide about a matching person. scope_kind everyone, organization (scope_value: the organization) or "
            "person (scope_value: the person's id or name). heard_where: where they heard it.", writes=True,
            text=P("string", "the fact, in the person's words, trimmed"),
            scope_kind=P("string", "who it is about", enum=SCOPES),
            scope_value=P("string", "the organization, or the person's id or name; '' for everyone"),
            heard_where=P("string", "where they heard it, or ''", required=False))
def remember(ctx: ToolContext, text: str, scope_kind: str, scope_value: str, heard_where: str = "") -> dict[str, Any]:
    why = scan(text)
    if why:
        return {"error": why}
    if scope_kind not in SCOPES:
        return {"error": "scope_kind is everyone, organization or person"}
    value = " ".join(str(scope_value or "").split())
    if scope_kind == "organization" and not value:
        return {"error": "an organization scope needs the organization's name"}
    if scope_kind == "person":
        from tailwatch.desk.tools_core import resolve_person

        person, error = resolve_person(ctx.store, value)
        if error:
            return error
        assert person is not None
        value = str(person["id"])
    if scope_kind == "everyone":
        value = ""
    entry_id = ctx.store.add_intel(" ".join(str(text).split()), scope_kind=scope_kind, scope_value=util.clip(value, 120),
                                   said_by=ctx.user or "desk", heard_where=util.clip(" ".join(str(heard_where or "").split()), 200),
                                   review_days=REVIEW_DAYS)
    entry = ctx.store.intel_entry(entry_id) or {}
    return {"id": f"#G{entry_id}", "scope": scope_kind, "scope_value": value, "review_at": str(entry.get("review_at") or "")[:10]}


@TOOLS.tool("intel", "What the team told us: #G id, scope, who said it, when, when it is up for review. state active "
            "(default), retired or all.", state=P("string", "active, retired or all", required=False,
                                                  enum=("active", "retired", "all")))
def intel(ctx: ToolContext, state: str = "active") -> dict[str, Any]:
    states = ("active", "retired") if state == "all" else (state or "active",)
    rows = [row for s in states for row in ctx.store.intel(state=s, limit=100)]
    return {"state": state, "entries": [{"id": f"#G{r['id']}", "text": r["text"], "scope": r["scope_kind"],
                                         "scope_value": r["scope_value"], "said_by": r["said_by"], "heard_where": r["heard_where"],
                                         "said_on": r["created_at"][:10], "review_at": r["review_at"][:10], "state": r["state"],
                                         "asks": r["asks"]} for r in rows]}


@TOOLS.tool("confirm_intel", "A person's answer about an entry: keep true (another 30 days) or keep false (retired).",
            writes=True, intel_id=P("integer", "the #G number"), keep=P("boolean", "true keeps it, false retires it"))
def confirm_intel(ctx: ToolContext, intel_id: int, keep: bool) -> dict[str, Any]:
    return confirm(ctx.store, int(intel_id), keep=bool(keep), by=ctx.user or "desk")


def confirm(store: Any, intel_id: int, *, keep: bool, by: str, note: str = "") -> dict[str, Any]:
    """Keep (review again in 30 days, asks cleared) or retire one entry; shared with decide G."""
    entry = store.intel_entry(intel_id)
    if entry is None:
        return {"error": f"no intel #G{intel_id}"}
    if entry["state"] != "active":
        return {"error": f"#G{intel_id} is already {entry['state']}"}
    if keep:
        store.set_intel(intel_id, review_at=util.ahead(days=REVIEW_DAYS), asks=0)
        return {"id": f"#G{intel_id}", "state": "active", "review_at": util.ahead(days=REVIEW_DAYS)[:10], "by": by}
    store.set_intel(intel_id, state="retired")
    return {"id": f"#G{intel_id}", "state": "retired", "by": by, "note": note}
