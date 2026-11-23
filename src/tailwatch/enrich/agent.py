"""The enricher: one routine (``enrich_person``) that finds and proves a person's profiles.

The model works in the one loop with the read-only toolkit (``enrich.tools``), the worker
prompt (``skills/enrichment/agent.md``) plus the learned rules, and a per-person budget that
tools and model share. Its answer is a verdict per link with a verbatim quote; ``verify.check``
then lowers what the evidence does not carry. Nothing here writes to the list: the callers
(``candidates``, ``links``) decide what to do with the result.

Two seed modes:

- ``report``: a reported move about someone not on the list. Expected organizations are the
  ones left and joined.
- ``fill_links``: a tracked person's missing links. The expected organization is the one the
  list holds, and the links already on the row are proven anchors for cross-links.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tailwatch import links as link_rules
from tailwatch import util
from tailwatch.agent import loop, prompts, routines
from tailwatch.enrich import names, verify
from tailwatch.enrich import settings as tunables
from tailwatch.enrich.tools import Toolkit

WORKER_PROMPT = "enrichment/agent.md"
VERDICTS = ["verified", "unverified", "rejected"]
FACTS = ("name", "organization", "role", "dates", "cross_link")
LINK_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["kind", "url", "verdict", "confidence", "quote", "quote_url", "facts", "reason"],
    "properties": {
        "kind": {"type": "string", "enum": list(link_rules.KINDS)},
        "url": {"type": "string"},
        "verdict": {"type": "string", "enum": VERDICTS},
        "confidence": {"type": "number"},
        "quote": {"type": "string"},
        "quote_url": {"type": "string"},
        "facts": {"type": "object", "additionalProperties": False, "required": list(FACTS),
                  "properties": {f: {"type": "boolean"} for f in FACTS}},
        "reason": {"type": "string"},
    },
}
ENRICHMENT_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["name", "organization", "title", "summary", "links", "concerns"],
    "properties": {
        "name": {"type": "string"},
        "organization": {"type": "string"},
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "links": {"type": "array", "items": LINK_SCHEMA},
        "concerns": {"type": "array", "items": {"type": "string"}},
    },
}


class EnrichError(RuntimeError):
    """The enricher gave no usable answer. ``usd`` is what the failed run still spent."""

    def __init__(self, message: str, *, usd: float = 0.0) -> None:
        super().__init__(message)
        self.usd = usd


def instructions(store: Any) -> str:
    text = prompts.path_of("skills", WORKER_PROMPT).read_text(encoding="utf-8").strip()
    learned = tunables.rules(store)
    if learned:
        text += "\n\n# Learned rules (versioned; a person applied each)\n" + "\n".join(f"- {r}" for r in learned)
    return text


def aliases_for(store: Any, config: Any) -> dict[str, set[str]]:
    return names.alias_table(dict(getattr(getattr(config, "priority", None), "aliases", {}) or {}), tunables.aliases(store))


def seed_for_report(candidate: Mapping[str, Any], identity: Mapping[str, Any]) -> dict[str, Any]:
    """The first message for a reported move: who, what moved, the report and its links."""
    subject = candidate.get("subject") or {}
    handles = [str(h) for h in identity.get("handles") or [] if str(h).strip()]
    given = link_rules.normalize_all(_links_of(handles))
    return {
        "mode": "report",
        "today": util.iso()[:10],
        "person": {"name": identity.get("name") or "", "left": identity.get("left") or "",
                   "joined": identity.get("joined") or "", "role": identity.get("role") or "", "handles": handles},
        "report": {"feed": candidate.get("feed") or "", "item": candidate.get("item_id") or "",
                   "url": candidate.get("url") or "", "text": util.clip(candidate.get("text"), 4000),
                   "author": subject.get("author") or "", "published_at": subject.get("published_at") or ""},
        "given_links": given,
        "expected_organizations": [o for o in (identity.get("left"), identity.get("joined")) if o],
    }


def seed_for_person(person: Mapping[str, Any]) -> dict[str, Any]:
    """The first message for a tracked person's missing links."""
    known = dict(person.get("links") or {})
    return {
        "mode": "fill_links",
        "today": util.iso()[:10],
        "person": {"id": person["id"], "name": person.get("name") or "", "organization": person.get("organization") or "",
                   "title": person.get("title") or "", "location": person.get("location") or ""},
        "known_links": known,
        "missing_kinds": [k for k in ("linkedin", "github", "x", "homepage", "semantic_scholar", "bluesky", "scholar")
                          if k not in known],
        "expected_organizations": [person["organization"]] if person.get("organization") else [],
    }


def _links_of(values: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for value in values:
        kind = link_rules.guess_kind(value)
        out.setdefault(kind, value)
    return out


def enrich_person(store: Any, config: Any, env: Mapping[str, str], seed: dict[str, Any] | None = None, *,
                  toolkit: Toolkit | None = None, person_id: int | None = None, mode: str = "") -> dict[str, Any]:
    """Run the enricher on one seed and check its answer. Writes nothing but the run log.

    Returns ``{outcome: verified|needs_review, reason, primary, links (checked), verified
    (kind -> url), name, organization, title, summary, concerns, shared_with, removed, usd,
    model_usd, tool_usd, stopped, tool_calls}``. Raises ``EnrichError`` when the model gave no
    answer (the failed run is logged with its spend).

    Called with ``person_id`` (and ``mode="fill_links"``) instead of a seed, it is the one-person
    link fill (``links.fill_person``): verified links fill the person's empty kinds and the result
    carries ``links_added`` (and ``added``, the same kind -> url dict); a failure comes back as ``{"error": ...}``."""
    if seed is None:
        if person_id is None or mode not in ("", "fill_links"):
            raise ValueError("enrich_person needs a seed, or a person_id in fill_links mode")
        from tailwatch.enrich import links

        person = store.person(int(person_id))
        if person is None:
            return {"error": f"no person {person_id}"}
        try:
            filled = links.fill_person(store, config, env, person, toolkit=toolkit)
            return {**filled, "added": filled["links_added"]}
        except EnrichError as error:
            return {"error": str(error), "usd": error.usd}
    per_person = float(getattr(getattr(config, "budgets", None), "enrich_per_person", 0.20))
    toolkit = toolkit or Toolkit(store, config, env, budget=loop.Budget(per_person))
    names_offered = toolkit.available()
    tools = loop.StepTools(specs=toolkit.specs(names_offered), evidence=frozenset(names_offered) & Toolkit.EVIDENCE)
    spec = routines.spec_for("enrich_person", config, instructions=instructions(store), tools=tools,
                             schema=ENRICHMENT_SCHEMA, budget=toolkit.budget, max_tokens=6000)
    who = str((seed.get("person") or {}).get("name") or "?")
    subject = f"person:{seed['person']['id']}" if seed.get("mode") == "fill_links" else f"report:{who}"
    try:
        run = routines.run_routine(store, spec, {**seed, "tools": names_offered}, subject=subject, env=env,
                                   execute=toolkit.call, tool_usd=lambda: toolkit.usd)
    except loop.LoopError as error:
        failed = loop.failed_run(error)
        raise EnrichError(str(error), usd=round(toolkit.usd + (failed.cost_usd if failed else 0.0), 6)) from error
    answer = run.answer if isinstance(run.answer, dict) else {}
    floors = tunables.floors(store)
    checked = verify.check(answer, seed, toolkit, store=store, floors=floors, aliases=aliases_for(store, config))
    verified = verify.best_links(checked["links"])
    outcome, reason, primary = decide(config, env, checked, verified)
    if run.stopped and outcome != "verified":
        reason = f"{reason}; the run stopped early ({run.stopped})"
    return {
        "outcome": outcome, "reason": reason, "primary": primary, "links": checked["links"],
        "verified": {k: v["url"] for k, v in verified.items()},
        "name": str(answer.get("name") or who), "organization": str(answer.get("organization") or ""),
        "title": str(answer.get("title") or ""), "summary": util.clip(answer.get("summary"), 600),
        "concerns": [str(c) for c in answer.get("concerns") or []][:8],
        "shared_with": checked["shared_with"], "removed": checked["removed"],
        "usd": round(run.cost_usd + toolkit.usd, 6), "model_usd": run.cost_usd, "tool_usd": toolkit.usd,
        "stopped": run.stopped, "tool_calls": [{"tool": t["tool"], "args": t["args"], "ok": t["ok"]} for t in run.trace][:40],
        "ledger": toolkit.ledger[-40:],
    }


def checkable_kinds(config: Any, env: Mapping[str, str]) -> list[str]:
    """Link kinds a checker reads, LinkedIn first when it is on."""
    from tailwatch.connectors.registry import enabled_checkers

    kinds = list(enabled_checkers(config, env))
    return sorted(kinds, key=lambda k: (k != "linkedin", kinds.index(k)))


def decide(config: Any, env: Mapping[str, str], checked: Mapping[str, Any],
           verified: Mapping[str, Mapping[str, Any]]) -> tuple[str, str, str]:
    """(outcome, reason, primary kind). Verified needs a proven link of a kind some enabled
    checker reads, so the newcomer is watched from the next pass; LinkedIn is the primary when
    it is on. A link already held by someone else sends the whole candidate to review."""
    if checked.get("shared_with"):
        ids = ", ".join(f"#{i}" for i in checked["shared_with"])
        return "needs_review", f"a link is already on the list ({ids})", ""
    kinds = checkable_kinds(config, env)
    primary = next((k for k in kinds if k in verified), "")
    if primary:
        return "verified", f"{primary} verified", primary
    if verified:
        return "needs_review", f"only {', '.join(verified)} verified, and no checker reads it", ""
    tried = [link for link in checked.get("links") or [] if link.get("claimed") == "verified"]
    if tried:
        first = tried[0]
        return "needs_review", f"{first['kind']} not proven: " + "; ".join(first.get("notes") or ["lowered"]), ""
    return "needs_review", "no profile was proven", ""
