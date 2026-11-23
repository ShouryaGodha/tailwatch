"""Shared builders for the editor, floors and verification tests (no tests here): a person with a
fake profile source, a judged change, a fake enrichment toolkit and an editor answer."""

from __future__ import annotations

import json
from typing import Any

from conftest import verdict
from tailwatch import util

BEFORE = {"bio": "Researcher at Analytical Engines", "organization": "Analytical Engines"}
AFTER = {"bio": "I left Analytical Engines last week to start something new", "organization": "Difference Labs"}
QUOTE = "I left Analytical Engines last week"


def card(store: Any, name: str = "Ada Lovelace", *, signal: str = "left", kind: str = "fake", before: dict | None = None,
         after: dict | None = None, confidence: float = 0.9, url: str = "", **overrides: Any) -> dict[str, Any]:
    """A person, two reads of one source (the second with a change) and a verdict on it. Returns ids."""
    slug = name.split()[0].lower()
    url = url or f"https://{slug}.example"
    person_id, _ = store.add_person(name, organization="Analytical Engines", title="Researcher",
                                    links={"homepage": url} if not kind.startswith("feed:") else {})
    source_id = store.ensure_source(person_id, kind, url, cadence_hours=24)
    source = store.source(source_id)
    snapshot = {"name": name, "organization": "Analytical Engines", "title": "Researcher"}
    first = store.record_check(source, status="ok", content=before or BEFORE, first_read=True, person_snapshot=snapshot)
    observation_id = store.record_check(source, status="ok", content=after or AFTER, previous_id=first, person_snapshot=snapshot,
                                        changes=[{"label": "bio changed", "before": (before or BEFORE).get("bio", ""),
                                                  "after": (after or AFTER).get("bio", "")}])
    now = overrides.pop("now", {"organization": "Difference Labs", "title": "Founder", "since": ""})
    verdict_id = store.save_verdict(observation_id, person_id, verdict(signal, confidence=confidence, now=now, **overrides))
    return {"person_id": person_id, "source_id": source_id, "observation_id": observation_id, "verdict_id": verdict_id,
            "url": url, "previous_id": first}


def answer(verdict_: str = "confirm", *, quote: str = QUOTE, url: str = "", confidence: float = 0.9, **extra: Any) -> dict[str, Any]:
    base = {"verdict": verdict_, "confidence": confidence, "quotes": [{"url": url, "text": quote}] if quote else [],
            "fields": {"organization": "", "title": ""}, "now_at": None, "note": "checked", "issues": [], "drop_reason": ""}
    return {**base, **extra}


def seed_of(request: Any) -> dict[str, Any]:
    return json.loads(request.messages[0]["content"])


class FakeToolkit:
    """The shape of ``tailwatch.enrich.tools.Toolkit``: specs, call, EVIDENCE, texts, urls, usd."""

    EVIDENCE = frozenset({"web_read", "web_search", "linkedin_profile"})
    KNOWN = ("web_search", "web_read", "linkedin_profile")

    def __init__(self, budget: Any, pages: dict[str, str] | None = None, cost: float = 0.0) -> None:
        self.budget, self.pages, self.cost = budget, pages or {}, cost
        self.texts: dict[str, str] = {}
        self.urls: set[str] = set()
        self.usd = 0.0
        self.calls: list[str] = []

    def specs(self, names: Any) -> list[dict[str, Any]]:
        schema = {"type": "object", "properties": {"url": {"type": "string"}}, "required": [], "additionalProperties": False}
        return [{"name": n, "description": n, "input_schema": schema} for n in names if n in self.KNOWN]

    def call(self, name: str, args: dict[str, Any]) -> Any:
        self.calls.append(name)
        self.usd = round(self.usd + self.cost, 6)
        self.budget.charge(self.cost)
        url = str(args.get("url") or "")
        text = self.pages.get(url, "")
        self.texts[url] = text
        self.urls.add(url)
        return {"url": url, "text": text}


def factory(pages: dict[str, str] | None = None, made: list[FakeToolkit] | None = None, cost: float = 0.0) -> Any:
    def make(store: Any, config: Any, env: Any, *, budget: Any) -> FakeToolkit:
        toolkit = FakeToolkit(budget, pages, cost)
        if made is not None:
            made.append(toolkit)
        return toolkit

    return make


def spend(store: Any, usd: float) -> None:
    """A verify_card run that cost ``usd`` today."""
    from tailwatch.agent.loop import Run

    run_id = store.open_run("verify_card", subject="earlier")
    store.finish_run(run_id, Run(cost_usd=usd), state="done")


__all__ = ["AFTER", "BEFORE", "QUOTE", "FakeToolkit", "answer", "card", "factory", "seed_of", "spend", "util"]
