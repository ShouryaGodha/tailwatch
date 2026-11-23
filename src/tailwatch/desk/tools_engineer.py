"""The engineer skill's desk tools: record a task, list the tasks and their pull requests, the
code map. The engineer itself runs from the scheduler (``engineer.run_waiting``), never inside a
chat turn; a pull request is closed through the core ``decide`` tool (kind T, close) and merged
on GitHub by a person, never from here."""

from __future__ import annotations

from typing import Any

from tailwatch.agent.registry import P, Toolbox, ToolContext
from tailwatch.desk import engineer

TOOLS = Toolbox("engineer")
STATES = tuple(engineer.STATES)


@TOOLS.tool("engineer", "Record an engineer task in the person's own words: what to change and why, with any #R, #I, #P "
            "ids they named. The engineer answers in this thread with a draft pull request; while it is switched off the "
            "task is parked and the tool says so.", writes=True,
            text=P("string", "the task, one plain paragraph in the person's words"))
def start(ctx: ToolContext, text: str) -> dict[str, Any]:
    return engineer.start_task(ctx.store, ctx.config, ctx.env, text, by=ctx.user or ctx.mode or "desk", thread=ctx.thread)


@TOOLS.tool("tasks", "The engineer's tasks in one state: open (waiting, running or with a pull request open), waiting, "
            "merged, closed, failed, done (answered without a change) or all; each with its #T id, who asked, the pull "
            "request, the tests, the self-review and the cost.",
            state=P("string", "which tasks", required=False, enum=STATES))
def tasks(ctx: ToolContext, state: str = "open") -> dict[str, Any]:
    if state not in STATES:
        raise ValueError(f"state is one of {', '.join(STATES)}")
    return {"state": state, "switched_on": bool(ctx.config.switches.engineer), "tasks": engineer.tasks(ctx.store, state)}


@TOOLS.tool("code_map", "tailwatch's modules, each with what it is for: for a question about where something lives.")
def code_map(ctx: ToolContext) -> dict[str, Any]:
    root = engineer.local_root()
    if root is None:
        return {"error": "the code map is read from a clone of tailwatch; this install has none"}
    return {"code_map": engineer.code_map(root)[:15000]}
