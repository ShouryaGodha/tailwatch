from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from tailwatch.connectors.arxiv import ArxivChecker
from tailwatch.connectors.base import CheckContext
from tailwatch.connectors.http import Call, Http, HttpError, fixture_transport

FIXTURES = Path(__file__).parent / "fixtures" / "connectors"
QUERY_XML = (FIXTURES / "arxiv_query.xml").read_text()
URL = "arxiv:au:Lovelace, Ada"
API = "http://export.arxiv.org/api/query"
EMPTY = '<feed xmlns="http://www.w3.org/2005/Atom"><title>empty</title></feed>'


def ctx(routes: dict[str, Any]) -> CheckContext:
    return CheckContext(http=Http(transport=fixture_transport(routes)))


def newer(xml: str) -> str:
    entry = ('<entry><id>http://arxiv.org/abs/2604.00001v1</id><published>2026-04-01T00:00:00Z</published>'
             '<title>A Newer Paper</title><author><name>Ada Lovelace</name>'
             '<arxiv:affiliation>Babbage Labs</arxiv:affiliation></author><author><name>Mary Somerville</name></author>'
             '<link href="http://arxiv.org/abs/2604.00001v1" rel="alternate" type="text/html"/></entry>')
    return re.sub(r"(<entry>)", entry + r"\1", xml, count=1)


def test_first_read_parses_papers_newest_first() -> None:
    seen: list[Call] = []

    def api(call: Call) -> str:
        seen.append(call)
        return QUERY_XML
    read = ArxivChecker().read(URL, None, ctx({API: api}))
    assert read.status == "ok"
    assert seen[0].params["search_query"] == 'au:"Lovelace, Ada"' and seen[0].params["sortBy"] == "submittedDate"
    papers = read.content["papers"]
    assert [p["id"] for p in papers] == ["2603.01234", "2511.00042"]  # versions stripped
    assert papers[0]["title"] == "Notes on the Analytical Engine"
    assert papers[0]["authors"] == ["Ada Lovelace", "Charles Babbage"]
    assert papers[0]["venue"] == "cs.LG" and papers[1]["venue"] == "J. Engines 1 (2025)"
    assert read.content["organization"] == "Analytical Engines Ltd"
    assert read.content["name"] == "Ada Lovelace"


def test_unchanged_read_gives_no_changes() -> None:
    checker = ArxivChecker()
    before = checker.read(URL, None, ctx({API: QUERY_XML})).content
    after = checker.read(URL, before, ctx({API: QUERY_XML})).content
    assert before == after and checker.diff(before, after) == []


def test_new_paper_and_affiliation_changed() -> None:
    checker = ArxivChecker()
    before = checker.read(URL, None, ctx({API: QUERY_XML})).content
    after = checker.read(URL, before, ctx({API: newer(QUERY_XML)})).content
    changes = checker.diff(before, after)
    assert [c.label for c in changes] == ["affiliation changed", "new paper"]
    assert (changes[0].before, changes[0].after) == ("Analytical Engines Ltd", "Babbage Labs")
    paper = changes[1]
    assert paper.after.splitlines() == ["A Newer Paper", "Ada Lovelace, Mary Somerville", "http://arxiv.org/abs/2604.00001v1"]
    assert paper.content_date == "2026-04-01"


def test_affiliation_is_carried_when_a_read_lists_none() -> None:
    checker = ArxivChecker()
    before = checker.read(URL, None, ctx({API: QUERY_XML})).content
    bare = QUERY_XML.replace("<arxiv:affiliation>Analytical Engines Ltd</arxiv:affiliation>", "")
    after = checker.read(URL, before, ctx({API: bare})).content
    assert after["organization"] == "Analytical Engines Ltd" and checker.diff(before, after) == []


def test_author_page_reads_the_atom2_feed() -> None:
    read = ArxivChecker().read("https://arxiv.org/a/lovelace_a_1", None,
                               ctx({"https://arxiv.org/a/lovelace_a_1.atom2": QUERY_XML}))
    assert read.status == "ok" and len(read.content["papers"]) == 2


def test_author_page_falls_back_to_the_query_when_the_feed_is_down() -> None:
    seen: list[str] = []

    def api(call: Call) -> str:
        seen.append(call.params["search_query"])
        return QUERY_XML
    read = ArxivChecker().read("https://arxiv.org/a/lovelace_a_1", None,
                               ctx({"https://arxiv.org/a/lovelace_a_1.atom2": (503, "down"), API: api}))
    assert read.status == "ok" and seen == ["au:lovelace_a"]


def test_unknown_author_page_is_gone_and_api_failures_are_errors() -> None:
    assert ArxivChecker().read("https://arxiv.org/a/nobody_x_1", None,
                               ctx({"https://arxiv.org/a/nobody_x_1.atom2": (404, "")})).status == "gone"
    assert ArxivChecker().read(URL, None, ctx({API: (503, "busy")})).status == "error"
    assert ArxivChecker().read(URL, None, ctx({API: "<html>not xml"})).status == "error"

    def timeout(call: Call) -> Any:
        raise HttpError("timeout reading arxiv")
    assert ArxivChecker().read(URL, None, ctx({API: timeout})).status == "error"


def test_zero_papers_after_some_is_guarded() -> None:
    checker = ArxivChecker()
    before = checker.read(URL, None, ctx({API: QUERY_XML})).content
    after = checker.read(URL, before, ctx({API: EMPTY})).content
    assert after["papers"] == [] and checker.guard(before, after)
