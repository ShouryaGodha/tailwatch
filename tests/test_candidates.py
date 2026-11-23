"""Candidates end to end: identify → filter → enrich → add, review, out of scope, round-ups, the
daily cap, people already listed or removed, and a person's decisions."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from tailwatch import util
from tailwatch.agent.providers.scripted import turn
from tailwatch.connectors.http import Http, fixture_transport
from tailwatch.desk import notices
from tailwatch.enrich import candidates
from tailwatch.enrich import settings as tunables

GH = "https://github.com/adal"
TEXT = "Ada Lovelace left Analytical Engines to join Difference Labs as Chief Scientist. github.com/adal"
USER = {"login": "adal", "name": "Ada Lovelace", "company": "Difference Labs",
        "bio": "Chief Scientist at Difference Labs, before that Analytical Engines", "blog": ""}
HTTP = Http(fixture_transport({"https://api.github.com/users/adal": USER}))


def identity(**overrides: Any) -> dict[str, Any]:
    base = {"is_report": True, "name": "Ada Lovelace", "handles": ["github.com/adal"], "left": "Analytical Engines",
            "joined": "Difference Labs", "role": "Chief Scientist", "others": [], "reason": "a reported move"}
    return {**base, **overrides}


def answer(links: list[dict[str, Any]]) -> dict[str, Any]:
    return {"name": "Ada Lovelace", "organization": "Difference Labs", "title": "Chief Scientist", "summary": "",
            "links": links, "concerns": []}


def github_link(**overrides: Any) -> dict[str, Any]:
    base = {"kind": "github", "url": GH, "verdict": "verified", "confidence": 0.9, "quote": "Chief Scientist at Difference Labs",
            "quote_url": GH, "facts": {"name": True, "organization": True, "role": True, "dates": False, "cross_link": False},
            "reason": "the bio names the move"}
    return {**base, **overrides}


class Script:
    """The identifier answers ``who``; the enricher reads the GitHub profile, then answers ``final``."""

    def __init__(self, who: Any, final: dict[str, Any]) -> None:
        self.who, self.final = who, final
        self.identify_calls = 0
        self.enrich_calls = 0

    def __call__(self, request: Any) -> Any:
        if request.schema_name == "identify_mention":
            self.identify_calls += 1
            return self.who(request) if callable(self.who) else self.who
        done = sum(1 for m in request.messages if m["role"] == "assistant" and m.get("tool_calls"))
        if done == 0:
            self.enrich_calls += 1
            return turn("", ("github_user", {"login": "adal"}))
        return self.final


def report(store: Any, item: str = "1", text: str = TEXT) -> int:
    candidate_id, _ = store.add_candidate("trusted-news", item, url=f"https://news.example/{item}", text=text,
                                          subject={"names": [], "mentions": {"github": ["adal"]}, "author": "news",
                                                   "published_at": util.iso()})
    return candidate_id


@pytest.fixture
def posted() -> Iterator[list[str]]:
    said: list[str] = []
    notices.set_poster(lambda text, thread: said.append(text) or "1")
    yield said
    notices.set_poster(None)


def test_a_verified_profile_adds_the_person_with_the_report_as_first_event(store: Any, config: Any, model: Any) -> None:
    script = Script(identity(), answer([github_link()]))
    model(script)
    candidate_id = report(store)
    counts = candidates.run(store, config, {}, http=HTTP, log=None)
    assert counts["added"] == 1 and counts["identified"] == 1
    row = store.candidate(candidate_id)
    assert row["state"] == "added" and row["enrichment"]["verified"] == {"github": GH}
    person = store.person(row["person_id"])
    assert person["name"] == "Ada Lovelace" and person["organization"] == "Analytical Engines"
    assert person["added_because"] == "report: trusted-news 1" and person["links"] == {"github": GH}
    pending = store.pending_checks(5)
    assert len(pending) == 1 and pending[0]["person_id"] == person["id"]
    assert pending[0]["events"][0]["label"] == "reported by trusted-news"
    assert candidates.run(store, config, {}, http=HTTP, log=None)["seen"] == 0  # settled rows are not worked again


def test_unproven_profiles_wait_for_review(store: Any, config: Any, model: Any) -> None:
    model(Script(identity(), answer([github_link(quote="Founder of a secret startup")])))
    candidate_id = report(store)
    counts = candidates.run(store, config, {}, http=HTTP, log=None)
    row = store.candidate(candidate_id)
    assert counts["needs_review"] == 1 and row["state"] == "needs_review"
    assert "quote is not in the fetched text" in row["reason"] and not store.people()


def test_reports_outside_the_tiers_are_out_of_scope_before_any_search(store: Any, config: Any, model: Any) -> None:
    script = Script(identity(left="Acme", joined="Initech"), answer([]))
    model(script)
    candidate_id = report(store)
    counts = candidates.run(store, config, {}, http=HTTP, log=None)
    assert counts["out_of_scope"] == 1 and store.candidate(candidate_id)["state"] == "out_of_scope"
    assert script.enrich_calls == 0
    config.priority.tiers = []
    assert candidates.out_of_scope(config, identity(left="Acme", joined="Initech"), {}) == ""


def test_a_round_up_becomes_one_candidate_per_person(store: Any, config: Any, model: Any) -> None:
    grace = {"name": "Grace Hopper", "handles": [], "left": "Analytical Engines", "joined": "Compiler Co", "role": "Engineer"}
    script = Script(identity(others=[grace]), answer([]))
    model(script)
    candidate_id = report(store)
    counts = candidates.run(store, config, {}, http=HTTP, log=None)
    rows = {r["item_id"]: r for r in store.candidates()}
    assert set(rows) == {"1", "1#1"} and counts["round_up"] == 1
    child = rows["1#1"]
    assert child["subject"]["identified"]["name"] == "Grace Hopper" and child["subject"]["from_candidate"] == candidate_id
    assert script.identify_calls == 1  # the round-up's people are not asked about again
    assert child["state"] == "needs_review" and rows["1"]["state"] == "needs_review"


def test_the_daily_cap_stops_the_job_and_says_so_once(store: Any, config: Any, model: Any, posted: list[str]) -> None:
    script = Script(identity(), answer([github_link()]))
    model(script)
    store.log_run("enrich_person", None, tool_usd=config.budgets.enrich_daily)
    report(store)
    first = candidates.run(store, config, {}, http=HTTP, log=None)
    second = candidates.run(store, config, {}, http=HTTP, log=None)
    assert first["stopped"].startswith("daily cap") and second["stopped"].startswith("daily cap")
    assert len(posted) == 1 and "daily budget is spent" in posted[0]
    assert script.identify_calls == 0 and store.candidates()[0]["state"] == "new"


def test_nobody_is_added_twice(store: Any, config: Any, model: Any) -> None:
    script = Script(identity(), answer([github_link()]))
    model(script)
    report(store, "1")
    second = report(store, "2", text="Ada Lovelace (github.com/adal) is Chief Scientist at Difference Labs now")
    counts = candidates.run(store, config, {}, http=HTTP, log=None)
    assert counts["added"] == 1 and counts["listed_already"] == 1 and len(store.people()) == 1
    row = store.candidate(second)
    assert row["state"] == "dismissed" and row["reason"].startswith("already on the list")
    assert len(store.pending_checks(5)) == 2  # the second report is the listed person's event too
    assert script.enrich_calls == 1


def test_a_removed_person_is_never_added_back(store: Any, config: Any, model: Any) -> None:
    person_id, _ = store.add_person("Ada Lovelace", organization="Analytical Engines", links={"github": GH})
    store.remove_person(person_id, reason="not for us")
    script = Script(identity(), answer([github_link()]))
    model(script)
    candidate_id = report(store)
    candidates.run(store, config, {}, http=HTTP, log=None)
    row = store.candidate(candidate_id)
    assert row["state"] == "dismissed" and "removed" in row["reason"] and script.enrich_calls == 0
    assert store.person(person_id)["status"] == "ignored"


def test_a_role_on_the_out_of_scope_list_is_dismissed(store: Any, config: Any, model: Any) -> None:
    tunables.change(store, "out_of_scope_roles", "sales", by="U1")
    script = Script(identity(role="Head of Sales"), answer([]))
    model(script)
    candidate_id = report(store)
    candidates.run(store, config, {}, http=HTTP, log=None)
    assert store.candidate(candidate_id)["state"] == "dismissed" and script.enrich_calls == 0


def test_a_post_about_nobody_is_dismissed(store: Any, config: Any, model: Any) -> None:
    model(Script(identity(is_report=False, name="", handles=[], reason="a product launch"), answer([])))
    candidate_id = report(store)
    candidates.run(store, config, {}, http=HTTP, log=None)
    row = store.candidate(candidate_id)
    assert row["state"] == "dismissed" and row["reason"] == "a product launch"


def test_failures_count_attempts_then_wait_for_review(store: Any, config: Any, model: Any) -> None:
    model(lambda request: "not json")
    candidate_id = report(store)
    for _ in range(candidates.MAX_ATTEMPTS):
        candidates.run(store, config, {}, http=HTTP, log=None)
    row = store.candidate(candidate_id)
    assert row["state"] == "needs_review" and row["attempts"] == candidates.MAX_ATTEMPTS


def test_a_persons_decisions(store: Any, config: Any, model: Any) -> None:
    model(Script(identity(), answer([])))
    candidate_id = report(store)
    candidates.run(store, config, {}, http=HTTP, log=None)
    assert store.candidate(candidate_id)["state"] == "needs_review"
    assert "no verified link" in candidates.decide(store, config, {}, candidate_id, "add", by="U1")["error"]
    assert "needs a reason" in candidates.decide(store, config, {}, candidate_id, "dismiss", by="U1")["error"]
    again = candidates.decide(store, config, {}, candidate_id, "retry", note="try the homepage", by="U1")
    assert again["state"] == "identified" and store.candidate(candidate_id)["attempts"] == 0
    added = candidates.decide(store, config, {}, candidate_id, "add", links=["https://github.com/adal"], by="U1")
    assert added["state"] == "added" and store.person(added["person_id"])["links"] == {"github": GH}
    row = store.candidate(candidate_id)
    assert row["state"] == "added" and [d["action"] for d in row["enrichment"]["decisions"]] == ["retry", "add"]
    assert "added already" in candidates.decide(store, config, {}, candidate_id, "retry", by="U1")["error"]
    other = report(store, "9")
    dismissed = candidates.decide(store, config, {}, other, "dismiss", note="wrong person", by="U1")
    assert dismissed["state"] == "dismissed" and store.candidate(other)["reason"] == "wrong person"
    assert "no candidate" in candidates.decide(store, config, {}, 999, "add", by="U1")["error"]
