from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tailwatch import util
from tailwatch.connectors.base import CheckContext
from tailwatch.connectors.http import Call, Http, HttpError, fixture_transport
from tailwatch.connectors.x import XChecker

URL = "https://x.com/ada"
API = "https://api.twitterapi.io/twitter/user"
ENV = {"TWITTERAPI_IO_KEY": "key"}
NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
USER = {"id": "42", "userName": "Ada", "name": "Ada Lovelace", "location": "London", "statusesCount": 100,
        "followers": 12345, "profile_bio": {"description": "Engines. https://t.co/abc",
                                            "entities": {"description": {"urls": [{"url": "https://t.co/abc", "expanded_url": "https://ada.dev"}]},
                                                         "url": {"urls": [{"url": "https://t.co/w", "expanded_url": "https://ada.dev"}]}}}}


def tweet(tid: int, text: str, retweet: bool = False) -> dict[str, Any]:
    item: dict[str, Any] = {"id": str(tid), "text": text, "createdAt": "Mon Nov 23 10:00:00 +0000 2026",
                            "url": f"https://x.com/Ada/status/{tid}"}
    if retweet:
        item["retweeted_tweet"] = {"id": "1"}
    return item


TWEETS = [tweet(1002, "Second"), tweet(1001, "First"), tweet(999, "RT someone", retweet=True)]


@pytest.fixture(autouse=True)
def frozen() -> Iterator[None]:
    util.set_now(NOW)
    yield
    util.set_now(None)


class Recorder:
    def __init__(self, user: Any = None, tweets: Any = None) -> None:
        self.user, self.tweets, self.calls = user, tweets, []

    def routes(self) -> dict[str, Any]:
        def info(call: Call) -> Any:
            self.calls.append("info")
            return self.user if self.user is not None else {"status": "success", "data": USER}

        def posts(call: Call) -> Any:
            self.calls.append("posts")
            return self.tweets if self.tweets is not None else {"status": "success", "data": {"tweets": TWEETS}}
        return {f"{API}/info": info, f"{API}/last_tweets": posts}

    def ctx(self, env: dict[str, str] | None = None) -> CheckContext:
        return CheckContext(http=Http(transport=fixture_transport(self.routes())), env=ENV if env is None else env)


def test_first_read_reads_profile_and_posts_and_reports_credits() -> None:
    rec = Recorder()
    read = XChecker().read(URL, None, rec.ctx())
    assert read.status == "ok" and rec.calls == ["info", "posts"]
    content = read.content
    assert content["bio"] == "Engines. https://ada.dev" and content["website"] == "https://ada.dev"
    assert content["handle"] == "ada" and content["statuses"] == 100 and "followers" not in content
    assert [p["text"] for p in content["posts"]] == ["Second", "First"]  # the repost is left out
    assert content["posts"][0]["date"] == "2026-11-23T10:00:00Z"
    assert content["posts_fetched_at"].startswith("2026-11-23T12:00")
    assert read.credits == 18 + 15 * 3  # every post returned is billed, reposts included
    assert read.usd == pytest.approx(read.credits / 100_000)


def test_unchanged_post_count_skips_the_posts_call_and_costs_only_the_profile() -> None:
    checker = XChecker()
    before = checker.read(URL, None, Recorder().ctx()).content
    rec = Recorder()
    read = checker.read(URL, before, rec.ctx())
    assert rec.calls == ["info"] and read.credits == 18
    assert read.content["posts"] == before["posts"] and read.raw["posts_skipped"] is True
    assert checker.diff(before, read.content) == []


def test_posts_are_fetched_again_when_the_count_moves_or_after_two_weeks() -> None:
    checker = XChecker()
    before = checker.read(URL, None, Recorder().ctx()).content
    rec = Recorder(user={"status": "success", "data": {**USER, "statusesCount": 101}},
                   tweets={"data": {"tweets": [tweet(1003, "Joined Babbage Labs"), *TWEETS]}})
    after = checker.read(URL, before, rec.ctx()).content
    assert rec.calls == ["info", "posts"]
    changes = checker.diff(before, after)
    assert [c.label for c in changes] == ["new post"]
    assert changes[0].after.startswith("Joined Babbage Labs") and changes[0].content_date == "2026-11-23"

    stale = {**before, "posts_fetched_at": util.iso(NOW - timedelta(days=14))}
    rec = Recorder()
    checker.read(URL, stale, rec.ctx())
    assert rec.calls == ["info", "posts"]


def test_profile_field_changes() -> None:
    checker = XChecker()
    before = checker.read(URL, None, Recorder().ctx()).content
    user = {**USER, "name": "Ada L.", "location": "Paris", "profile_bio": {"description": "Babbage Labs."}}
    after = checker.read(URL, before, Recorder(user={"data": user}).ctx()).content
    assert [c.label for c in checker.diff(before, after)] == ["name changed", "bio changed", "location changed",
                                                              "website changed"]


def test_an_old_post_sliding_back_is_not_new() -> None:
    checker = XChecker()
    before = checker.read(URL, None, Recorder().ctx()).content
    rec = Recorder(user={"data": {**USER, "statusesCount": 99}}, tweets={"tweets": [tweet(1001, "First"), tweet(900, "Old")]})
    assert checker.diff(before, checker.read(URL, before, rec.ctx()).content) == []


@pytest.mark.parametrize("answer", [{"status": "error", "msg": "User not found"},
                                    (404, {"status": "error", "message": "user does not exist"}),
                                    {"data": {"unavailable": True, "unavailableReason": "Suspended"}}])
def test_gone(answer: Any) -> None:
    read = XChecker().read(URL, None, Recorder(user=answer).ctx())
    assert read.status == "gone"


@pytest.mark.parametrize(("status", "outage"), [(402, "out of credits"), (401, "key rejected"),
                                                (403, "key rejected"), (429, "rate limited"), (503, "")])
def test_refusals_are_errors_with_outages(status: int, outage: str) -> None:
    read = XChecker().read(URL, None, Recorder(user=(status, {"msg": "nope"})).ctx())
    assert read.status == "error" and read.outage == outage and read.credits == 0


def test_a_refused_posts_call_keeps_the_profile_credits() -> None:
    read = XChecker().read(URL, None, Recorder(tweets=(402, {"msg": "Credits exhausted"})).ctx())
    assert read.status == "error" and read.outage == "out of credits" and read.credits == 18


def test_timeout_is_an_error() -> None:
    def timeout(call: Call) -> Any:
        raise HttpError("timeout")
    ctx = CheckContext(http=Http(transport=fixture_transport({f"{API}/info": timeout})), env=ENV)
    assert XChecker().read(URL, None, ctx).status == "error"


def test_missing_key_is_an_outage_without_a_call() -> None:
    rec = Recorder()
    read = XChecker().read(URL, None, rec.ctx(env={}))
    assert read.status == "error" and read.outage == "no credential" and rec.calls == []


def test_zero_posts_after_posts_is_guarded() -> None:
    checker = XChecker()
    before = checker.read(URL, None, Recorder().ctx()).content
    rec = Recorder(user={"data": {**USER, "statusesCount": 101}}, tweets={"tweets": []})
    after = checker.read(URL, before, rec.ctx()).content
    assert after["posts"] == [] and checker.guard(before, after)
    assert rec.calls == ["info", "posts"]


def test_a_protected_account_is_read_without_posts_and_reopening_replays_nothing() -> None:
    checker = XChecker()
    before = checker.read(URL, None, Recorder().ctx()).content
    rec = Recorder(user={"data": {**USER, "protected": True}})
    closed = checker.read(URL, before, rec.ctx()).content
    assert rec.calls == ["info"] and closed["posts"] == [] and checker.guard(before, closed) == ""
    assert [c.label for c in checker.diff(before, closed)] == ["protected flag changed"]
    reopened = checker.read(URL, closed, Recorder().ctx()).content
    assert [c.label for c in checker.diff(closed, reopened)] == ["protected flag changed"]


def test_another_account_behind_the_handle_is_a_new_baseline() -> None:
    checker = XChecker()
    before = checker.read(URL, None, Recorder().ctx()).content
    other = checker.read(URL, before, Recorder(user={"data": {**USER, "id": "77", "name": "Someone"}}).ctx()).content
    assert checker.diff(before, other) == []
