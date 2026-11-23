"""The one entry point for a message to the bot, from Slack, the CLI or a test: ``respond``.

Two modes. In the desk (the desk channel, or the CLI) the agent has the core tools and the
skills it has loaded: default skills from the start, more through ``use_skill``, whose tools
are offered from the next step. In any other channel (the digest's) it has the channel tools
and the read-only core four (``find_person person_history why status``).

Every message is a fresh context: the seed is the thread's memory (its last 30 lines) and the
message. The run row is keyed by the message's key and checkpointed after every turn, so a
retried event resumes where it stopped and never answers, or writes, twice. An answer of
``NO_REPLY`` means the message was not for the bot: nothing is said.
"""

from __future__ import annotations

import importlib
import json
import uuid
from collections.abc import Mapping
from typing import Any

from tailwatch import util
from tailwatch.agent import loop, prompts, registry, routines, skills
from tailwatch.desk import ids

SILENT = "NO_REPLY"
THREAD_KEEP = 30
CHANNEL_CORE = ("find_person", "person_history", "why", "status")
RUNNING_GRACE_MINUTES = 10
LINE_CAP = 3000


# -- the tool table -------------------------------------------------------------------------------
_missing: dict[str, str] = {}


def tool_table() -> dict[str, registry.Tool]:
    """Every registered tool. While a module of ``registry.MODULES`` does not import (not
    installed yet, or broken) the table is built from the ones that do, and ``missing_modules``
    says why each other one is missing; a name defined twice is still an error."""
    try:
        return registry.all_tools()
    except registry.RegistryError:
        raise
    except Exception:
        pass
    present = []
    _missing.clear()
    for path in registry.MODULES:
        try:
            importlib.import_module(path)
            present.append(path)
        except Exception as error:
            _missing[path] = f"{type(error).__name__}: {error}"
    return registry.build(present)


def missing_modules() -> dict[str, str]:
    tool_table()
    return dict(_missing)


def offered(mode: str, loaded: list[str], table: Mapping[str, registry.Tool] | None = None) -> list[registry.Tool]:
    table = table if table is not None else tool_table()
    if mode == "desk":
        owners = ("core", *loaded)
        return [t for t in table.values() if t.owner in owners]
    return [t for t in table.values() if t.owner == "channel" or (t.owner == "core" and t.name in CHANNEL_CORE)]


# -- threads and modes ----------------------------------------------------------------------------
def thread_key(channel: str, thread: str, user: str = "") -> str:
    """The memory of one conversation: a Slack thread, or (with no thread) one person's line."""
    return f"{channel or 'cli'}:{thread or user or 'local'}"


def record_post(store: Any, channel: str, ts: str, text: str) -> None:
    """Keep a post of the bot's (the desk note, a held card, the digest) as the first line of
    the thread under it, so a reply there reads its ids (``#R12``) as the thread's root."""
    store.add_thread_line(thread_key(channel, ts), {"who": "bot", "root": True, "text": util.clip(text, 8000),
                                                    "at": util.iso()}, keep=THREAD_KEEP)


def mode_for(config: Any, channel: str) -> str:
    desk_channel = config.delivery.slack.desk_channel if config is not None else ""
    return "desk" if channel == "cli" or (desk_channel and channel == desk_channel) else "channel"


def prompt_values(config: Any) -> dict[str, Any]:
    return {"NAME": getattr(config, "name", "tailwatch"), "DIGEST_CAP": config.digest.cap if config is not None else 10,
            "SIGNALS": getattr(config, "signals", "")}


def instructions(mode: str, config: Any, env: Mapping[str, str], available: Mapping[str, skills.Skill]) -> str:
    template = prompts.text("desk.md" if mode == "desk" else "channel.md")
    text = prompts.render(template, prompt_values(config)) if prompts.placeholders(template) else template
    if mode == "desk":
        text += "\n\n# Skills\n\n" + (skills.index_text(available) or "- (no skills are installed)")
    return text


def root_of(lines: list[Mapping[str, Any]], given: str = "") -> str:
    if given:
        return given
    for line in lines:
        if line.get("root"):
            return str(line.get("text") or "")
    if lines and lines[0].get("who") == "bot":
        return str(lines[0].get("text") or "")
    return ""


# -- respond --------------------------------------------------------------------------------------
def respond(store: Any, config: Any, env: Mapping[str, str], *, text: str, user: str, channel: str = "", thread: str = "",
            message_key: str = "", mode: str | None = None, root_text: str = "",
            checkers: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Answer one message. Returns ``{answer, run_id, state, skills}``; ``state`` is done, silent
    (``NO_REPLY``: post nothing), failed (retry with the same key to resume), or the stored state
    of an earlier run of the same key (with ``duplicate`` true: post nothing again).
    ``root_text`` is the post the thread hangs off when the caller has it (else the thread's
    recorded root); ``checkers`` replaces the enabled checkers (tests, the demo)."""
    mode = mode or mode_for(config, channel)
    routine = "desk" if mode == "desk" else "channel"
    env = dict(env or {})
    row = store.run_by_key(message_key) if message_key else None
    if row is not None:
        if row["state"] in ("done", "silent", "capped"):
            return {"answer": "" if row["state"] == "silent" else row["answer"], "run_id": row["id"], "state": row["state"],
                    "skills": [], "duplicate": True}
        if row["state"] == "running" and (row["started_at"] or "") > util.ago(minutes=RUNNING_GRACE_MINUTES):
            return {"answer": "", "run_id": row["id"], "state": "running", "skills": [], "duplicate": True}
    available = skills.available(env)
    table = tool_table()
    key = thread_key(channel, thread, user)
    lines = store.thread_lines(key)
    root = root_of(lines, root_text)
    root_ids = ids.parse(root)
    budget = loop.Budget(float(config.budgets.desk_per_message) if config is not None else 0.5)
    ctx = registry.ToolContext(store=store, config=config, env=env, user=user, mode=mode, channel=channel, thread=thread,
                               root_ids=root_ids)
    trusted = registry.may_write(ctx)
    checkpoint = row.get("checkpoint") if row else None
    if checkpoint:
        state = loop.Run.from_json(checkpoint)
    else:
        loaded = [name for name, s in available.items() if s.default] if mode == "desk" else []
        seed = {"now": util.iso(), "mode": mode, "user": user, "user_may_decide": trusted, "root_post": util.clip(root, 4000),
                "root_ids": root_ids, "thread": [_seed_line(line) for line in lines[-THREAD_KEEP:]],
                "skills_loaded": loaded if mode == "desk" else None, "message": text}
        state = loop.Run(messages=[{"role": "user", "content": json.dumps(seed, default=str, ensure_ascii=False)}],
                         loaded=loaded)
        store.add_thread_line(key, {"who": user or "local", "trusted": trusted, "text": util.clip(text, LINE_CAP),
                                    "at": util.iso()}, keep=THREAD_KEEP)
    ctx.extra.update({"loaded": state.loaded, "budget": budget, "message_key": message_key or uuid.uuid4().hex,
                      "message": text, "checkers": checkers})

    def step_tools(run: loop.Run) -> loop.StepTools:
        return registry.step_tools(offered(mode, run.loaded, table))

    def execute(name: str, args: dict[str, Any]) -> Any:
        tools = offered(mode, state.loaded, table)
        if name not in {t.name for t in tools}:
            hint = "; load its skill with use_skill first" if mode == "desk" and name in table else ""
            return {"error": f"no tool named {name} here{hint}"}
        return registry.executor(tools, ctx)(name, args)

    spec_base = routines.spec_for(routine, config, instructions=instructions(mode, config, env, available))
    if row is None:
        try:
            run_id = int(store.open_run(routine, subject=util.clip(f"{channel or 'cli'}:{user}", 200),
                                        key=message_key or None, model=spec_base.model))
        except Exception:  # the same key opened by a concurrent delivery of the event
            again = store.run_by_key(message_key) if message_key else None
            if again is None:
                raise
            return {"answer": "", "run_id": again["id"], "state": again["state"], "skills": [], "duplicate": True}
    else:
        run_id = int(row["id"])

    def save(run: loop.Run) -> None:
        store.checkpoint_run(run_id, run)

    spec = routines.spec_for(routine, config, instructions=spec_base.instructions, tools=step_tools, budget=budget,
                             free_tools=frozenset({"use_skill"}), checkpoint=save, output_cap=loop.CHAT_OUTPUT_CAP,
                             max_tokens=4000, error_stop=5)
    try:
        result = loop.run(spec, state=state, execute=execute, env=env)
    except Exception as error:
        failed = loop.failed_run(error) or state
        store.checkpoint_run(run_id, failed)
        store.finish_run(run_id, failed, state="failed", error=f"{type(error).__name__}: {error}", tool_usd=budget.spent,
                         keep_checkpoint=True)
        return {"answer": "", "run_id": run_id, "state": "failed", "skills": list(failed.loaded),
                "error": util.clip(error, 500)}
    answer = str(result.answer or "").strip()
    silent = answer == SILENT or answer.startswith(SILENT)
    state_name = "silent" if silent else "done"
    store.finish_run(run_id, result, state=state_name, answer=answer, tool_usd=budget.spent)
    if not silent:
        store.add_thread_line(key, {"who": "bot", "text": util.clip(answer, LINE_CAP), "at": util.iso()}, keep=THREAD_KEEP)
    return {"answer": "" if silent else answer, "run_id": run_id, "state": state_name, "skills": list(result.loaded),
            **({"stopped": result.stopped} if result.stopped else {})}


def _seed_line(line: Mapping[str, Any]) -> dict[str, Any]:
    out = {"who": line.get("who"), "text": line.get("text")}
    if "trusted" in line:
        out["trusted"] = line["trusted"]
    if isinstance(line.get("pending"), Mapping):
        out["pending"] = {k: v for k, v in line["pending"].items() if k != "token"}
    if line.get("settled"):
        out["settled"] = True
    return out
