from __future__ import annotations

from pathlib import Path
from typing import Any

from tailwatch.connectors.base import CheckContext
from tailwatch.connectors.http import Call, Http, HttpError, fixture_transport
from tailwatch.connectors.rss import RssChecker

FIXTURES = Path(__file__).parent / "fixtures" / "connectors"
ATOM = (FIXTURES / "blog_atom.xml").read_text()
RSS = (FIXTURES / "blog_rss.xml").read_text()
URL = "https://ada.dev/feed.xml"


def ctx(answer: Any) -> CheckContext:
    return CheckContext(http=Http(transport=fixture_transport({URL: answer})))


def test_atom_feed_first_read() -> None:
    read = RssChecker().read(URL, None, ctx(ATOM))
    assert read.status == "ok"
    assert read.content["name"] == "Ada's notes" and read.content["website"] == "https://ada.dev/"
    posts = read.content["posts"]
    assert [p["title"] for p in posts] == ["On notation", "Hello"]
    assert posts[0]["text"] == "Why notation matters." and posts[0]["date"] == "2026-02-01T09:00:00Z"
    assert posts[1]["date"] == "2026-01-01T09:00:00Z"  # <updated> when there is no <published>


def test_rss_feed_dates_and_ids() -> None:
    posts = RssChecker().read(URL, None, ctx(RSS)).content["posts"]
    assert posts[0]["id"] == "https://lab.example/news/ada" and posts[0]["date"] == "2026-03-02T10:00:00Z"


def test_unchanged_and_new_post() -> None:
    checker = RssChecker()
    before = checker.read(URL, None, ctx(ATOM)).content
    assert checker.diff(before, checker.read(URL, before, ctx(ATOM)).content) == []
    entry = ("<entry><title>Joining Babbage Labs</title><link href='https://ada.dev/babbage'/>"
             "<id>tag:ada.dev,2026:babbage</id><published>2026-03-01T09:00:00Z</published>"
             "<summary>Some news.</summary></entry>")
    after = checker.read(URL, before, ctx(ATOM.replace("<entry>", entry + "<entry>", 1))).content
    changes = checker.diff(before, after)
    assert [c.label for c in changes] == ["new post"]
    assert changes[0].after.splitlines() == ["Joining Babbage Labs", "Some news.", "https://ada.dev/babbage"]
    assert changes[0].content_date == "2026-03-01"


def test_gone_errors_and_not_a_feed() -> None:
    assert RssChecker().read(URL, None, ctx((404, ""))).status == "gone"
    assert RssChecker().read(URL, None, ctx((500, ""))).status == "error"
    assert RssChecker().read(URL, None, ctx("<html><body>hello</body></html>")).status == "error"

    def timeout(call: Call) -> Any:
        raise HttpError("timeout")
    assert RssChecker().read(URL, None, ctx(timeout)).status == "error"


def test_entity_declarations_are_refused() -> None:
    bomb = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><rss><channel><title>&a;</title></channel></rss>'
    assert RssChecker().read(URL, None, ctx(bomb)).status == "error"


def test_empty_feed_after_entries_is_guarded() -> None:
    checker = RssChecker()
    before = checker.read(URL, None, ctx(ATOM)).content
    empty = '<feed xmlns="http://www.w3.org/2005/Atom"><title>Ada\'s notes</title></feed>'
    after = checker.read(URL, before, ctx(empty)).content
    assert checker.guard(before, after)
