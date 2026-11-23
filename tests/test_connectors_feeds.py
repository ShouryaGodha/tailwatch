from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tailwatch.connectors.base import CheckContext
from tailwatch.connectors.feeds import FeedError
from tailwatch.connectors.feeds.bluesky_account import BlueskyAccountFeed
from tailwatch.connectors.feeds.hackernews import HackerNewsFeed
from tailwatch.connectors.feeds.rss_feed import RssFeed
from tailwatch.connectors.feeds.x_account import XAccountFeed
from tailwatch.connectors.http import Call, Http, HttpError, fixture_transport

FIXTURES = Path(__file__).parent / "fixtures" / "connectors"


def ctx(routes: dict[str, Any], env: dict[str, str] | None = None) -> CheckContext:
    return CheckContext(http=Http(transport=fixture_transport(routes)), env=env or {})


# --------------------------------------------------------------------------- Hacker News

HN = "https://hn.algolia.com/api/v1/search_by_date"
HITS = [{"objectID": "2", "title": "Ada Lovelace joins Babbage Labs", "url": "https://babbage.example/ada",
         "author": "pg", "created_at": "2026-03-02T10:00:00Z", "created_at_i": 200,
         "story_text": "<p>Her page: https://ada.dev and https://x.com/ada</p>"},
        {"objectID": "1", "title": "Ask HN: who is hiring?", "url": None, "author": "x", "created_at_i": 100}]


def test_hackernews_first_poll_only_marks() -> None:
    items, marker = HackerNewsFeed().poll(None, {"query": "joins"}, ctx({HN: {"hits": HITS}}))
    assert items == [] and marker == "200"


def test_hackernews_items_after_the_marker_oldest_first_with_mentions() -> None:
    seen: list[dict] = []

    def search(call: Call) -> Any:
        seen.append(dict(call.params))
        return {"hits": HITS}
    items, marker = HackerNewsFeed().poll("50", {"query": "joins OpenAI"}, ctx({HN: search}))
    assert seen[0]["query"] == "joins OpenAI" and seen[0]["tags"] == "story"
    assert seen[0]["numericFilters"] == "created_at_i>50"
    assert [i.id for i in items] == ["hn:1", "hn:2"] and marker == "200"
    assert items[0].url == "https://news.ycombinator.com/item?id=1"
    story = items[1]
    assert story.mentions == {"x": ["ada"], "homepage": ["https://babbage.example/ada", "https://ada.dev"]}
    assert story.published_at == "2026-03-02T10:00:00Z" and story.author == "pg"


def test_hackernews_failures_raise() -> None:
    with pytest.raises(FeedError):
        HackerNewsFeed().poll("1", {"query": "x"}, ctx({HN: (503, "down")}))
    with pytest.raises(FeedError):
        HackerNewsFeed().poll("1", {}, ctx({HN: {"hits": []}}))


# --------------------------------------------------------------------------- RSS

FEED_URL = "https://lab.example/news.xml"
RSS = (FIXTURES / "blog_rss.xml").read_text()


def test_rss_feed_first_poll_only_marks() -> None:
    items, marker = RssFeed().poll(None, {"url": FEED_URL}, ctx({FEED_URL: RSS}))
    assert items == [] and marker == "https://lab.example/news/ada"


def test_rss_feed_items_above_the_marker() -> None:
    items, marker = RssFeed().poll("https://lab.example/news/opens", {"url": FEED_URL}, ctx({FEED_URL: RSS}))
    assert [i.id for i in items] == ["https://lab.example/news/ada"] and marker == "https://lab.example/news/ada"
    assert items[0].mentions == {"github": ["ada"], "x": ["ada_l"]}
    assert items[0].text.startswith("Ada Lovelace joins the lab")
    nothing, same = RssFeed().poll(marker, {"url": FEED_URL}, ctx({FEED_URL: RSS}))
    assert nothing == [] and same == marker


def test_rss_feed_marker_rolled_off_returns_every_entry_oldest_first() -> None:
    items, _ = RssFeed().poll("gone-id", {"url": FEED_URL}, ctx({FEED_URL: RSS}))
    assert [i.id for i in items] == ["https://lab.example/news/opens", "https://lab.example/news/ada"]


def test_rss_feed_failures_raise() -> None:
    def timeout(call: Call) -> Any:
        raise HttpError("timeout")
    with pytest.raises(FeedError):
        RssFeed().poll("x", {"url": FEED_URL}, ctx({FEED_URL: timeout}))
    with pytest.raises(FeedError):
        RssFeed().poll("x", {"url": FEED_URL}, ctx({FEED_URL: "<html/>"}))


# --------------------------------------------------------------------------- Bluesky account

XRPC = "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"


def bsky_post(rkey: str, text: str, at: str, facets: list | None = None, reason: dict | None = None) -> dict:
    item: dict[str, Any] = {"post": {"uri": f"at://did:plc:news/app.bsky.feed.post/{rkey}",
                                     "author": {"did": "did:plc:news", "handle": "moves.example.com"},
                                     "record": {"text": text, "createdAt": at, "facets": facets or []},
                                     "indexedAt": at}}
    if reason:
        item["reason"] = reason
    return item


def mention_facet(text: str, handle: str) -> dict:
    start = text.encode().index(f"@{handle}".encode())
    return {"index": {"byteStart": start, "byteEnd": start + len(f"@{handle}".encode())},
            "features": [{"$type": "app.bsky.richtext.facet#mention", "did": "did:plc:x"}]}


TEXT = "Café news: @ada.bsky.social joins Babbage Labs, says @moves.example.com"
BSKY = {"feed": [
    bsky_post("3c", TEXT, "2026-03-03T00:00:00Z", facets=[
        mention_facet(TEXT, "ada.bsky.social"), mention_facet(TEXT, "moves.example.com"),
        {"index": {"byteStart": 0, "byteEnd": 4},
         "features": [{"$type": "app.bsky.richtext.facet#link", "uri": "https://github.com/ada"}]}]),
    bsky_post("3r", "a repost", "2026-03-02T12:00:00Z", reason={"$type": "app.bsky.feed.defs#reasonRepost"}),
    bsky_post("3a", "Older news", "2026-03-01T00:00:00Z")]}


def test_bluesky_account_first_poll_only_marks() -> None:
    items, marker = BlueskyAccountFeed().poll(None, {"handle": "moves.example.com"}, ctx({XRPC: BSKY}))
    assert items == [] and marker == "2026-03-03T00:00:00Z"


def test_bluesky_account_extracts_mentions_from_facets() -> None:
    items, marker = BlueskyAccountFeed().poll("2026-03-01T00:00:00Z", {"handle": "moves.example.com"}, ctx({XRPC: BSKY}))
    assert [i.text for i in items] == [TEXT] and marker == "2026-03-03T00:00:00Z"
    assert items[0].mentions == {"bluesky": ["ada.bsky.social"], "github": ["ada"]}  # the announcer is dropped
    assert items[0].url == "https://bsky.app/profile/moves.example.com/post/3c"


# --------------------------------------------------------------------------- X account

X = "https://api.twitterapi.io/twitter/user/last_tweets"
ENV = {"TWITTERAPI_IO_KEY": "key"}
TWEETS = {"status": "success", "data": {"tweets": [
    {"id": "300", "text": "Big move: @AdaL joins @BabbageLabs from @movesbot https://t.co/x",
     "createdAt": "Mon Mar 02 10:00:00 +0000 2026",
     "entities": {"user_mentions": [{"screen_name": "AdaL", "name": "Ada Lovelace"},
                                    {"screen_name": "BabbageLabs", "name": "Babbage Labs"},
                                    {"screen_name": "movesbot", "name": "Moves"}],
                  "urls": [{"url": "https://t.co/x", "expanded_url": "https://www.linkedin.com/in/ada-l/"}]}},
    {"id": "250", "text": "RT", "retweeted_tweet": {"id": "1"}},
    {"id": "200", "text": "Older", "createdAt": "Sun Mar 01 10:00:00 +0000 2026"}]}}


def test_x_account_first_poll_only_marks() -> None:
    items, marker = XAccountFeed().poll(None, {"handle": "@movesbot"}, ctx({X: TWEETS}, ENV))
    assert items == [] and marker == "300"


def test_x_account_extracts_mentions_names_and_links() -> None:
    sent: list[Call] = []

    def answer(call: Call) -> Any:
        sent.append(call)
        return TWEETS
    items, marker = XAccountFeed().poll("200", {"handle": "movesbot"}, ctx({X: answer}, ENV))
    assert sent[0].params == {"userName": "movesbot"} and sent[0].headers["X-API-Key"] == "key"
    assert [i.id for i in items] == ["x:300"] and marker == "300"
    item = items[0]
    assert item.mentions == {"x": ["adal", "babbagelabs"], "linkedin": ["https://www.linkedin.com/in/ada-l"]}
    assert item.names == ["Ada Lovelace", "Babbage Labs"]
    assert "https://www.linkedin.com/in/ada-l/" in item.text  # t.co expanded


@pytest.mark.parametrize(("status", "outage"), [(402, "out of credits"), (401, "key rejected"), (429, "rate limited")])
def test_x_account_refusals_raise_with_outages(status: int, outage: str) -> None:
    with pytest.raises(FeedError) as error:
        XAccountFeed().poll("1", {"handle": "movesbot"}, ctx({X: (status, {"msg": "no"})}, ENV))
    assert error.value.outage == outage


def test_x_account_without_a_key_raises() -> None:
    with pytest.raises(FeedError) as error:
        XAccountFeed().poll("1", {"handle": "movesbot"}, ctx({}))
    assert error.value.outage == "no credential"


def test_feed_error_is_an_http_error_so_the_watch_loop_records_it() -> None:
    assert issubclass(FeedError, HttpError)
