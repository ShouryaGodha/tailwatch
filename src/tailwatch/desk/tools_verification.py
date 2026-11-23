"""The verification skill's desk tools: the card reviews and the editor on request. A person's
decisions on a card go through the core ``decide`` tool, which calls ``desk.verify.decide``."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tailwatch import util
from tailwatch.agent.registry import P, Toolbox, ToolContext
from tailwatch.store.cards import REVIEW_STATES

TOOLS = Toolbox("verification")
LIST_CAP = 30


def _brief(review: Mapping[str, Any]) -> dict[str, Any]:
    answer = review.get("answer") or {}
    return {"id": f"#R{review['id']}", "state": review.get("state"), "person": review.get("person_name") or (answer.get("card") or {}).get("name"),
            "person_id": review.get("person_id"), "signal": review.get("signal"), "summary": review.get("summary"),
            "editor_said": answer.get("editor_verdict"), "verdict": answer.get("verdict"), "failed": answer.get("failed") or [],
            "note": util.clip(review.get("note") or answer.get("note"), 300), "decision": review.get("decision") or {},
            "updated": str(review.get("updated_at") or "")[:16]}


@TOOLS.tool("cards", "The card reviews in one state, newest first (at most 30): #R id, the person, the signal, the summary, "
            "the editor's verdict, the floors that failed, the note and any decision.", evidence=True,
            state=P("string", "which reviews; held by default", required=False, enum=REVIEW_STATES))
def cards(ctx: ToolContext, state: str = "held") -> dict[str, Any]:
    if state not in REVIEW_STATES:
        return {"error": f"state is one of {', '.join(REVIEW_STATES)}"}
    return {"state": state, "cards": [_brief(r) for r in ctx.store.reviews(state=state, limit=LIST_CAP)]}


@TOOLS.tool("card", "One card review in full: the card, the change behind it, the editor's answer with its quotes, every "
            "floor's result, the issues it opened, the landing or repair code applied, the decision so far.", evidence=True,
            review_id=P("integer", "the number of #R12 is 12"))
def card(ctx: ToolContext, review_id: int) -> dict[str, Any]:
    review = ctx.store.review(int(review_id))
    if review is None:
        return {"error": f"no card #R{review_id}"}
    answer = dict(review.get("answer") or {})
    answer.pop("transcript", None)
    row = ctx.store.card_row(int(review["verdict_id"])) or {}
    events = ctx.store.events_of(int(review["observation_id"]))
    return {"id": f"#R{review['id']}", "state": review["state"], "rounds": review.get("rounds"), "note": review.get("note"),
            "decision": review.get("decision") or {}, "floors": review.get("floors") or {}, "answer": answer,
            "verdict": {k: row.get(k) for k in ("id", "signal", "summary", "context", "reason", "confidence", "now", "kind",
                                                "source_url", "observed_at", "person_name")},
            "changes": [{"label": e["label"], "before": util.clip(e["before_text"], 600), "after": util.clip(e["after_text"], 600)}
                        for e in events]}


@TOOLS.tool("verify_now", "Run the editor again now on one card (pending, held or fixing) and report what it found. It "
            "spends from the editor's budget.", mutating=True, evidence=True,
            review_id=P("integer", "the number of #R12 is 12"))
def verify_now(ctx: ToolContext, review_id: int) -> dict[str, Any]:
    from tailwatch.desk import verify

    result = verify.verify_one(ctx.store, ctx.config, ctx.env, int(review_id),
                               toolkit_factory=ctx.extra.get("toolkit_factory"))
    if not result.get("error"):
        review = ctx.store.review(int(review_id)) or {}
        result = {**result, "id": f"#R{review_id}", "note": review.get("note"),
                  "quotes": (review.get("answer") or {}).get("quotes") or []}
    return result
