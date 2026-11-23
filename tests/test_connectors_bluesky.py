from __future__ import annotations

from typing import Any

from tailwatch.connectors.base import CheckContext
from tailwatch.connectors.bluesky import BlueskyChecker
from tailwatch.connectors.http import Call, Http, HttpError, fixture_transport

URL = "https://bsky.app/profile/ada.bsky.social"
XRPC = "https://public.api.bsky.app/xrpc"
DID = "did:plc:ada"
PROFILE = {"did": DID, "handle": "ada.bsky.social", "displayName": "Ada Lovelace", "description": "Engines.",
           "followersCount": 10}


def post(rkey: str, text: str, at: str, did: str = DID, reason: dict | None = None) -> dict:
    item: dict[str, Any] = {"post": {"uri": f"at://{did}/app.bsky.feed.post/{rkey}", "author": {"did": did},
                                     "record": {"text": text, "createdAt": at}, "indexedAt": at}}
    if reason:
        item["reason"] = reason
    return item


FEED = {"feed": [post("3b", "Second post", "2026-03-02T00:00:00Z"),
                 post("3r", "Someone else's post I reposted", "2026-03-01T12:00:00Z", did="did:plc:other",
                      reason={"$type": "app.bsky.feed.defs#reasonRepost"}),
                 post("3a", "First post", "2026-03-01T00:00:00Z")]}


def ctx(profile: Any = None, feed: Any = None) -> CheckContext:
    routes = {f"{XRPC}/app.bsky.actor.getProfile": profile if profile is not None else PROFILE,
              f"{XRPC}/app.bsky.feed.getAuthorFeed": feed if feed is not None else FEED}
    return CheckContext(http=Http(transport=fixture_transport(routes)))


def test_first_read_keeps_own_posts_only() -> None:
    read = BlueskyChecker().read(URL, None, ctx())
    assert read.status == "ok"
    assert read.content["name"] == "Ada Lovelace" and read.content["bio"] == "Engines."
    posts = read.content["posts"]
    assert [p["text"] for p in posts] == ["Second post", "First post"]
    assert posts[0]["url"] == "https://bsky.app/profile/ada.bsky.social/post/3b"


def test_unchanged_read_gives_no_changes() -> None:
    checker = BlueskyChecker()
    before = checker.read(URL, None, ctx()).content
    assert checker.diff(before, checker.read(URL, before, ctx()).content) == []


def test_name_bio_and_new_post() -> None:
    checker = BlueskyChecker()
    before = checker.read(URL, None, ctx()).content
    feed = {"feed": [post("3c", "Joined Babbage Labs!", "2026-03-03T00:00:00Z"), *FEED["feed"]]}
    after = checker.read(URL, before, ctx(profile={**PROFILE, "displayName": "Ada L.", "description": "Babbage Labs."},
                                          feed=feed)).content
    changes = checker.diff(before, after)
    assert [c.label for c in changes] == ["name changed", "bio changed", "new post"]
    assert changes[2].after.startswith("Joined Babbage Labs!") and changes[2].content_date == "2026-03-03"


def test_a_repost_is_never_a_new_post() -> None:
    checker = BlueskyChecker()
    before = checker.read(URL, None, ctx()).content
    feed = {"feed": [post("3z", "A repost", "2026-03-04T00:00:00Z", did="did:plc:other",
                          reason={"$type": "app.bsky.feed.defs#reasonRepost"}), *FEED["feed"]]}
    assert checker.diff(before, checker.read(URL, before, ctx(feed=feed)).content) == []


def test_profile_not_found_is_gone_and_other_failures_are_errors() -> None:
    gone = BlueskyChecker().read(URL, None, ctx(profile=(400, {"error": "InvalidRequest", "message": "Profile not found"})))
    assert gone.status == "gone"
    assert BlueskyChecker().read(URL, None, ctx(profile=(502, "bad gateway"))).status == "error"
    limited = BlueskyChecker().read(URL, None, ctx(feed=(429, {"error": "RateLimitExceeded"})))
    assert limited.status == "error" and limited.outage == "rate limited"

    def timeout(call: Call) -> Any:
        raise HttpError("timeout")
    assert BlueskyChecker().read(URL, None, ctx(profile=timeout)).status == "error"


def test_empty_feed_after_posts_is_guarded() -> None:
    checker = BlueskyChecker()
    before = checker.read(URL, None, ctx()).content
    after = checker.read(URL, before, ctx(feed={"feed": []})).content
    assert checker.guard(before, after)
