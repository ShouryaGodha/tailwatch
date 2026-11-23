"""The daily link enrichment: verified links fill empty kinds only, and everyone looked at is marked."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from tailwatch import util
from tailwatch.agent.providers.scripted import turn
from tailwatch.connectors.http import Http, fixture_transport
from tailwatch.enrich import links
from tailwatch.enrich import settings as tunables

HOME = "https://ada.example"
GH = "https://github.com/adal"
USER = {"login": "adal", "name": "Ada Lovelace", "company": "", "bio": "looms and numbers", "blog": "https://ada.example"}
HTTP = Http(fixture_transport({"https://api.github.com/users/adal": USER}))


def link(kind: str, url: str, quote: str) -> dict[str, Any]:
    return {"kind": kind, "url": url, "verdict": "verified", "confidence": 0.9, "quote": quote, "quote_url": url,
            "facts": {"name": True, "organization": False, "role": False, "dates": False, "cross_link": True}, "reason": ""}


def enricher(request: Any) -> Any:
    done = sum(1 for m in request.messages if m["role"] == "assistant" and m.get("tool_calls"))
    if done == 0:
        return turn("", ("github_user", {"login": "adal"}))
    return {"name": "Ada Lovelace", "organization": "Analytical Engines", "title": "", "summary": "", "concerns": [],
            "links": [link("github", GH, "looms and numbers"),
                      link("homepage", "https://ada.example", "looms and numbers")]}


def test_verified_links_fill_empty_kinds_only_and_mark_the_person(store: Any, config: Any, model: Any) -> None:
    util.set_now(util.now() - timedelta(days=30))
    person_id, _ = store.add_person("Ada Lovelace", organization="Analytical Engines", links={"homepage": HOME})
    full_id, _ = store.add_person("Grace Hopper", links={"homepage": "https://grace.example", "github": "https://github.com/grace"})
    util.set_now(None)
    adapter = model(enricher)
    counts = links.enrich_links(store, config, {}, http=HTTP, log=None)
    assert counts["people"] == 1 and counts["links_added"] == 1  # Grace has two links: not thin, not new
    person = store.person(person_id)
    assert person["links"] == {"homepage": HOME, "github": GH}  # the homepage was not replaced
    assert person["links_enriched_at"] and not store.person(full_id)["links_enriched_at"]
    seed = adapter.requests[0].messages[0]["content"]
    assert '"mode": "fill_links"' in seed and '"known_links": {"homepage": "https://ada.example"}' in seed
    assert links.enrich_links(store, config, {}, http=HTTP, log=None)["people"] == 0  # nobody is paid for twice


def test_newcomers_come_first_and_the_backfill_follows_the_setting(store: Any, config: Any, model: Any) -> None:
    util.set_now(util.now() - timedelta(days=30))
    old = [store.add_person(f"Old {i}", links={"homepage": f"https://old{i}.example"})[0] for i in range(3)]
    util.set_now(None)
    new, _ = store.add_person("New Person", links={"homepage": "https://new.example", "github": "https://github.com/newp"})
    tunables.change(store, "links_per_day", 1, by="U1")
    chosen = [p["id"] for p in links.people_to_look_at(store, config)]
    assert chosen == [new, old[0]]


def test_the_one_person_form_fills_links_for_the_custodian(store: Any, config: Any, model: Any) -> None:
    from tailwatch.agent import loop
    from tailwatch.enrich import agent
    from tailwatch.enrich.tools import Toolkit

    person_id, _ = store.add_person("Ada Lovelace", organization="Analytical Engines", links={"homepage": HOME})
    model(enricher)
    kit = Toolkit(store, config, {}, budget=loop.Budget(0.2), http=HTTP)
    result = agent.enrich_person(store, config, {}, person_id=person_id, mode="fill_links", toolkit=kit)
    assert result["links_added"] == {"github": GH} and store.person(person_id)["links_enriched_at"]
    assert agent.enrich_person(store, config, {}, person_id=999, mode="fill_links")["error"] == "no person 999"
