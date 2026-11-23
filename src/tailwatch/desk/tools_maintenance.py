"""The maintenance skill's desk tools: the day's report, the proposals, the learned rules with
their versions, the holdout score, and a preview of what the maintainer would propose today.
A person's apply, reject or revert of a proposal goes through the core ``decide`` tool, which
calls ``maintainer.decide``."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from tailwatch import util
from tailwatch.agent.registry import P, Toolbox, ToolContext
from tailwatch.desk import maintainer

TOOLS = Toolbox("maintenance")
STATES = ("proposed", "applied", "rejected", "reverted", "expired", "all")


def _since_of(day: str, zone: str) -> str:
    """The start of a local day ('YYYY-MM-DD'), or '' for the last day and a bit."""
    text = str(day or "").strip()
    if not text:
        return ""
    try:
        when = datetime.strptime(text, "%Y-%m-%d")
    except ValueError as error:
        raise ValueError("day is YYYY-MM-DD, or '' for the last day") from error
    from zoneinfo import ZoneInfo

    return util.iso(when.replace(tzinfo=ZoneInfo(zone)))


@TOOLS.tool("report", "What the desk did since a day (YYYY-MM-DD, or '' for the last day): reviews by outcome and the "
            "floors that failed, held cards and decisions, issues and what the playbooks did, candidates, jobs, the run "
            "log by routine with cost and errors, the watchers' reads, judge samples, spend, the holdout score, unused rules.",
            day=P("string", "YYYY-MM-DD, or '' for the last day", required=False))
def report(ctx: ToolContext, day: str = "") -> dict[str, Any]:
    packet = maintainer.gather(ctx.store, ctx.config, ctx.env, since=_since_of(day, ctx.config.schedule.timezone))
    current = packet.get("current") or {}
    packet["current"] = {"judge_prompt_version": current.get("judge_prompt_version"), "settings": current.get("settings"),
                         "prompts": {k: f"({len(v)} characters)" for k, v in (current.get("prompts") or {}).items()}}
    return packet


def _proposal(p: dict[str, Any]) -> dict[str, Any]:
    value = p.get("value") or {}
    kind = "T" if p.get("setting") == "CODE" and p.get("skill") == "engineer" else "P"
    return {"id": f"#{kind}{p['id']}", "skill": p.get("skill"), "setting": p.get("setting"), "op": p.get("op"),
            "text": value.get("text"), "rationale": util.clip(p.get("rationale"), 300), "evidence": p.get("evidence"),
            "urgent": bool(p.get("urgent")), "state": p.get("state"), "by": p.get("by_whom"), "decided_by": p.get("decided_by"),
            "note": p.get("note") or "", "created": str(p.get("created_at") or "")[:10], "expires": str(p.get("expires_at") or "")[:10],
            **({"applied": value["applied"]} if value.get("applied") else {}), **({"task": f"#T{value['task']}"} if value.get("task") else {})}


@TOOLS.tool("proposals", "The proposals in one state (proposed by default): #P id, skill, setting, op, the text, the "
            "rationale, the evidence ids, who decided, and for an applied one the setting's key and version.",
            state=P("string", "which proposals", required=False, enum=STATES))
def proposals(ctx: ToolContext, state: str = "proposed") -> dict[str, Any]:
    if state not in STATES:
        raise ValueError(f"state is one of {', '.join(STATES)}")
    rows = [p for p in ctx.store.proposals(state=state, limit=50) if not (p["setting"] == "CODE" and p["skill"] == "engineer")]
    return {"state": state, "proposals": [_proposal(p) for p in rows]}


@TOOLS.tool("rules", "A skill's learned rules, floors and aliases as they stand, each with its version, who changed it "
            "last and the recent versions, so a person can see what an apply or a revert did.",
            skill=P("string", "the skill", enum=maintainer.SKILLS))
def rules(ctx: ToolContext, skill: str) -> dict[str, Any]:
    if skill not in maintainer.SKILLS:
        raise ValueError(f"skill is one of {', '.join(maintainer.SKILLS)}")
    out: dict[str, Any] = {"skill": skill, "settings": []}
    for setting in maintainer.SETTINGS_FOR[skill]:
        key = maintainer.setting_key(skill, setting)
        row = ctx.store.setting_row(key)
        history = [{"version": h["version"], "by": h["by_whom"], "at": str(h["at"])[:16], "note": util.clip(h.get("note"), 160)}
                   for h in ctx.store.setting_history(key, limit=6)]
        out["settings"].append({"key": key, "value": row["value"] if row else maintainer.current_value(ctx.store, key, setting),
                                "version": row["version"] if row else 0, "updated_by": row["updated_by"] if row else "",
                                "history": history})
    if skill == "judge":
        out["note"] = "the judge has no learned rules: it changes through its prompt and the pack's rules (an OTHER proposal)"
    return out


@TOOLS.tool("score", "The editor's score on the labelled holdout: recall, false pass, hold rate and how many cards.")
def score(ctx: ToolContext) -> dict[str, Any]:
    found = maintainer.holdout_score(ctx.store)
    return found if found is not None else {"error": "no score yet: label cards right or wrong first"}


@TOOLS.tool("preview", "Run the maintainer now without writing or posting anything: what it would say, propose and open "
            "today. One paid model call within the maintainer's daily cap; only when a person asks.",
            mutating=True, memo_errors=True)
def preview(ctx: ToolContext) -> dict[str, Any]:
    return maintainer.preview(ctx.store, ctx.config, ctx.env)
