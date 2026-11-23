"""Who goes first in the digest: one model call per digest over every flagged person, with a
deterministic fallback (the pack's signal order, the owner's tiers, the judge's category and
priority) whenever the call is off or fails. Learned rules for the ranker are appended to its
prompt (``RANKING:RULES``)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tailwatch.agent import prompts, routines
from tailwatch.pipeline.packs import Pack

RULES_KEY = "RANKING:RULES"
MAX_RANKED = 60
SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["ranked"],
    "properties": {"ranked": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["person_id", "score", "worth", "strength", "why"],
        "properties": {"person_id": {"type": "integer"}, "score": {"type": "integer"}, "worth": {"type": "string"},
                       "strength": {"type": "string", "enum": ["strong", "fair", "weak"]}, "why": {"type": "string"}}}}},
}


def tier_of(row: Mapping[str, Any], config: Any) -> int:
    """0 for the first tier, 1 for the second..., len(tiers) for nobody's."""
    tiers = config.priority.tiers if config is not None else []
    names = " ".join(str(x or "") for x in (row.get("person_org"), (row.get("snapshot") or {}).get("organization"),
                                             (row.get("now") or {}).get("organization"))).lower()
    for index, tier in enumerate(tiers):
        for org in tier.orgs:
            words = [org, *config.priority.aliases.get(org, [])]
            if any(w.lower() in names for w in words if w):
                return index
    return len(tiers)


def fallback(rows: list[dict[str, Any]], pack: Pack, config: Any) -> list[dict[str, Any]]:
    def key(row: Mapping[str, Any]) -> tuple[Any, ...]:
        in_priority = 0 if row.get("category") in pack.priority_categories else 1
        return (tier_of(row, config), pack.order(row["signal"]), in_priority, -int(row.get("priority") or 0), row["observed_at"])

    return sorted(rows, key=key)


def instructions(store: Any, config: Any, pack: Pack) -> str:
    tiers = config.priority.tiers
    text = prompts.render(prompts.text("rank.md"), {
        "DIGEST_CAP": config.digest.cap,
        "TIERS": "\n".join(f"{i + 1}. {t.name}: {', '.join(t.orgs)}" for i, t in enumerate(tiers)) or "(no tiers set)",
        "PRIORITY_CATEGORIES": ", ".join(pack.priority_categories) or "none set",
        "SIGNAL_ORDER": " > ".join(pack.names),
    })
    rules = store.get_setting(RULES_KEY, []) if store is not None else []
    if rules:
        text += "\n\n## Learned rules (a person applied each)\n" + "\n".join(f"- {r}" for r in rules)
    return text


def seed_of(store: Any, row: Mapping[str, Any]) -> dict[str, Any]:
    history = []
    for read in store.latest_reads(row["person_id"]):
        for position in (read["content"].get("positions") or [])[:8]:
            history.append(f"{position.get('title')} at {position.get('company')}, {position.get('start') or '?'} to "
                           f"{'now' if position.get('current') else position.get('end') or '?'}")
    person = store.person(row["person_id"]) or {}
    return {"person_id": row["person_id"], "name": row["person_name"], "title": row.get("person_title"),
            "organization": row.get("person_org"), "history": history[:12], "tags": row.get("person_tags") or [],
            "intel": [i["text"] for i in store.intel_for(person)],
            "signal": {"label": row["signal"], "summary": row["summary"], "context": row["context"], "confidence": row["confidence"],
                       "category": row["category"], "priority": row["priority"], "source": row["kind"], "seen": row["observed_at"][:10],
                       "flags": row.get("flags") or [], **({"landed": row["landed"]} if row.get("landed") else {})}}


def rank(store: Any, config: Any, env: Mapping[str, str], rows: list[dict[str, Any]], pack: Pack) -> tuple[list[dict[str, Any]], str]:
    """Rows best first, each with ``why`` when the agent ranked it. Returns (rows, how)."""
    ordered = fallback(rows, pack, config)
    if not config.switches.rank or len(rows) <= 1:
        return ordered, "fallback"
    head = ordered[:MAX_RANKED]
    spec = routines.spec_for("rank_cards", config, instructions=instructions(store, config, pack), schema=SCHEMA,
                             final_call=False, max_tokens=6000)
    try:
        run = routines.run_routine(store, spec, [seed_of(store, r) for r in head], subject=f"{len(head)} people", env=env)
    except Exception:
        return ordered, "fallback"
    by_id = {r["person_id"]: r for r in head}
    ranked: list[dict[str, Any]] = []
    for entry in (run.answer or {}).get("ranked", []):
        row = by_id.pop(int(entry.get("person_id") or 0), None)
        if row is not None:
            ranked.append({**row, "why": str(entry.get("why") or "")[:200], "score": entry.get("score")})
    rest = [r for r in ordered if r["person_id"] in by_id] + ordered[MAX_RANKED:]
    return ranked + rest, "agent"
