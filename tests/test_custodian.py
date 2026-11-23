"""The custodian: scans write an issue once, keep their rota and refuse a flood; playbooks are
idempotent and capped; the tick runs at most 30, three attempts each, moves the card behind
an issue, and stops when the desk is paused."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from conftest import FakeChecker, verdict
from tailwatch import util
from tailwatch.connectors.base import Read
from tailwatch.desk import custodian, playbooks, scans


def _person(store: Any, name: str = "Ada Lovelace", url: str = "https://ada.example", **kw: Any) -> tuple[int, int]:
    person_id, _ = store.add_person(name, organization="Analytical Engines", title="Researcher",
                                    links={"homepage": url, **kw.pop("links", {})})
    source_id = store.ensure_source(person_id, "fake", url, cadence_hours=24)
    return person_id, source_id


def _broken(store: Any, source_id: int) -> None:
    store.set_source_status(source_id, "broken", "404 twice")


def _parked(store: Any, person_id: int, source_id: int) -> int:
    observation = store.record_check(store.source(source_id), status="ok", content={"bio": "x"},
                                     changes=[{"label": "bio changed"}])
    store.judge_failed(observation, "bad answer", park_after=1)
    return observation


def _review_fixing(store: Any, person_id: int, source_id: int, content: dict[str, Any] | None = None) -> int:
    observation = store.record_check(store.source(source_id), status="ok", content=content or {"bio": "x"},
                                     changes=[{"label": "bio changed"}])
    verdict_id = store.save_verdict(observation, person_id, verdict("left"))
    review = store.open_review(store.verdict(verdict_id))
    store.set_review(review["id"], state="fixing")
    return int(review["id"])


# -- scans ----------------------------------------------------------------------------------------
def test_a_scan_opens_an_issue_once(store: Any, config: Any) -> None:
    person_id, _ = store.add_person("Grace Hopper", links={"github": "grace"})
    first = scans.run(store, config, {}, ("no_active_source",), checkers={})
    assert first["opened"] == {"no_active_source": 1}
    again = scans.run(store, config, {}, ("no_active_source",), force=True, checkers={})
    assert again["opened"] == {"no_active_source": 0}
    (issue,) = store.issues()
    assert issue["subject_key"] == f"person:{person_id}" and issue["playbook"] == "fill_links"


def test_the_rota_and_the_issue_cooldown(store: Any, config: Any) -> None:
    store.add_person("Grace Hopper", links={"github": "grace"})
    scans.run(store, config, {}, ("no_active_source",), checkers={})
    assert scans.run(store, config, {}, ("no_active_source",), checkers={})["ran"] == []
    (issue,) = store.issues()
    store.close_issue(issue["id"], "fixed")
    util.set_now(util.now() + timedelta(hours=7))
    rerun = scans.run(store, config, {}, ("no_active_source",), checkers={})
    assert rerun["ran"] == ["no_active_source"] and rerun["opened"] == {"no_active_source": 0}  # 30-day cooldown
    util.set_now(util.now() + timedelta(days=31))
    assert scans.run(store, config, {}, ("no_active_source",), checkers={})["opened"] == {"no_active_source": 1}


def test_a_flood_writes_nothing(store: Any, config: Any) -> None:
    for i in range(scans.FLOOD + 1):
        store.add_person(f"Person {i}", links={"github": f"p{i}"})
    out = scans.run(store, config, {}, ("no_active_source",), checkers={})
    assert out["flood"] == ["no_active_source"] and store.issues() == []


def test_shared_links_need_an_account_to_merge(store: Any, config: Any) -> None:
    a, _ = store.add_person("Ada Lovelace", links={"github": "ada"})
    b, _ = store.add_person("A. Lovelace", links={"homepage": "https://lab.example"})
    store.ensure_source(b, "github", "https://github.com/ada", cadence_hours=24)
    c, _ = store.add_person("Charles Babbage", links={"github": "charles"})
    store.ensure_source(c, "homepage", "https://lab.example", cadence_hours=24)
    scans.run(store, config, {}, ("shared_links",), checkers={})
    found = {i["subject_key"]: i for i in store.issues()}
    assert found[f"people:{a}+{b}"]["playbook"] == "merge_people"
    assert found[f"people:{b}+{c}"]["playbook"] == ""  # a shared page is not proof


def test_broken_and_parked_scans(store: Any, config: Any) -> None:
    person_id, source_id = _person(store)
    _broken(store, source_id)
    observation = _parked(store, person_id, store.ensure_source(person_id, "fake", "https://ada.example/two", cadence_hours=24))
    out = scans.run(store, config, {}, ("broken_sources", "parked_checks"), checkers={"fake": FakeChecker()})
    assert out["opened"] == {"broken_sources": 1, "parked_checks": 1}
    kinds = {i["kind"]: i for i in store.issues()}
    assert kinds["broken_source"]["args"]["source_id"] == source_id
    assert kinds["parked_check"]["args"]["observation_id"] == observation


def test_candidates_waiting_over_a_week(store: Any, config: Any) -> None:
    candidate_id, _ = store.add_candidate("news", "1", text="someone moved", state="needs_review")
    assert scans.run(store, config, {}, ("candidates_waiting",), checkers={})["opened"] == {"candidates_waiting": 0}
    util.set_now(util.now() + timedelta(days=8))
    scans.run(store, config, {}, ("candidates_waiting",), force=True, checkers={})
    (issue,) = store.issues()
    assert f"#C{candidate_id}" in issue["note"] and issue["playbook"] == ""


# -- playbooks ------------------------------------------------------------------------------------
def test_reopen_source_probes_once(store: Any, config: Any) -> None:
    _person_id, source_id = _person(store)
    _broken(store, source_id)
    checker = FakeChecker({"https://ada.example": None})
    still = playbooks.run("reopen_source", store, config, {}, {"source_id": source_id}, checkers={"fake": checker})
    assert still["changed"] is False and store.source(source_id)["status"] == "broken"
    checker.pages["https://ada.example"] = {"bio": "back"}
    again = playbooks.run("reopen_source", store, config, {}, {"source_id": source_id}, checkers={"fake": checker})
    assert "once in 30 days" in again["refused"]
    util.set_now(util.now() + timedelta(days=31))
    dry = playbooks.run("reopen_source", store, config, {}, {"source_id": source_id}, dry_run=True, checkers={"fake": checker})
    assert dry["dry_run"] and store.source(source_id)["status"] == "broken"
    done = playbooks.run("reopen_source", store, config, {}, {"source_id": source_id}, checkers={"fake": checker})
    assert done["changed"] and store.source(source_id)["status"] == "active"
    assert playbooks.run("reopen_source", store, config, {}, {"source_id": source_id}, checkers={"fake": checker})["changed"] is False


def test_unpark_once_per_prompt_version(store: Any, config: Any) -> None:
    person_id, source_id = _person(store)
    observation = _parked(store, person_id, source_id)
    first = playbooks.run("unpark_event", store, config, {}, {"observation_id": observation})
    assert first["changed"] and store.observation(observation)["judge_state"] == "pending"
    store.judge_failed(observation, "bad again", park_after=1)
    second = playbooks.run("unpark_event", store, config, {}, {"observation_id": observation})
    assert "once under" in second["refused"]


def test_clean_settings_keeps_the_deny_list(store: Any, config: Any) -> None:
    store.set_setting("DAY:seen:2025-01-01", 1)
    store.set_setting("DESK:PAUSED", "")
    store.set_setting("DAY:INTEL:RULES", ["keep me"])
    util.set_now(util.now() + timedelta(days=91))
    refused = playbooks.run("clean_settings", store, config, {}, {"prefix": "DESK:"})
    assert refused["refused_prefixes"] == ["DESK:"] and not refused["changed"]
    assert playbooks.run("clean_settings", store, config, {}, {"days": 7})["refused"]
    done = playbooks.run("clean_settings", store, config, {}, {})
    assert done["changed"] and store.get_setting("DAY:seen:2025-01-01") is None
    assert store.get_setting("DAY:INTEL:RULES") == ["keep me"] and store.setting_row("DESK:PAUSED") is not None
    assert playbooks.run("clean_settings", store, config, {}, {})["changed"] is False


def test_merge_needs_proof_both_rows_hold(store: Any, config: Any) -> None:
    a, _ = store.add_person("Ada Lovelace", links={"github": "ada"})
    b, _ = store.add_person("A. Lovelace", links={"linkedin": "https://linkedin.com/in/ada-l"})
    refused = playbooks.run("merge_people", store, config, {}, {"keep_id": a, "drop_id": b, "proof": "github:ada"})
    assert "not held by both" in refused["refused"]
    store.ensure_source(b, "github", "https://github.com/ada", cadence_hours=24)
    assert "needs proof" in playbooks.run("merge_people", store, config, {},
                                          {"keep_id": a, "drop_id": b, "proof": "homepage:lab.example"})["refused"]
    done = playbooks.run("merge_people", store, config, {}, {"keep_id": a, "drop_id": b, "proof": "github:ada"})
    assert done["changed"] and store.person(a)["status"] == "ignored"  # the LinkedIn row is kept
    again = playbooks.run("merge_people", store, config, {}, {"keep_id": a, "drop_id": b, "proof": "github:ada"})
    assert again["changed"] is False


def test_fix_card_fields_needs_the_quote_in_the_read(store: Any, config: Any) -> None:
    person_id, source_id = _person(store)
    review_id = _review_fixing(store, person_id, source_id, {"bio": "Founder at Difference Labs since May"})
    args = {"review_id": review_id, "organization": "Difference Labs", "title": "Founder", "quote": "Founder at Difference Labs"}
    missing = playbooks.run("fix_card_fields", store, config, {}, {**args, "quote": "CEO of Difference Labs"})
    assert "not in the latest read" in missing["refused"]
    done = playbooks.run("fix_card_fields", store, config, {}, args)
    assert done["changed"] and store.person(person_id)["organization"] == "Difference Labs"
    assert playbooks.run("fix_card_fields", store, config, {}, args)["changed"] is False


def test_three_runs_a_person_a_day(store: Any, config: Any) -> None:
    person_id, source_id = _person(store)
    parked = [_parked(store, person_id, source_id) for _ in range(4)]
    results = [playbooks.run("unpark_event", store, config, {}, {"observation_id": o, "person_id": person_id}) for o in parked]
    assert [r["changed"] for r in results] == [True, True, True, False] and results[3]["retry_later"]
    util.set_now(util.now() + timedelta(days=1))
    assert playbooks.run("unpark_event", store, config, {}, {"observation_id": parked[3], "person_id": person_id})["changed"]


def test_fill_links_ten_a_day(store: Any, config: Any) -> None:
    person_id, _ = store.add_person("Grace Hopper", links={"github": "grace"})
    store.set_setting(f"{playbooks.COUNT_KEY}fill_links:{util.local_day()}", playbooks.FILL_LINKS_PER_DAY)
    capped = playbooks.run("fill_links", store, config, {}, {"person_id": person_id})
    assert capped["retry_later"] and capped["cap"] == "playbook"


def test_mark_wrong_person_asks_for_the_right_link(store: Any, config: Any) -> None:
    _person_id, source_id = _person(store)
    done = playbooks.run("mark_wrong_person", store, config, {}, {"source_id": source_id, "reason": "another Ada"})
    assert done["changed"] and store.source(source_id)["status"] == "wrong_person"
    assert store.issues()[0]["playbook"] == "fill_links"
    assert playbooks.run("mark_wrong_person", store, config, {}, {"source_id": source_id})["changed"] is False


def test_only_known_playbooks_run(store: Any, config: Any) -> None:
    import pytest

    with pytest.raises(playbooks.PlaybookError):
        playbooks.run("remove_person", store, config, {}, {"person_id": 1})
    assert "remove_person" not in playbooks.KNOWN and "set_status" not in playbooks.KNOWN


# -- the tick -------------------------------------------------------------------------------------
def test_a_fix_that_changed_something_reopens_the_card(store: Any, config: Any) -> None:
    person_id, source_id = _person(store)
    review_id = _review_fixing(store, person_id, source_id)
    _broken(store, source_id)
    store.open_issue("broken_source", f"source:{source_id}", playbook="reopen_source", args={"source_id": source_id},
                     found_by="editor", review_id=review_id)
    checker = FakeChecker({"https://ada.example": {"bio": "back"}})
    out = custodian.run(store, config, {}, checkers={"fake": checker})
    assert out["drained"]["fixed"] == 1 and out["drained"]["reviews_reopened"] == 1
    review = store.review(review_id)
    assert review["state"] == "pending" and review["rounds"] == 1
    assert store.issues(state="fixed")[0]["result"]["changed"] is True


def test_a_fix_that_changed_nothing_holds_the_card(store: Any, config: Any) -> None:
    person_id, source_id = _person(store)
    review_id = _review_fixing(store, person_id, source_id)
    _broken(store, source_id)
    store.open_issue("broken_source", f"source:{source_id}", playbook="reopen_source", args={"source_id": source_id},
                     found_by="editor", review_id=review_id)
    out = custodian.run(store, config, {}, checkers={"fake": FakeChecker({"https://ada.example": None})})
    assert out["drained"]["noop"] == 1 and store.review(review_id)["state"] == "held"
    assert "changed nothing" in store.review(review_id)["note"]


def test_thirty_a_tick_and_three_attempts(store: Any, config: Any) -> None:
    for i in range(35):
        store.open_issue("parked_check", f"observation:{9000 + i}", playbook="unpark_event",
                         args={"observation_id": 9000 + i}, found_by="test")
    first = custodian.drain(store, config, {}, checkers={})
    assert first["run"] == custodian.ISSUES_PER_TICK and first["failed"] == 30
    assert all(i["attempts"] == 1 for i in store.issues(state="open")[:30])
    for _ in range(4):
        custodian.drain(store, config, {}, checkers={})
    assert len(store.issues(state="failed", limit=100)) == 35 and store.issues(state="open") == []


def test_deferred_by_a_cap_keeps_its_attempts(store: Any, config: Any) -> None:
    person_id, _ = store.add_person("Grace Hopper", links={"github": "grace"})
    store.set_setting(f"{playbooks.COUNT_KEY}fill_links:{util.local_day()}", playbooks.FILL_LINKS_PER_DAY)
    issue_id, _ = store.open_issue("no_active_source", f"person:{person_id}", playbook="fill_links",
                                   args={"person_id": person_id}, found_by="test")
    out = custodian.drain(store, config, {}, checkers={})
    assert out["deferred"] == 1 and store.issue(issue_id)["state"] == "open" and store.issue(issue_id)["attempts"] == 0


def test_an_issue_with_no_playbook_closes_when_it_has_moved_on(store: Any, config: Any) -> None:
    issue_id, _ = store.open_issue("candidates_waiting", "candidates:needs_review", found_by="test")
    custodian.drain(store, config, {}, checkers={})
    assert store.issue(issue_id)["state"] == "open"
    util.set_now(util.now() + timedelta(days=8))
    custodian.drain(store, config, {}, checkers={})
    assert store.issue(issue_id)["state"] == "wontfix"


def test_pause_stops_the_custodian(store: Any, config: Any) -> None:
    store.set_setting("DESK:PAUSED", {"reason": "migrating"})
    store.open_issue("parked_check", "observation:1", playbook="unpark_event", args={"observation_id": 1}, found_by="test")
    assert custodian.run(store, config, {}, checkers={}) == {"stopped": "paused: migrating"}
    assert store.issues()[0]["attempts"] == 0
    store.set_setting("DESK:PAUSED", "")
    assert "drained" in custodian.run(store, config, {}, checkers={})


def test_the_tick_runs_the_scans(store: Any, config: Any) -> None:
    store.add_person("Grace Hopper", links={"github": "grace"})
    out = custodian.run(store, config, {}, checkers={})
    assert "no_active_source" in out["scans"]["ran"] and store.issues()[0]["kind"] == "no_active_source"


def test_a_failing_read_while_probing_is_no_change(store: Any, config: Any) -> None:
    _person_id, source_id = _person(store)
    _broken(store, source_id)
    checker = FakeChecker({"https://ada.example": Read.failed("timeout")})
    result = playbooks.run("reopen_source", store, config, {}, {"source_id": source_id}, checkers={"fake": checker})
    assert result["changed"] is False and "still answers error" in result["what"]
