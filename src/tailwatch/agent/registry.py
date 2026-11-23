"""Every tool once, and one permission check.

A tool module creates a ``Toolbox`` for its owner (``channel``, ``core`` or a skill's name) and
decorates its handlers:

    TOOLS = Toolbox("data")

    @TOOLS.tool("issues", "List open data issues.", state=P("string", "open|all", required=False))
    def issues(ctx: ToolContext, state: str = "open") -> dict: ...

Flags: ``writes`` (only a person on the team list may use it, checked here and nowhere else),
``mutating`` (memoised by operation hash, so a resumed run never runs it twice; every writing
tool is mutating), ``evidence`` (the result carries outside text and is wrapped as evidence),
``memo_errors`` (even a failure is memoised: it spent money before it failed).

The registry imports every module in ``MODULES``; a name defined twice is an error. The same
registry feeds the Slack modes, the desk's skills and the MCP server.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from tailwatch.agent.loop import StepTools

MODULES = (
    "tailwatch.desk.tools_channel",
    "tailwatch.desk.tools_core",
    "tailwatch.desk.tools_intel",
    "tailwatch.desk.tools_verification",
    "tailwatch.desk.tools_data",
    "tailwatch.desk.tools_maintenance",
    "tailwatch.desk.tools_engineer",
    "tailwatch.enrich.tools_desk",
)


@dataclass
class ToolContext:
    """Who is asking and with what: handed to every tool handler."""

    store: Any
    config: Any
    env: Mapping[str, str] = field(default_factory=dict)
    user: str = ""
    mode: str = "desk"
    channel: str = ""
    thread: str = ""
    root_ids: dict[str, list[int]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class P:
    """One parameter of a tool."""

    type: str
    description: str = ""
    required: bool = True
    enum: tuple[str, ...] = ()
    items: str = "string"

    def schema(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type, "description": self.description}
        if self.enum:
            out["enum"] = list(self.enum)
        if self.type == "array":
            out["items"] = {"type": self.items}
        return out


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Any]
    owner: str
    writes: bool = False
    mutating: bool = False
    evidence: bool = False
    memo_errors: bool = False

    @property
    def spec(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


class Toolbox:
    def __init__(self, owner: str) -> None:
        self.owner = owner
        self.tools: list[Tool] = []

    def tool(self, name: str, description: str, *, writes: bool = False, mutating: bool | None = None,
             evidence: bool = False, memo_errors: bool = False, **params: P) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        schema = {"type": "object", "properties": {k: v.schema() for k, v in params.items()},
                  "required": [k for k, v in params.items() if v.required], "additionalProperties": False}

        def decorate(handler: Callable[..., Any]) -> Callable[..., Any]:
            self.tools.append(Tool(name=name, description=description, input_schema=schema, handler=handler,
                                   owner=self.owner, writes=writes, mutating=writes if mutating is None else mutating,
                                   evidence=evidence, memo_errors=memo_errors))
            return handler

        return decorate


class RegistryError(RuntimeError):
    pass


_cache: dict[str, Tool] | None = None


def build(modules: Iterable[str] = MODULES) -> dict[str, Tool]:
    tools: dict[str, Tool] = {}
    for path in modules:
        module = importlib.import_module(path)
        box = getattr(module, "TOOLS", None)
        if not isinstance(box, Toolbox):
            raise RegistryError(f"{path} has no TOOLS toolbox")
        for tool in box.tools:
            if tool.name in tools:
                raise RegistryError(f"tool {tool.name!r} is defined by {tools[tool.name].owner} and {tool.owner}")
            tools[tool.name] = tool
    return tools


def all_tools() -> dict[str, Tool]:
    global _cache
    if _cache is None:
        _cache = build()
    return _cache


def owned_by(*owners: str) -> list[Tool]:
    return [t for t in all_tools().values() if t.owner in owners]


def step_tools(tools: Iterable[Tool]) -> StepTools:
    tools = list(tools)
    return StepTools(specs=[t.spec for t in tools], mutating=frozenset(t.name for t in tools if t.mutating),
                     memo_errors=frozenset(t.name for t in tools if t.memo_errors),
                     evidence=frozenset(t.name for t in tools if t.evidence))


def team(ctx: ToolContext) -> set[str]:
    listed = set(getattr(ctx.config, "team", None) or [])
    try:
        listed |= set(ctx.store.get_setting("DESK:TEAM", []) or [])
    except Exception:
        pass
    return {str(u) for u in listed if str(u).strip()}


def may_write(ctx: ToolContext) -> bool:
    """A writing tool needs a person on the team list. An empty list lets everyone write, unless
    ``require_team_list`` is set in the config. The local CLI and MCP user (``local``) is the owner."""
    if ctx.user == "local":
        return True
    listed = team(ctx)
    if not listed:
        return not bool(getattr(ctx.config, "require_team_list", False))
    return ctx.user in listed


def execute(tool: Tool, ctx: ToolContext, args: Mapping[str, Any]) -> Any:
    if tool.writes and not may_write(ctx):
        return {"error": f"{tool.name} changes things and needs a person on the team list; "
                         "note what was asked and say who can confirm"}
    allowed = set(tool.input_schema.get("properties", {}))
    unknown = [k for k in args if k not in allowed]
    if unknown:
        return {"error": f"{tool.name} takes no argument(s) {', '.join(unknown)}"}
    missing = [k for k in tool.input_schema.get("required", []) if k not in args]
    if missing:
        return {"error": f"{tool.name} needs {', '.join(missing)}"}
    return tool.handler(ctx, **dict(args))


def executor(tools: Iterable[Tool], ctx: ToolContext) -> Callable[[str, dict[str, Any]], Any]:
    by_name = {t.name: t for t in tools}

    def run(name: str, args: dict[str, Any]) -> Any:
        tool = by_name.get(name) or all_tools().get(name)
        if tool is None or (name not in by_name and tool.owner not in getattr(ctx, "extra", {}).get("owners", ())):
            return {"error": f"no tool named {name} here"}
        return execute(tool, ctx, args)

    return run
