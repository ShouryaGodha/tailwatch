"""The proof checks: each one can lower the enricher's verdict, none can raise it."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from tailwatch import util
from tailwatch.agent import loop
from tailwatch.connectors.http import Http, fixture_transport
from tailwatch.enrich import settings as tunables
from tailwatch.enrich import verify
from tailwatch.enrich.tools import Toolkit

LI = "https://www.linkedin.com/in/ada-lovelace"
GH = "https://github.com/adal"
HOME = "https://ada.example"
FLOORS = tunables.DEFAULTS[tunables.FLOORS]


def seed(**person: Any) -> dict[str, Any]:
    who = {"name": "Ada Lovelace", "left": "Analytical Engines", "joined": "Difference Labs", **person}
    return {"mode": "report", "person": who, "report": {"published_at": util.iso()}, "given_links": {},
            "expected_organizations": [o for o in (who["left"], who["joined"]) if o]}


def toolkit(store: Any, config: Any, *records: dict[str, Any]) -> Toolkit:
    kit = Toolkit(store, config, {}, budget=loop.Budget(1.0), http=Http(fixture_transport({})))
    for record in records:
        kit._record(record)
    return kit


def linkedin_record(name: str = "Ada Lovelace", company: str = "Analytical Engines", current: bool = True,
                    links: list[str] | None = None) -> dict[str, Any]:
    text = f"LinkedIn {name}: Research Scientist\n- Research Scientist at {company} (2024-01 to present)"
    work = [{"company": company, "title": "Research Scientist", "start": "2024-01", "end": "", "current": current}]
    return {"_texts": {LI: text}, "_profiles": {LI: {"kind": "linkedin", "url": LI, "name": name, "text": text, "work": work,
                                                     "links": links or []}}}


def link(url: str = LI, kind: str = "linkedin", **overrides: Any) -> dict[str, Any]:
    base = {"kind": kind, "url": url, "verdict": "verified", "confidence": 0.9, "quote": "Research Scientist at Analytical Engines",
            "quote_url": url, "facts": {}, "reason": "the profile shows the employer"}
    return {**base, **overrides}


def run_check(store: Any, kit: Toolkit, links: list[dict[str, Any]], the_seed: dict[str, Any] | None = None,
              **floors: Any) -> dict[str, Any]:
    return verify.check({"links": links}, the_seed or seed(), kit, store=store, floors={**FLOORS, **floors})


def verdicts(result: dict[str, Any]) -> list[str]:
    return [link["verdict"] for link in result["links"]]


def test_good_proof_stays_verified(store: Any, config: Any) -> None:
    result = run_check(store, toolkit(store, config, linkedin_record()), [link()])
    assert verdicts(result) == ["verified"] and result["links"][0]["notes"] == []


def test_a_quote_not_in_the_fetched_text_lowers_to_unverified(store: Any, config: Any) -> None:
    result = run_check(store, toolkit(store, config, linkedin_record()), [link(quote="Chief Scientist at Difference Labs")])
    assert verdicts(result) == ["unverified"] and "quote is not in the fetched text" in result["links"][0]["notes"][0]


def test_a_url_no_tool_returned_is_rejected(store: Any, config: Any) -> None:
    result = run_check(store, toolkit(store, config, linkedin_record()), [link(url="https://www.linkedin.com/in/made-up")])
    assert verdicts(result) == ["rejected"] and "not returned by any tool" in result["links"][0]["notes"][0]


def test_a_different_name_rejects_a_linkedin_and_lowers_other_kinds(store: Any, config: Any) -> None:
    kit = toolkit(store, config, linkedin_record(name="Grace Hopper"),
                  {"_texts": {GH: "Grace H. Research Scientist at Analytical Engines"},
                   "_profiles": {GH: {"kind": "github", "url": GH, "name": "Grace H", "text": "Analytical Engines", "links": []}}})
    result = run_check(store, kit, [link(), link(url=GH, kind="github")])
    assert verdicts(result) == ["rejected", "unverified"]
    initials = run_check(store, toolkit(store, config, linkedin_record(name="A. Lovelace")), [link()])
    assert verdicts(initials) == ["verified"]
    strict = run_check(store, toolkit(store, config, linkedin_record(name="A. Lovelace")), [link()], name_level_min="variant")
    assert verdicts(strict) == ["unverified"]


def test_confidence_under_the_floor_lowers(store: Any, config: Any) -> None:
    result = run_check(store, toolkit(store, config, linkedin_record()), [link(confidence=0.7)])
    assert verdicts(result) == ["unverified"] and "under 0.80" in result["links"][0]["notes"][0]


def test_no_expected_organization_lowers_unless_cross_linked(store: Any, config: Any) -> None:
    github = {"_texts": {GH: "Ada Lovelace | bio: numbers and looms | blog: https://ada.example"},
              "_profiles": {GH: {"kind": "github", "url": GH, "name": "Ada Lovelace", "text": "numbers and looms", "links": [HOME]}}}
    alone = run_check(store, toolkit(store, config, github), [link(url=GH, kind="github", quote="numbers and looms")])
    assert verdicts(alone) == ["unverified"] and "shows none of" in alone["links"][0]["notes"][0]
    homepage = {"_texts": {HOME: "Ada Lovelace, Research Scientist at Analytical Engines."},
                "_profiles": {HOME: {"kind": "homepage", "url": HOME, "name": "", "text": "Research Scientist at Analytical Engines",
                                     "links": [GH]}}}
    crossed = run_check(store, toolkit(store, config, github, homepage),
                        [link(url=GH, kind="github", quote="numbers and looms"), link(url=HOME, kind="homepage")])
    assert verdicts(crossed) == ["verified", "verified"] and crossed["links"][0]["cross_linked"]


def test_a_link_someone_on_the_list_holds_is_rejected_and_shared(store: Any, config: Any) -> None:
    other, _ = store.add_person("Ada Byron", links={"linkedin": LI})
    result = run_check(store, toolkit(store, config, linkedin_record()), [link()])
    assert verdicts(result) == ["rejected"] and result["shared_with"] == [other]
    mine = run_check(store, toolkit(store, config, linkedin_record()), [link()],
                     {**seed(), "person": {**seed()["person"], "id": other}})
    assert verdicts(mine) == ["verified"]  # the person's own link is not shared with themselves


def test_a_page_linking_a_different_linkedin_is_rejected(store: Any, config: Any) -> None:
    homepage = {"_texts": {HOME: "Ada Lovelace, Research Scientist at Analytical Engines."},
                "_profiles": {HOME: {"kind": "homepage", "url": HOME, "name": "", "text": "Analytical Engines",
                                     "links": ["https://www.linkedin.com/in/someone-else"]}}}
    result = run_check(store, toolkit(store, config, linkedin_record(), homepage), [link(), link(url=HOME, kind="homepage")])
    assert verdicts(result) == ["verified", "rejected"]


def test_a_linkedin_still_at_the_old_place_long_after_the_report_lowers(store: Any, config: Any) -> None:
    old = seed()
    old["report"] = {"published_at": util.iso(util.now() - timedelta(days=400))}
    result = run_check(store, toolkit(store, config, linkedin_record()), [link()], old)
    assert verdicts(result) == ["unverified"] and "still shows Analytical Engines" in result["links"][0]["notes"][0]
    fresh = run_check(store, toolkit(store, config, linkedin_record()), [link()])
    assert verdicts(fresh) == ["verified"]


def test_checks_never_raise_a_verdict(store: Any, config: Any) -> None:
    kit = toolkit(store, config, linkedin_record())
    result = run_check(store, kit, [link(verdict="unverified"), link(verdict="rejected"), link(verdict="certain")])
    assert verdicts(result) == ["unverified", "rejected", "unverified"]
    assert verify.lower("unverified", "verified") == "unverified" and verify.lower("verified", "rejected") == "rejected"


def test_fill_links_known_links_anchor_cross_links(store: Any, config: Any) -> None:
    person, _ = store.add_person("Ada Lovelace", organization="Analytical Engines", links={"homepage": HOME})
    github = {"_texts": {GH: "Ada Lovelace | bio: looms | blog: https://ada.example"},
              "_profiles": {GH: {"kind": "github", "url": GH, "name": "Ada Lovelace", "text": "looms", "links": [HOME]}}}
    the_seed = {"mode": "fill_links", "person": {"id": person, "name": "Ada Lovelace", "organization": "Analytical Engines"},
                "known_links": {"homepage": HOME}, "expected_organizations": ["Analytical Engines"]}
    result = run_check(store, toolkit(store, config, github), [link(url=GH, kind="github", quote="looms")], the_seed)
    assert verdicts(result) == ["verified"]
    assert verify.best_links(result["links"]) == {"github": result["links"][0]}
