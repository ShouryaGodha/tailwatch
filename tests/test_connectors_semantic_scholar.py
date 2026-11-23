from __future__ import annotations

from typing import Any

from tailwatch.connectors.base import CheckContext
from tailwatch.connectors.http import Call, Http, HttpError, fixture_transport
from tailwatch.connectors.semantic_scholar import SemanticScholarChecker

URL = "https://www.semanticscholar.org/author/123456"
AUTHOR = "https://api.semanticscholar.org/graph/v1/author/123456"
PROFILE = {"authorId": "123456", "name": "Ada Lovelace", "affiliations": ["Analytical Engines Ltd"],
           "homepage": "https://ada.dev", "paperCount": 2, "hIndex": 2}
PAPERS = {"data": [
    {"paperId": "p1", "title": "Bernoulli Numbers by Machine", "year": 2025, "publicationDate": "2025-11-20",
     "venue": "J. Engines", "url": "https://www.semanticscholar.org/paper/p1", "externalIds": {},
     "authors": [{"authorId": "123456", "name": "Ada Lovelace"}]},
    {"paperId": "p2", "title": "Notes on the Analytical Engine", "year": 2026, "publicationDate": "2026-03-02",
     "venue": "", "url": "", "externalIds": {"ArXiv": "2603.01234"},
     "authors": [{"authorId": "123456", "name": "Ada Lovelace"}, {"authorId": "9", "name": "Charles Babbage"}]}]}


def ctx(profile: Any = None, papers: Any = None, env: dict[str, str] | None = None) -> CheckContext:
    routes = {f"{AUTHOR}/papers": papers if papers is not None else PAPERS, AUTHOR: profile if profile is not None else PROFILE}
    return CheckContext(http=Http(transport=fixture_transport(routes)), env=env or {})


def test_first_read_sorts_papers_newest_first() -> None:
    read = SemanticScholarChecker().read(URL, None, ctx())
    assert read.status == "ok"
    content = read.content
    assert content["name"] == "Ada Lovelace" and content["organization"] == "Analytical Engines Ltd"
    assert content["website"] == "https://ada.dev" and content["stats"] == {"papers": 2, "h_index": 2}
    assert [p["id"] for p in content["papers"]] == ["p2", "p1"]
    assert content["papers"][0]["url"] == "https://arxiv.org/abs/2603.01234"
    assert content["papers"][0]["authors"] == ["Ada Lovelace", "Charles Babbage"]


def test_api_key_is_sent_when_set() -> None:
    headers: list[dict] = []

    def author(call: Call) -> Any:
        headers.append(dict(call.headers))
        return PROFILE
    http = Http(transport=fixture_transport({f"{AUTHOR}/papers": PAPERS, AUTHOR: author}))
    SemanticScholarChecker().read(URL, None, CheckContext(http=http, env={"SEMANTIC_SCHOLAR_API_KEY": "k"}))
    assert headers[0]["x-api-key"] == "k"


def test_unchanged_read_gives_no_changes() -> None:
    checker = SemanticScholarChecker()
    before = checker.read(URL, None, ctx()).content
    assert checker.diff(before, checker.read(URL, before, ctx()).content) == []


def test_affiliation_changed_and_new_paper() -> None:
    checker = SemanticScholarChecker()
    before = checker.read(URL, None, ctx()).content
    new = {"paperId": "p3", "title": "A Newer Paper", "publicationDate": "2026-04-01", "venue": "NeurIPS",
           "url": "https://www.semanticscholar.org/paper/p3", "authors": [{"name": "Ada Lovelace"}]}
    after = checker.read(URL, before, ctx(profile={**PROFILE, "affiliations": ["Babbage Labs"]},
                                          papers={"data": [new, *PAPERS["data"]]})).content
    changes = checker.diff(before, after)
    assert [c.label for c in changes] == ["affiliation changed", "new paper"]
    assert changes[0].after == "Babbage Labs"
    assert changes[1].after.startswith("A Newer Paper (NeurIPS)") and changes[1].content_date == "2026-04-01"


def test_404_is_gone_429_is_a_rate_limit_outage_and_5xx_or_timeout_is_an_error() -> None:
    assert SemanticScholarChecker().read(URL, None, ctx(profile=(404, {"error": "Author not found"}))).status == "gone"
    limited = SemanticScholarChecker().read(URL, None, ctx(profile=(429, {"message": "Too Many Requests"})))
    assert limited.status == "error" and limited.outage == "rate limited"
    assert SemanticScholarChecker().read(URL, None, ctx(papers=(500, "oops"))).status == "error"

    def timeout(call: Call) -> Any:
        raise HttpError("timeout")
    assert SemanticScholarChecker().read(URL, None, ctx(profile=timeout)).status == "error"


def test_empty_papers_for_an_author_with_papers_is_guarded() -> None:
    checker = SemanticScholarChecker()
    before = checker.read(URL, None, ctx()).content
    after = checker.read(URL, before, ctx(papers={"data": []})).content
    assert checker.guard(before, after)
