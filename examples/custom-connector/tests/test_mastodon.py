"""Run with ``pytest`` from this directory (tailwatch installed). No network: every answer is a fixture."""

from __future__ import annotations

from tailwatch_mastodon import MastodonChecker

from tailwatch.connectors.base import CheckContext
from tailwatch.connectors.http import Http, fixture_transport

URL = "https://mastodon.social/@ada"
LOOKUP = "https://mastodon.social/api/v1/accounts/lookup"
STATUSES = "https://mastodon.social/api/v1/accounts/1/statuses"
ACCOUNT = {
    "id": "1",
    "username": "ada",
    "display_name": "Ada Lovelace",
    "note": "<p>Engines &amp; notation.</p>",
    "followers_count": 10,
}
POSTS = [
    {
        "id": "11",
        "content": "<p>Hello</p>",
        "created_at": "2026-03-01T00:00:00.000Z",
        "url": "https://mastodon.social/@ada/11",
        "reblog": None,
    }
]


def ctx(account=ACCOUNT, posts=POSTS) -> CheckContext:
    return CheckContext(http=Http(transport=fixture_transport({STATUSES: posts, LOOKUP: account})))


def test_first_read_and_unchanged() -> None:
    checker = MastodonChecker()
    read = checker.read(URL, None, ctx())
    assert read.status == "ok" and read.content["bio"] == "Engines & notation."
    assert checker.diff(read.content, checker.read(URL, read.content, ctx()).content) == []


def test_new_post_and_bio_change() -> None:
    checker = MastodonChecker()
    before = checker.read(URL, None, ctx()).content
    new = {
        "id": "12",
        "content": "<p>Joined Babbage Labs</p>",
        "created_at": "2026-03-02T00:00:00.000Z",
        "url": "https://mastodon.social/@ada/12",
    }
    after = checker.read(
        URL, before, ctx(account={**ACCOUNT, "note": "Babbage Labs."}, posts=[new, *POSTS])
    ).content
    assert [c.label for c in checker.diff(before, after)] == ["bio changed", "new post"]


def test_unknown_account_is_gone_and_a_down_instance_is_an_error() -> None:
    assert (
        MastodonChecker().read(URL, None, ctx(account=(404, {"error": "Record not found"}))).status == "gone"
    )
    assert MastodonChecker().read(URL, None, ctx(account=(503, "down"))).status == "error"
