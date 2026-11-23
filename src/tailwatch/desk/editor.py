"""The editor: the agent that checks one card before the digest carries it.

A bounded tool loop (the ``verify_card`` routine) with read-only tools: the enrichment
toolkit's search and read tools behind a whitelist (a paid LinkedIn pull only for a card of the
pack's first signal whose newest LinkedIn read is older than a day, while the linkedin
connector is on) and three free reads of the store (``latest_read``, ``judge_input``,
``prior_cards``). It answers confirm, fix, hold or drop with verbatim quotes; the floors
(``desk.floors``) then lower that answer where the evidence does not hold. The editor writes
nothing; ``desk.verify`` records the outcome.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from tailwatch import util
from tailwatch.agent import loop, prompts, routines
from tailwatch.agent.loop import StepTools
from tailwatch.desk import floors
from tailwatch.pipeline import digest, judge, packs

PROMPT = "verification/agent.md"
RULES_KEY = "VERIFICATION:RULES"
ENRICH_TOOLS = ("web_search", "web_read", "github_user", "x_profile", "x_posts", "bluesky_profile", "scholar_search",
                "list_lookup", "linkedin_profile")
OWN_TOOLS = ("latest_read", "judge_input", "prior_cards")
PULL_AGE_HOURS = 24
TEXT_CAP = 12_000
JUDGE_INPUT_CAP = 20_000
TRANSCRIPT_CAP = 40
DROP_REASONS = ("", "flag_only", "intern", "change_gone", "duplicate", "not_a_signal")

_text = {"type": "string"}
_role = {"type": "object", "additionalProperties": False, "required": ["organization", "title", "since"],
         "properties": {"organization": _text, "title": _text, "since": _text}}
SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["verdict", "confidence", "quotes", "fields", "now_at", "note", "issues", "drop_reason"],
    "properties": {
        "verdict": {"type": "string", "enum": ["confirm", "fix", "hold", "drop"]},
        "confidence": {"type": "number"},
        "quotes": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["url", "text"],
                                              "properties": {"url": _text, "text": _text}}},
        "fields": {"type": "object", "additionalProperties": False, "required": ["organization", "title"],
                   "properties": {"organization": _text, "title": _text}},
        "now_at": {"anyOf": [_role, {"type": "null"}]},
        "note": _text,
        "issues": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["kind", "detail"],
                                              "properties": {"kind": _text, "detail": _text}}},
        "drop_reason": {"type": "string", "enum": list(DROP_REASONS)},
    },
}
OWN_SPECS = [
    {"name": "latest_read", "description": "The newest successful read of the card's source as text (with position records "
     "when the source has them) and the read before the change. Free. Start here.",
     "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    {"name": "judge_input", "description": "What the judge saw when it wrote this card: the person on record, the change, "
     "the reads of the other sources, the recent changes, the earlier signals. Free.",
     "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    {"name": "prior_cards", "description": "The person's cards a digest already carried, and their earlier signals. Free.",
     "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
]


class EditError(RuntimeError):
    """The editor gave no usable answer (the model failed, or the card is gone)."""


def aliases_of(config: Any) -> dict[str, str]:
    return floors.alias_map(dict(getattr(getattr(config, "priority", None), "aliases", {}) or {}))


def _checkers() -> dict[str, Any]:
    try:
        from tailwatch.connectors.registry import checker_classes

        return {k: cls() for k, cls in checker_classes().items()}
    except Exception:
        return {}


def _read(observation: Mapping[str, Any] | None, kind: str, checkers: Mapping[str, Any]) -> dict[str, Any]:
    if not observation:
        return {}
    content = observation.get("content") or {}
    positions = content.get("positions") if isinstance(content.get("positions"), list) else []
    return {"id": observation["id"], "read_at": observation.get("observed_at"), "content": content,
            "text": judge.describe(kind, content, checkers), "raw": util.clip(util.dumps(content), TEXT_CAP),
            "positions": [{k: p.get(k) for k in ("company", "title", "start", "end", "current")} for p in positions][:15],
            "name": str(content.get("name") or "") if isinstance(content, Mapping) else ""}


def card_for(store: Any, config: Any, review: Mapping[str, Any], *, checkers: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Everything the editor and the floors read for one card: the digest's row, the change,
    the latest read of the source and the read before the change (texts and positions), the
    person's posted cards in the repost window, who else holds the card's links."""
    row = store.card_row(int(review["verdict_id"]))
    if row is None:
        raise EditError(f"#R{review.get('id')}: verdict {review['verdict_id']} is gone")
    pack = packs.load(config.signals)
    checkers = checkers if checkers is not None else _checkers()
    kind = str(row["kind"])
    observation = store.observation(int(row["observation_id"])) or {}
    latest = store.last_ok_observation(int(row["source_id"]))
    previous = store.last_ok_observation(int(row["source_id"]), before_id=int(row["observation_id"]))
    snapshot = row.get("snapshot") or {}
    person_links = dict(row.get("person_links") or {})
    links = [(k, v) for k, v in person_links.items()] + ([(kind, row["source_url"])] if not judge.is_feed(kind) else [])
    since = util.ago(days=max(1, int(config.digest.repost_days or 0)))
    latest_read = _read(latest, kind, checkers)
    first = pack.names[0]
    linkedin = store.latest_kind_read(int(row["person_id"]), "linkedin")
    fresh = linkedin is not None and str(linkedin["observed_at"]) > util.ago(hours=PULL_AGE_HOURS)
    return {
        "review_id": review.get("id"), "verdict_id": row["id"], "person_id": row["person_id"], "observation_id": row["observation_id"],
        "source_id": row["source_id"], "kind": kind, "url": row["source_url"], "source_status": row["source_status"],
        "trusted": judge.trusted_kind(kind, config), "name": row["person_name"],
        "organization": snapshot.get("organization") or row["person_org"], "title": snapshot.get("title") or row["person_title"],
        "signal": row["signal"], "first_signal": first, "summary": row["summary"], "context": row["context"],
        "reason": row["reason"], "confidence": row["confidence"], "now": row.get("now") or {}, "category": row["category"],
        "signal_date": row["signal_date"], "same_as": row["same_as"], "supersedes": row["supersedes"],
        "flags": row.get("flags") or [], "observed_at": row["observed_at"], "links": person_links,
        "events": [{"label": e["label"], "before": util.clip(e["before_text"], 2000), "after": util.clip(e["after_text"], 2000),
                    "content_date": e["content_date"]} for e in store.events_of(int(row["observation_id"]))],
        "judged_content": observation.get("content") or {}, "latest": latest_read, "previous": _read(previous, kind, checkers),
        "profile_name": latest_read.get("name", ""),
        "posted": [p for p in store.posted_cards(int(row["person_id"]), since=since) if int(p["verdict_id"]) != int(row["id"])],
        "move_key": digest.move_key(row, pack), "holders": store.link_holders(int(row["person_id"]), links),
        "allow_linkedin": row["signal"] == first and not fresh and bool(config.connector("linkedin").enabled),
        "intel": [{"said": i["text"], "by": i["said_by"], "on": i["created_at"][:10]}
                  for i in store.intel_for(store.person(int(row["person_id"])) or {})],
        "today": util.iso()[:10], "_row": row,
    }


def seed(card: Mapping[str, Any]) -> dict[str, Any]:
    """The first message: the card as the digest would show it and the change behind it; the
    long texts come from the tools."""
    keep = ("review_id", "person_id", "name", "organization", "title", "signal", "summary", "context", "reason", "confidence",
            "now", "category", "signal_date", "kind", "url", "observed_at", "events", "links", "intel", "today")
    out = {k: card.get(k) for k in keep if card.get(k) not in (None, "", [], {})}
    out["latest_read_at"] = (card.get("latest") or {}).get("read_at")
    out["posted_before"] = len(card.get("posted") or [])
    out["linkedin_pull_allowed"] = bool(card.get("allow_linkedin"))
    return out


def instructions(store: Any, config: Any) -> str:
    pack = packs.load(config.signals)
    text = prompts.render(prompts.path_of("skills", PROMPT).read_text(encoding="utf-8"), {
        "PACK_DESCRIPTION": pack.description,
        "SIGNAL_LIST": "\n".join(f"- {s.name}: {s.describe}" for s in pack.signals),
        "FIRST_SIGNAL": pack.names[0],
        "REPOST_DAYS": config.digest.repost_days,
        "CONFIDENCE_FLOOR": f"{floors.confidence_floor(store):.2f}",
    })
    rules = store.get_setting(RULES_KEY, []) if store is not None else []
    if rules:
        text += "\n\n# Learned rules (versioned; a person applied each)\n" + "\n".join(f"- {r}" for r in rules)
    return text


def tools_for(store: Any, config: Any, card: Mapping[str, Any], toolkit: Any) -> tuple[StepTools, Callable[[str, dict[str, Any]], Any]]:
    """The editor's step tools and the executor: the whitelist, the LinkedIn gate, the store reads."""
    allowed = [n for n in ENRICH_TOOLS if n != "linkedin_profile" or card.get("allow_linkedin")]
    try:
        enrich_specs = list(toolkit.specs(allowed))
    except Exception:
        enrich_specs = []
    names = {s.get("name") for s in enrich_specs}
    evidence = frozenset((set(getattr(toolkit, "EVIDENCE", set())) & names) | set(OWN_TOOLS))
    step = StepTools(specs=[*enrich_specs, *OWN_SPECS], evidence=evidence)

    def execute(name: str, args: dict[str, Any]) -> Any:
        if name == "latest_read":
            return _latest_read(card)
        if name == "judge_input":
            return _judge_input(store, config, card)
        if name == "prior_cards":
            return _prior_cards(store, card)
        if name == "linkedin_profile" and not card.get("allow_linkedin"):
            return {"error": "a paid LinkedIn pull is not allowed for this card; read latest_read or search instead"}
        if name not in names:
            return {"error": f"{name} is not one of the editor's tools"}
        return toolkit.call(name, args)

    return step, execute


def _latest_read(card: Mapping[str, Any]) -> dict[str, Any]:
    latest, previous = card.get("latest") or {}, card.get("previous") or {}
    if not latest:
        return {"error": "no successful read of this source is stored"}
    return {"source": card.get("kind"), "url": card.get("url"), "read_at": latest.get("read_at"),
            "is_the_read_the_judge_saw": int(latest.get("id") or 0) == int(card.get("observation_id") or 0),
            "text": util.clip(latest.get("text"), TEXT_CAP), "positions": latest.get("positions") or [],
            "before_the_change": {"read_at": previous.get("read_at"), "text": util.clip(previous.get("text"), TEXT_CAP // 2),
                                  "positions": previous.get("positions") or []} if previous else None}


def _judge_input(store: Any, config: Any, card: Mapping[str, Any]) -> Any:
    observation = store.observation(int(card["observation_id"])) or {}
    row = card.get("_row") or {}
    check = {**observation, "kind": card.get("kind"), "url": card.get("url"), "events": store.events_of(int(card["observation_id"])),
             "source_id": card.get("source_id")}
    try:
        seen = judge.judge_input(store, check, config, _checkers())
    except Exception as error:
        return {"error": f"the judge's input could not be rebuilt: {type(error).__name__}"}
    seen["verdict"] = {k: row.get(k) for k in ("signal", "summary", "context", "reason", "confidence", "now", "signal_date")}
    text = util.dumps(seen)
    return seen if len(text) <= JUDGE_INPUT_CAP else {"truncated": text[:JUDGE_INPUT_CAP]}


def _prior_cards(store: Any, card: Mapping[str, Any]) -> dict[str, Any]:
    posted = [{"verdict_id": p["verdict_id"], "signal": p["signal"], "summary": p["summary"], "posted": str(p["at"])[:10],
               "move": p["move_key"]} for p in card.get("posted") or []]
    earlier = [{"id": v["id"], "signal": v["signal"], "summary": v["summary"], "seen": v["created_at"][:10], "delivered": bool(v.get("posted"))}
               for v in store.prior_alerts(int(card["person_id"]), since=util.ago(days=365)) if int(v["id"]) != int(card["verdict_id"])]
    return {"posted": posted, "earlier_signals": earlier[:10], "this_move": card.get("move_key")}


def texts_of(card: Mapping[str, Any], toolkit: Any) -> dict[str, str]:
    """Every text a quote may come from: what the tools fetched and the stored latest read."""
    texts = {str(k): str(v) for k, v in (getattr(toolkit, "texts", None) or {}).items()}
    latest = card.get("latest") or {}
    if latest:
        url = str(card.get("url") or "latest")
        texts[url] = f"{latest.get('text') or ''}\n{latest.get('raw') or ''}"
    return texts


def issues_for(card: Mapping[str, Any], answer: Mapping[str, Any], results: Mapping[str, Mapping[str, str]],
               repair: Mapping[str, str] | None) -> list[dict[str, Any]]:
    """The data issues a fix asks the custodian for: one per failed floor that a playbook
    repairs, the editor's own issues, and a correction of the card's fields."""
    person, source, review = card.get("person_id"), card.get("source_id"), card.get("review_id")
    base = {"person_id": person, "source_id": source, "review_id": review, "kind": card.get("kind")}
    out: list[dict[str, Any]] = []
    for name in floors.failed(results):
        kind = str(results[name].get("issue") or name)
        out.append({"kind": kind, "subject": f"source:{source}" if kind in ("wrong_person", "change_gone") else f"person:{person}",
                    "playbook": floors.playbook_of(results[name]), "detail": results[name].get("detail", ""), "args": base,
                    "floor": name})
    for issue in answer.get("issues") or []:
        kind = str((issue or {}).get("kind") or "other")
        if any(o["kind"] == kind for o in out):
            continue
        out.append({"kind": kind, "subject": f"person:{person}", "playbook": floors.PLAYBOOK_FOR.get(kind, ""),
                    "detail": str((issue or {}).get("detail") or ""), "args": base})
    fields = answer.get("fields") or {}
    if fields.get("organization") or fields.get("title"):
        quote = next((q["text"] for q in answer.get("quotes") or []
                      if any(floors.norm_text(f) and floors.norm_text(f) in floors.norm_text(q.get("text")) for f in fields.values())),
                     (answer.get("quotes") or [{}])[0].get("text", ""))
        out.append({"kind": "card_fields", "subject": f"review:{review}", "playbook": "fix_card_fields",
                    "detail": f"the card says {card.get('title') or '?'} at {card.get('organization') or '?'}; the evidence says "
                              f"{fields.get('title') or card.get('title') or '?'} at {fields.get('organization') or card.get('organization') or '?'}",
                    "args": {**base, "organization": str((repair or fields).get("organization") or ""),
                             "title": str((repair or fields).get("title") or ""), "quote": quote,
                             "from_positions": bool(repair)}})
    return out[:5]


def edit(store: Any, config: Any, env: Mapping[str, str], review: Mapping[str, Any], *, toolkit: Any,
         card: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Run the editor on one card and lower its answer by the floors. Writes only its run row.
    Returns the final verdict with the editor's own, the floors' results, the issues, the
    repair and the landing code may apply, the cost and a short transcript."""
    card = dict(card) if card is not None else card_for(store, config, review)
    budget = getattr(toolkit, "budget", None) or loop.Budget(float(config.budgets.editor_per_card))
    step, execute = tools_for(store, config, card, toolkit)
    spec = routines.spec_for("verify_card", config, instructions=instructions(store, config), tools=step, schema=SCHEMA,
                             budget=budget, max_tokens=4000)
    try:
        run = routines.run_routine(store, spec, seed(card), subject=f"review:{review.get('id')}", env=env, execute=execute,
                                   tool_usd=lambda: float(getattr(toolkit, "usd", 0.0) or 0.0))
    except loop.LoopError as error:
        spent = loop.failed_run(error)
        raise EditError(f"{error} (spent ${(spent.cost_usd if spent else 0.0) + float(getattr(toolkit, 'usd', 0.0) or 0.0):.3f})") from error
    answer: dict[str, Any] = dict(run.answer) if isinstance(run.answer, dict) else {}
    if not answer.get("verdict"):
        raise EditError("the editor answered without a verdict")
    aliases = aliases_of(config)
    results = floors.run_all(card, answer, texts=texts_of(card, toolkit), floor=floors.confidence_floor(store), aliases=aliases)
    lowered = floors.lower(answer, results, drop_allowed=floors.allow_drop(card, answer, results))
    said, verdict = lowered["editor_verdict"], lowered["verdict"]
    fields = answer.get("fields") or {}
    wants_fields = bool(fields.get("organization") or fields.get("title"))
    repair = floors.structured_fix(card, fields, aliases=aliases) if said in ("confirm", "fix") and wants_fields else None
    if verdict == "confirm" and wants_fields and repair is None:
        verdict = "fix"  # a correction no position record carries: the custodian checks its quote first
    landed = None
    if said in ("confirm", "fix") and card.get("signal") == card.get("first_signal"):
        landed = floors.landing(card, answer.get("now_at"), aliases=aliases)
    issues = issues_for(card, answer, results, repair) if verdict == "fix" else []
    reason = "; ".join(f"{n}: {results[n]['detail']}" for n in lowered["failed"]) or util.clip(answer.get("note"), 300)
    return {"verdict": verdict, "editor_verdict": said, "confidence": answer.get("confidence"), "quotes": answer.get("quotes") or [],
            "fields": fields, "now_at": answer.get("now_at"), "note": str(answer.get("note") or ""),
            "drop_reason": str(answer.get("drop_reason") or "") if verdict == "drop" else "", "issues": issues,
            "editor_issues": answer.get("issues") or [], "floors": results, "failed": lowered["failed"], "reason": reason,
            "repair": repair if verdict == "confirm" else None, "landed": landed if verdict == "confirm" else None,
            "cost_usd": round(run.cost_usd + float(getattr(toolkit, "usd", 0.0) or 0.0), 6), "model": run.model,
            "stopped": run.stopped, "transcript": [{"tool": t["tool"], "args": t["args"], "ok": t["ok"]} for t in run.trace][:TRANSCRIPT_CAP]}
