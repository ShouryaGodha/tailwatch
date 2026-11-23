"""The enricher's toolkit: caching (a replay is free), caps, the budget, ToolStop codes, and what
it keeps for the proof checks. Every provider answers from a fixture."""

from __future__ import annotations

from typing import Any

import pytest

from tailwatch.agent import loop
from tailwatch.connectors.http import Call, Http, Response, fixture_transport
from tailwatch.enrich.tools import SPECS, Toolkit

LINKEDIN = "https://www.linkedin.com/in/ada-lovelace"
EXA_SEARCH = {"results": [{"url": LINKEDIN, "title": "Ada Lovelace - Research Scientist - Analytical Engines | LinkedIn",
                           "highlights": ["Research Scientist at Analytical Engines since 2024."], "text": "Ada Lovelace. London."}],
              "costDollars": {"total": 0.005}}
APIFY_ITEM = [{"basic_info": {"fullname": "Ada Lovelace", "headline": "Research Scientist"},
               "experience": [{"title": "Research Scientist", "company": "Analytical Engines", "is_current": True,
                               "start_date": {"year": 2024, "month": 1}}]}]


def counting(routes: dict[str, Any]) -> tuple[Any, list[Call]]:
    calls: list[Call] = []
    inner = fixture_transport(routes)

    def transport(call: Call, timeout: float) -> Response:
        calls.append(call)
        return inner(call, timeout)

    return transport, calls


def kit(store: Any, config: Any, env: dict[str, str], transport: Any, limit: float = 0.20) -> Toolkit:
    return Toolkit(store, config, env, budget=loop.Budget(limit), http=Http(transport))


def test_web_search_records_evidence_and_a_replay_is_free(store: Any, config: Any) -> None:
    transport, calls = counting({"https://api.exa.ai/search": EXA_SEARCH})
    env = {"EXA_API_KEY": "k"}
    first = kit(store, config, env, transport)
    out = first.call("web_search", {"query": "Ada Lovelace Analytical Engines", "kind": "people"})
    assert out["results"][0]["url"] == LINKEDIN and "_texts" not in out
    assert first.usd == pytest.approx(0.005) and first.budget.spent == pytest.approx(0.005)
    assert first.returned(LINKEDIN) and first.quote_found("research scientist at Analytical Engines", LINKEDIN)
    assert first.profile_for(LINKEDIN)["name"] == "Ada Lovelace"
    assert calls[0].headers["x-api-key"] == "k" and calls[0].json["category"] == "people"

    replay = kit(store, config, {}, transport)  # no key needed for a cached answer
    replay.call("web_search", {"query": "Ada Lovelace Analytical Engines", "kind": "people"})
    assert len(calls) == 1 and replay.usd == 0.0 and replay.ledger[0]["cached"]
    assert replay.quote_found("Research Scientist at Analytical Engines", LINKEDIN)


def test_budget_out_raises_toolstop_before_the_call(store: Any, config: Any) -> None:
    transport, calls = counting({"https://api.exa.ai/search": EXA_SEARCH})
    tight = kit(store, config, {"EXA_API_KEY": "k"}, transport, limit=0.003)
    with pytest.raises(loop.ToolStop) as stop:
        tight.call("web_search", {"query": "Ada"})
    assert stop.value.code == "BUDGET" and not calls


def test_web_search_without_a_key_says_so_and_web_read_falls_back_to_a_plain_fetch(store: Any, config: Any) -> None:
    page = "<html><title>Ada</title><body><p>Ada Lovelace, Research Scientist at Analytical Engines.</p>" \
           "<a href='https://github.com/adal'>code</a></body></html>"
    transport, calls = counting({"https://ada.example": page})
    tools = kit(store, config, {}, transport)
    assert tools.call("web_search", {"query": "Ada"})["error"].startswith("NEED_KEY")
    read = tools.call("web_read", {"url": "https://ada.example"})
    assert "Research Scientist" in read["text"] and "https://github.com/adal" in read["links"]
    assert tools.returned("https://github.com/adal") and tools.quote_found("Research Scientist at Analytical Engines",
                                                                          "https://ada.example")
    assert tools.usd == 0.0 and len(calls) == 1
    assert tools.call("web_read", {"url": LINKEDIN})["error"].startswith("web_read does not read")


def test_provider_refusals_stop_the_run(store: Any, config: Any) -> None:
    no_key = kit(store, config, {}, fixture_transport({}))
    with pytest.raises(loop.ToolStop) as stop:
        no_key.call("x_profile", {"handle": "@ada"})
    assert stop.value.code == "NEED_KEY"
    limited = kit(store, config, {"TWITTERAPI_IO_KEY": "k"}, fixture_transport({"https://api.twitterapi.io/": (429, "{}")}))
    with pytest.raises(loop.ToolStop) as stop:
        limited.call("x_posts", {"handle": "ada"})
    assert stop.value.code == "RATE_LIMITED"
    assert "x_profile" not in no_key.available() and "web_search" in no_key.available()


def test_linkedin_is_capped_at_two_live_pulls_and_cached_pulls_are_free(store: Any, config: Any) -> None:
    transport, calls = counting({"https://api.apify.com/v2/acts/": APIFY_ITEM})
    env = {"APIFY_API_KEY": "k"}
    tools = kit(store, config, env, transport)
    first = tools.call("linkedin_profile", {"url": LINKEDIN})
    assert first["positions"][0]["company"] == "Analytical Engines"
    tools.call("linkedin_profile", {"url": "https://www.linkedin.com/in/other-1"})
    capped = tools.call("linkedin_profile", {"url": "https://www.linkedin.com/in/other-2"})
    assert "cap" in capped["error"] and len(calls) == 2
    assert tools.usd == pytest.approx(0.010)
    assert tools.call("linkedin_profile", {"url": LINKEDIN})["name"] == "Ada Lovelace"  # a cache hit passes the cap
    assert len(calls) == 2 and tools.usd == pytest.approx(0.010)
    assert tools.profile_for(LINKEDIN)["work"][0]["current"] is True


def test_github_user_keeps_its_links_for_cross_checks(store: Any, config: Any) -> None:
    user = {"login": "adal", "name": "Ada Lovelace", "company": "@analyticalengines", "blog": "ada.example",
            "twitter_username": "ada_l", "bio": "Engines and numbers"}
    tools = kit(store, config, {}, fixture_transport({"https://api.github.com/users/adal": user}))
    out = tools.call("github_user", {"login": "https://github.com/adal"})
    assert out["name"] == "Ada Lovelace" and out["links"] == ["https://ada.example", "https://x.com/ada_l"]
    assert tools.returned("https://ada.example") and tools.returned("https://x.com/ada_l")
    assert tools.profile_for("https://github.com/adal")["links"] == ["https://ada.example", "https://x.com/ada_l"]
    missing = kit(store, config, {}, fixture_transport({"https://api.github.com/users/": (404, "{}")}))
    assert missing.call("github_user", {"login": "nobody"})["error"].startswith("no GitHub user")


def test_list_lookup_finds_listed_and_removed_people(store: Any, config: Any) -> None:
    listed, _ = store.add_person("Ada Lovelace", links={"github": "https://github.com/adal"})
    gone, _ = store.add_person("Charles Babbage", links={"github": "https://github.com/cbabbage"})
    store.remove_person(gone, reason="not for us")
    tools = kit(store, config, {}, fixture_transport({}))
    out = tools.call("list_lookup", {"urls": ["github.com/adal", "https://github.com/cbabbage"], "name": "Ada Lovelace"})
    assert out["urls"][0]["listed"] and out["urls"][0]["person"] == f"#{listed} Ada Lovelace"
    assert out["urls"][1]["removed"] and not out["urls"][1]["listed"]
    assert out["people_named"][0]["person"].startswith(f"#{listed}")


def test_specs_and_arguments(store: Any, config: Any) -> None:
    tools = kit(store, config, {}, fixture_transport({}))
    specs = tools.specs(["web_search", "nope", "list_lookup"])
    assert [s["name"] for s in specs] == ["web_search", "list_lookup"]
    assert set(specs[0]) == {"name", "description", "input_schema"}
    assert tools.call("nope", {})["error"] == "no tool named nope"
    assert "takes no argument" in tools.call("github_user", {"login": "a", "x": 1})["error"]
    assert "needs login" in tools.call("github_user", {})["error"]
    assert frozenset(SPECS) - {"list_lookup"} == Toolkit.EVIDENCE


ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2401.01234v1</id><published>2024-01-02T00:00:00Z</published>
    <title>Notes on the Engine</title>
    <author><name>Ada Lovelace</name><arxiv:affiliation>Analytical Engines</arxiv:affiliation></author>
    <link href="http://arxiv.org/abs/2401.01234v1" rel="alternate" type="text/html"/>
  </entry>
</feed>"""


def test_free_research_and_social_tools(store: Any, config: Any) -> None:
    routes = {
        "http://export.arxiv.org/api/query": ATOM,
        "https://api.semanticscholar.org/graph/v1/author/search": {"data": [
            {"authorId": "123", "name": "Ada Lovelace", "url": "https://www.semanticscholar.org/author/123",
             "affiliations": ["Analytical Engines"], "homepage": "https://ada.example", "paperCount": 3, "hIndex": 2}]},
        "https://public.api.bsky.app/xrpc/app.bsky.actor.getProfile": {"handle": "ada.bsky.social", "displayName": "Ada",
                                                                        "description": "Engines. https://ada.example"},
    }
    tools = kit(store, config, {}, fixture_transport(routes))
    papers = tools.call("arxiv_search", {"name": "Ada Lovelace"})
    assert papers["papers"][0]["affiliations"] == {"Ada Lovelace": "Analytical Engines"}
    assert tools.quote_found("Ada Lovelace: Analytical Engines", papers["papers"][0]["url"])
    authors = tools.call("scholar_search", {"name": "Ada Lovelace"})
    assert authors["authors"][0]["affiliations"] == ["Analytical Engines"] and tools.returned("https://ada.example")
    profile = tools.call("bluesky_profile", {"handle": "@ada"})
    assert profile["name"] == "Ada" and profile["links"] == ["https://ada.example"]
    assert tools.profile_for("https://bsky.app/profile/ada.bsky.social")["kind"] == "bluesky"
    assert tools.usd == 0.0
