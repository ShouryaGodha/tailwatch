from __future__ import annotations

from typing import Any

import pytest

from tailwatch.connectors.base import CheckContext
from tailwatch.connectors.http import Call, Http, HttpError, Response, fixture_transport
from tailwatch.connectors.linkedin import DEFAULT_ACTOR, LinkedInChecker

URL = "https://www.linkedin.com/in/ada"
RUN = "https://api.apify.com/v2/acts/"
ENV = {"APIFY_API_KEY": "key"}
PROFILE = {"basic_info": {"fullname": "Ada Lovelace", "headline": "Research scientist at Analytical Engines",
                          "location": {"full": "London, England"}, "open_to_work": False, "about": "Engines.",
                          "follower_count": 501},
           "experience": [
               {"title": "Research scientist", "company": "Analytical Engines", "start_date": {"year": 2024, "month": "Mar"},
                "is_current": True},
               {"title": "Student", "company": "University of London", "start_date": {"year": 2020, "month": 9},
                "end_date": {"year": 2024, "month": 2}, "is_current": False}]}


class Fake:
    """An Apify endpoint answering one dataset item, and a dict standing in for the cache."""

    def __init__(self, item: Any = None, status: int = 201) -> None:
        self.item, self.status, self.calls, self.cache = item if item is not None else PROFILE, status, [], {}

    def ctx(self, env: dict[str, str] | None = None, **options: Any) -> CheckContext:
        def run(call: Call) -> Response:
            self.calls.append(call)
            import json
            body = self.item if isinstance(self.item, str) else json.dumps([self.item] if self.item != [] else [])
            return Response(self.status, body)
        return CheckContext(http=Http(transport=fixture_transport({RUN: run})), env=ENV if env is None else env,
                            options=options, cache_get=lambda p, k, age: self.cache.get((p, k)),
                            cache_put=lambda p, k, v: self.cache.__setitem__((p, k), v))


def with_experience(*jobs: dict[str, Any], **basic: Any) -> dict[str, Any]:
    return {"basic_info": {**PROFILE["basic_info"], **basic}, "experience": list(jobs)}


def test_first_read_normalises_positions_and_caches_the_answer() -> None:
    fake = Fake()
    read = LinkedInChecker().read(URL, None, fake.ctx())
    assert read.status == "ok" and read.usd == pytest.approx(0.005)
    content = read.content
    assert content["name"] == "Ada Lovelace" and content["location"] == "London, England"
    assert content["organization"] == "Analytical Engines" and content["title"] == "Research scientist"
    assert content["flags"] == {"open_to_work": False}
    assert content["positions"] == [
        {"title": "Research scientist", "company": "Analytical Engines", "start": "2024-03", "end": "", "current": True},
        {"title": "Student", "company": "University of London", "start": "2020-09", "end": "2024-02", "current": False}]
    assert fake.cache[("linkedin", URL)] == PROFILE
    call = fake.calls[0]
    assert call.url == f"{RUN}{DEFAULT_ACTOR}/run-sync-get-dataset-items"
    assert call.json == {"username": URL, "includeEmail": False}
    assert call.headers["Authorization"] == "Bearer key" and "key" not in str(call.params)


def test_a_fresh_cached_answer_is_reused_without_a_run() -> None:
    fake = Fake()
    fake.cache[("linkedin", URL)] = PROFILE
    read = LinkedInChecker().read(URL, None, fake.ctx())
    assert read.status == "ok" and fake.calls == [] and read.usd == 0


def test_a_cached_partial_scrape_is_not_reused() -> None:
    checker = LinkedInChecker()
    fake = Fake()
    before = checker.read(URL, None, fake.ctx()).content
    fake.cache[("linkedin", URL)] = with_experience()  # no experience
    read = checker.read(URL, before, fake.ctx())
    assert len(fake.calls) == 2 and read.content["positions"]


def test_actor_and_input_are_configurable() -> None:
    fake = Fake()
    LinkedInChecker().read(URL, None, fake.ctx(actor="someone/profile-actor", input={"profileUrls": ["{url}"], "x": 1}))
    assert "/acts/someone~profile-actor/" in fake.calls[0].url
    assert fake.calls[0].json == {"profileUrls": [URL], "x": 1}
    fake = Fake()
    LinkedInChecker().read(URL, None, fake.ctx(env={**ENV, "APIFY_LINKEDIN_ACTOR": "env~actor"}))
    assert "/acts/env~actor/" in fake.calls[0].url


def test_unchanged_read_gives_no_changes() -> None:
    checker = LinkedInChecker()
    before = checker.read(URL, None, Fake().ctx()).content
    shuffled = {**PROFILE, "experience": list(reversed(PROFILE["experience"]))}
    after = checker.read(URL, before, Fake(shuffled).ctx()).content
    assert checker.diff(before, after) == []


def test_role_ended_with_its_end_date_and_role_added() -> None:
    checker = LinkedInChecker()
    before = checker.read(URL, None, Fake().ctx()).content
    item = with_experience(
        {"title": "Founder", "company": "Babbage Labs", "start_date": {"year": 2026, "month": 10}, "is_current": True},
        {"title": "Research scientist", "company": "Analytical Engines", "start_date": {"year": 2024, "month": 3},
         "end_date": {"year": 2026, "month": 9}, "is_current": False},
        PROFILE["experience"][1], headline="Founder at Babbage Labs")
    after = checker.read(URL, before, Fake(item).ctx()).content
    changes = checker.diff(before, after)
    assert [c.label for c in changes] == ["headline changed", "role ended", "role added"]
    ended, added = changes[1], changes[2]
    assert ended.before == "Research scientist at Analytical Engines" and ended.content_date == "2026-09"
    assert added.after.startswith("Founder at Babbage Labs") and added.content_date == "2026-10"


def test_a_removed_current_role_is_role_ended_without_a_date() -> None:
    checker = LinkedInChecker()
    before = checker.read(URL, None, Fake().ctx()).content
    after = checker.read(URL, before, Fake(with_experience(PROFILE["experience"][1])).ctx()).content
    changes = checker.diff(before, after)
    assert [c.label for c in changes] == ["role ended"] and changes[0].content_date == ""


def test_a_new_title_at_the_same_company_is_one_title_change() -> None:
    checker = LinkedInChecker()
    before = checker.read(URL, None, Fake().ctx()).content
    promoted = with_experience({"title": "Principal scientist", "company": "Analytical Engines",
                                "start_date": {"year": 2026, "month": 6}, "is_current": True}, PROFILE["experience"][1])
    changes = checker.diff(before, checker.read(URL, before, Fake(promoted).ctx()).content)
    assert [c.label for c in changes] == ["title changed"]
    assert changes[0].after == "Principal scientist at Analytical Engines"


def test_open_to_work_flag_and_missing_flag() -> None:
    checker = LinkedInChecker()
    before = checker.read(URL, None, Fake().ctx()).content
    after = checker.read(URL, before, Fake(with_experience(*PROFILE["experience"], open_to_work=True)).ctx()).content
    assert [c.label for c in checker.diff(before, after)] == ["open to work flag changed"]
    no_flag = dict(PROFILE["basic_info"])
    no_flag.pop("open_to_work")
    silent = checker.read(URL, before, Fake({"basic_info": no_flag, "experience": PROFILE["experience"]}).ctx()).content
    assert checker.diff(before, silent) == []


def test_the_other_payload_shape_is_read() -> None:
    item = {"firstName": "Ada", "lastName": "Lovelace", "headline": "Scientist", "openToWork": True,
            "experience": [{"position": "Scientist", "companyName": "Analytical Engines",
                            "startDate": {"month": "Mar", "year": 2024}, "endDate": {"text": "Present"}}]}
    content = LinkedInChecker().read(URL, None, Fake(item).ctx()).content
    assert content["name"] == "Ada Lovelace" and content["flags"] == {"open_to_work": True}
    assert content["positions"][0] == {"title": "Scientist", "company": "Analytical Engines", "start": "2024-03",
                                       "end": "", "current": True}


def test_empty_experience_after_positions_is_guarded() -> None:
    checker = LinkedInChecker()
    before = checker.read(URL, None, Fake().ctx()).content
    after = checker.read(URL, before, Fake(with_experience()).ctx()).content
    assert checker.guard(before, after)


def test_no_profile_found_is_gone_and_a_non_profile_url_is_our_error() -> None:
    gone = LinkedInChecker().read(URL, None, Fake({"message": "No profile found or wrong input"}).ctx())
    assert gone.status == "gone"
    assert LinkedInChecker().read("https://www.linkedin.com/company/x", None, Fake().ctx()).status == "error"


@pytest.mark.parametrize(("status", "outage"), [(402, "out of credits"), (401, "key rejected"),
                                                (429, "rate limited"), (500, "")])
def test_provider_failures_are_errors_not_gone(status: int, outage: str) -> None:
    read = LinkedInChecker().read(URL, None, Fake(status=status).ctx())
    assert read.status == "error" and read.outage == outage


def test_an_empty_dataset_and_a_timeout_are_errors() -> None:
    assert LinkedInChecker().read(URL, None, Fake([]).ctx()).status == "error"

    def timeout(call: Call) -> Any:
        raise HttpError("timeout")
    ctx = CheckContext(http=Http(transport=fixture_transport({RUN: timeout})), env=ENV)
    assert LinkedInChecker().read(URL, None, ctx).status == "error"


def test_a_refused_key_moves_to_the_next() -> None:
    seen: list[str] = []

    def run(call: Call) -> Response:
        seen.append(call.headers["Authorization"])
        import json
        if call.headers["Authorization"] == "Bearer k1":
            return Response(402, '{"error": {"type": "not-enough-usage-to-run-paid-actor"}}')
        return Response(201, json.dumps([PROFILE]))
    ctx = CheckContext(http=Http(transport=fixture_transport({RUN: run})), env={"APIFY_API_KEY": "k1,k2"})
    assert LinkedInChecker().read(URL, None, ctx).status == "ok" and seen == ["Bearer k1", "Bearer k2"]
