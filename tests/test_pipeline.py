"""The core flow end to end: check → events → judge → digest, with a fake checker and a
scripted model."""

from __future__ import annotations

import json
from typing import Any

from conftest import FakeChecker, verdict

from tailwatch import util
from tailwatch.pipeline import digest, judge, runner
from tailwatch.store import open_store


def _person(store: Any) -> tuple[int, int]:
    person_id, _ = store.add_person("Ada Lovelace", organization="Analytical Engines", title="Researcher",
                                    links={"homepage": "https://ada.example"})
    source_id = store.ensure_source(person_id, "fake", "https://ada.example", cadence_hours=24)
    return person_id, source_id


def _pass(store: Any, config: Any, checker: FakeChecker) -> dict[str, Any]:
    ctx = runner.context_for(store, config, {}, "fake")
    return runner.run_kind(store, config, {}, "fake", checker, limit=100) if ctx else {}


def test_first_read_records_only_then_changes_become_events(store: Any, config: Any) -> None:
    _person_id, _ = _person(store)
    checker = FakeChecker({"https://ada.example": {"bio": "Researcher at Analytical Engines", "organization": "Analytical Engines"}})
    first = _pass(store, config, checker)
    assert first["checked"] == 1 and first["events"] == 0
    assert store.count_pending() == 0
    util.set_now(util.now().replace(year=util.now().year + 1))  # everything is due again
    checker.pages["https://ada.example"] = {"bio": "Joined Difference Labs", "organization": "Difference Labs"}
    second = _pass(store, config, checker)
    assert second["events"] == 2
    pending = store.pending_checks(10)
    assert len(pending) == 1 and {e["label"] for e in pending[0]["events"]} == {"bio changed", "organization changed"}


def test_gone_once_is_nothing_twice_retires(store: Any, config: Any) -> None:
    _person_id, source_id = _person(store)
    checker = FakeChecker({"https://ada.example": {"bio": "hello"}})
    _pass(store, config, checker)
    checker.pages["https://ada.example"] = None
    for _ in range(2):
        util.set_now(util.now().replace(year=util.now().year + 1))
        _pass(store, config, checker)
    source = store.source(source_id)
    assert source["status"] == "broken"
    assert [e["label"] for e in store.pending_checks(5)[0]["events"]] == ["account gone"]


def test_five_failures_move_to_weekly_retry(store: Any, config: Any) -> None:
    _person_id, source_id = _person(store)
    checker = FakeChecker({})
    for _ in range(5):
        util.set_now(util.now().replace(year=util.now().year + 1))
        _pass(store, config, checker)
    source = store.source(source_id)
    assert source["fail_streak"] == 5 and source["status"] == "active"
    assert source["status_note"].startswith("failing since")


def test_outage_stops_the_pass(store: Any, config: Any) -> None:
    from tailwatch.connectors.base import Read

    for i in range(20):
        pid, _ = store.add_person(f"Person {i}", links={"homepage": f"https://p{i}.example"})
        store.ensure_source(pid, "fake", f"https://p{i}.example", cadence_hours=24)
    checker = FakeChecker({f"https://p{i}.example": Read.failed("402", outage="out of credits") for i in range(20)})
    result = _pass(store, config, checker)
    assert result["checked"] == runner.OUTAGE_SAMPLE
    assert "out of credits" in result["outage"]


def _changed(store: Any, config: Any) -> int:
    person_id, _ = _person(store)
    checker = FakeChecker({"https://ada.example": {"bio": "Researcher", "organization": "Analytical Engines"}})
    _pass(store, config, checker)
    util.set_now(util.now().replace(year=util.now().year + 1))
    checker.pages["https://ada.example"] = {"bio": "I left Analytical Engines", "organization": "Difference Labs"}
    _pass(store, config, checker)
    return person_id


def test_judge_saves_one_verdict_per_check_and_the_digest_carries_it_once(store: Any, config: Any, model: Any) -> None:
    person_id = _changed(store, config)
    adapter = model(lambda request: verdict("left", now={"organization": "Difference Labs", "title": "Founder", "since": ""}))
    counts = judge.judge_pending(store, config, {}, checkers={"fake": FakeChecker()}, log=None)
    assert counts["judged"] == 1 and counts["signals"] == 1
    seed = json.loads(adapter.requests[0].messages[0]["content"])
    assert seed["change"]["changes"] and seed["person"]["name"] == "Ada Lovelace"
    assert adapter.requests[0].schema["properties"]["signal"]["enum"] == ["left", "looking", "none"]

    first = digest.post(store, config, {}, sinks=[])
    assert first["state"] == "sent" and first["people"] == 1
    assert digest.post(store, config, {}, sinks=[])["state"] == "already sent"
    assert store.posted_moves(person_id, since="0000")[0]["move_key"] == "left|difference labs"


def test_same_move_is_not_posted_twice(store: Any, config: Any, model: Any) -> None:
    person_id = _changed(store, config)
    model(lambda request: verdict("left", now={"organization": "Difference Labs", "title": "", "since": ""}))
    judge.judge_pending(store, config, {}, checkers={"fake": FakeChecker()}, log=None)
    digest.post(store, config, {}, sinks=[])
    # the same move arrives again from another check the next day
    obs = store.pending_checks(1)
    assert not obs
    source = store.sources_of(person_id)[0]
    observation = store.record_check(source, status="ok", content={"bio": "x"}, changes=[{"label": "bio changed"}])
    store.save_verdict(observation, person_id, verdict("left", now={"organization": "Difference Labs", "title": "", "since": ""}))
    util.set_now(util.now() + __import__("datetime").timedelta(days=1))
    built = digest.build(store, config, {}, rank=False)
    assert built["sections"] == [] and built["dropped"] == {"already posted": 1}


def test_gate_records_a_flag_alone_without_a_model_call(store: Any, config: Any, model: Any) -> None:
    person_id, source_id = _person(store)
    store.record_check(store.source(source_id), status="ok", content={}, changes=[{"label": "hireable flag changed"}])
    adapter = model(lambda request: (_ for _ in ()).throw(AssertionError("no model call expected")))
    counts = judge.judge_pending(store, config, {}, checkers={}, log=None)
    assert counts["gated"] == 1 and not adapter.requests
    assert store.verdicts_of(person_id)[0]["judged_by"] == "gate"


def test_bad_answers_park_the_check(store: Any, config: Any, model: Any) -> None:
    _person_id, source_id = _person(store)
    store.record_check(store.source(source_id), status="ok", content={}, changes=[{"label": "bio changed"}])
    model(lambda request: "not json")
    for _ in range(judge.MAX_ATTEMPTS):
        judge.judge_pending(store, config, {}, checkers={}, log=None)
    assert store.count_parked() == 1


def test_old_move_is_recorded_as_none(store: Any, config: Any, model: Any) -> None:
    person_id = _changed(store, config)
    model(lambda request: verdict("left", signal_date="2001-01"))
    judge.judge_pending(store, config, {}, checkers={"fake": FakeChecker()}, log=None)
    saved = store.verdicts_of(person_id)[0]
    assert saved["signal"] == "none" and "old_move" in saved["flags"]


def test_unknown_same_as_is_retried_then_dropped(store: Any, config: Any, model: Any) -> None:
    person_id = _changed(store, config)
    adapter = model(lambda request: verdict("left", same_as=999))
    judge.judge_pending(store, config, {}, checkers={"fake": FakeChecker()}, log=None)
    assert len(adapter.requests) == 2
    assert store.verdicts_of(person_id)[0]["same_as"] == 0


def test_store_on_disk_survives_reopen(tmp_path: Any, config: Any) -> None:
    url = f"sqlite:///{tmp_path / 'tw.db'}"
    first = open_store(url, config)
    first.add_person("Grace Hopper", links={"github": "https://github.com/grace"})
    second = open_store(url, config)
    assert second.find_people("grace")[0]["name"] == "Grace Hopper"
