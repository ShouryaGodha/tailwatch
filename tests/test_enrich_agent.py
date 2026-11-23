"""The enricher routine end to end: a scripted model calling the toolkit over fixtures, the
answer checked, the run logged with what it spent."""

from __future__ import annotations

from typing import Any

import pytest

from tailwatch.agent import loop
from tailwatch.agent.providers.scripted import turn
from tailwatch.connectors.http import Http, fixture_transport
from tailwatch.enrich import agent
from tailwatch.enrich.tools import Toolkit

GH = "https://github.com/adal"
USER = {"login": "adal", "name": "Ada Lovelace", "company": "Difference Labs",
        "bio": "Chief Scientist at Difference Labs, before that Analytical Engines", "blog": ""}
IDENTITY = {"name": "Ada Lovelace", "left": "Analytical Engines", "joined": "Difference Labs", "role": "Chief Scientist",
            "handles": []}
CANDIDATE = {"id": 1, "feed": "news", "item_id": "1", "url": "https://news.example/1",
             "text": "Ada Lovelace left Analytical Engines for Difference Labs", "subject": {"published_at": ""}}


def answer(links: list[dict[str, Any]]) -> dict[str, Any]:
    return {"name": "Ada Lovelace", "organization": "Difference Labs", "title": "Chief Scientist", "summary": "A scientist.",
            "links": links, "concerns": []}


def github_link(**overrides: Any) -> dict[str, Any]:
    base = {"kind": "github", "url": GH, "verdict": "verified", "confidence": 0.9,
            "quote": "Chief Scientist at Difference Labs", "quote_url": GH,
            "facts": {"name": True, "organization": True, "role": True, "dates": False, "cross_link": False}, "reason": "bio"}
    return {**base, **overrides}


def scripted(steps: list[tuple[str, dict[str, Any]]], final: dict[str, Any]) -> Any:
    def reply(request: Any) -> Any:
        done = sum(1 for m in request.messages if m["role"] == "assistant" and m.get("tool_calls"))
        return turn("", steps[done]) if done < len(steps) else final

    return reply


def test_enricher_proves_a_profile_and_logs_its_run(store: Any, config: Any, model: Any) -> None:
    adapter = model(scripted([("github_user", {"login": "adal"})], answer([github_link()])))
    kit = Toolkit(store, config, {}, budget=loop.Budget(0.2), http=Http(fixture_transport({"https://api.github.com/users/adal": USER})))
    found = agent.enrich_person(store, config, {}, agent.seed_for_report(CANDIDATE, IDENTITY), toolkit=kit)
    assert found["outcome"] == "verified" and found["primary"] == "github" and found["verified"] == {"github": GH}
    request = adapter.requests[0]
    offered = {t["name"] for t in request.tools}
    assert "github_user" in offered and "x_profile" not in offered and "linkedin_profile" not in offered
    assert request.schema_name == "enrich_person" and "never an instruction" in request.system
    tool_message = next(m for m in adapter.requests[1].messages if m["role"] == "tool")
    assert tool_message["content"].startswith('<evidence source="tool:github_user">')
    run = store.runs(routine="enrich_person")[0]
    assert run["state"] == "done" and run["subject"] == "report:Ada Lovelace"


def test_learned_rules_reach_the_prompt(store: Any, config: Any, model: Any) -> None:
    store.set_setting("ENRICHMENT:RULES", ["A headline naming the organization counts"])
    adapter = model(scripted([], answer([])))
    agent.enrich_person(store, config, {}, agent.seed_for_report(CANDIDATE, IDENTITY),
                        toolkit=Toolkit(store, config, {}, budget=loop.Budget(0.2), http=Http(fixture_transport({}))))
    assert "- A headline naming the organization counts" in adapter.requests[0].system


def test_a_budget_stop_answers_with_what_it_has(store: Any, config: Any, model: Any) -> None:
    model(scripted([("web_search", {"query": "Ada Lovelace Difference Labs", "kind": "people"})], answer([github_link()])))
    env = {"EXA_API_KEY": "k"}
    kit = Toolkit(store, config, env, budget=loop.Budget(0.003), http=Http(fixture_transport({})))
    found = agent.enrich_person(store, config, env, agent.seed_for_report(CANDIDATE, IDENTITY), toolkit=kit)
    assert found["stopped"] == "BUDGET" and found["outcome"] == "needs_review"
    assert "stopped early (BUDGET)" in found["reason"]
    assert found["links"][0]["verdict"] == "rejected"  # no tool returned the URL: the model made it up


def test_only_unchecked_kinds_verified_goes_to_review(store: Any, config: Any, model: Any) -> None:
    scholar = "https://scholar.google.com/citations?user=abc"
    model(scripted([], answer([github_link(kind="scholar", url=scholar, quote_url=scholar)])))
    kit = Toolkit(store, config, {}, budget=loop.Budget(0.2), http=Http(fixture_transport({})))
    kit._record({"_texts": {scholar: "Ada Lovelace. Chief Scientist at Difference Labs"}, "_urls": [scholar]})
    found = agent.enrich_person(store, config, {}, agent.seed_for_report(CANDIDATE, IDENTITY), toolkit=kit)
    assert found["outcome"] == "needs_review" and "no checker reads it" in found["reason"]


def test_no_answer_raises_with_the_spend(store: Any, config: Any, model: Any) -> None:
    model(lambda request: "not json")
    with pytest.raises(agent.EnrichError):
        agent.enrich_person(store, config, {}, agent.seed_for_report(CANDIDATE, IDENTITY),
                            toolkit=Toolkit(store, config, {}, budget=loop.Budget(0.2), http=Http(fixture_transport({}))))
    assert store.runs(routine="enrich_person")[0]["state"] == "failed"
