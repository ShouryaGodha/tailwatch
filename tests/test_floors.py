"""The floors: each one lowers a confirm, none raises a verdict."""

from __future__ import annotations

from typing import Any

from tailwatch.desk import floors


def _card(**overrides: Any) -> dict[str, Any]:
    base = {"person_id": 1, "verdict_id": 10, "observation_id": 5, "kind": "linkedin", "name": "Ada Lovelace",
            "organization": "Analytical Engines", "title": "Researcher", "signal": "left", "first_signal": "left",
            "confidence": 0.9, "source_status": "active", "holders": {}, "profile_name": "", "posted": [],
            "move_key": "left|difference labs", "same_as": 0, "supersedes": [], "events": [],
            "latest": {"id": 5, "content": {"x": 1}, "text": "", "positions": []}, "previous": {}, "judged_content": {"x": 1}}
    return {**base, **overrides}


def _answer(verdict: str = "confirm", **overrides: Any) -> dict[str, Any]:
    base = {"verdict": verdict, "confidence": 0.9, "quotes": [{"url": "u", "text": "I left Analytical Engines last week"}],
            "fields": {"organization": "", "title": ""}, "now_at": None, "issues": [], "drop_reason": "", "note": ""}
    return {**base, **overrides}


TEXTS = {"u": "Hello. I left Analytical Engines last week, after six years."}


def _final(card: dict[str, Any], answer: dict[str, Any], floor: float = 0.6) -> dict[str, Any]:
    results = floors.run_all(card, answer, texts=TEXTS, floor=floor)
    return floors.lower(answer, results, drop_allowed=floors.allow_drop(card, answer, results))


def test_every_floor_passes_and_a_confirm_stands() -> None:
    lowered = _final(_card(), _answer())
    assert lowered["verdict"] == "confirm" and lowered["failed"] == []


def test_identity_wrong_person_source_turns_confirm_into_fix() -> None:
    lowered = _final(_card(source_status="wrong_person"), _answer())
    assert lowered["failed"] == ["identity"] and lowered["verdict"] == "fix"


def test_identity_link_held_by_another_person() -> None:
    results = floors.run_all(_card(holders={"https://x.example": 7}), _answer(), texts=TEXTS, floor=0.6)
    assert results["identity"]["result"] == "fail" and results["identity"]["issue"] == "shared_link"


def test_identity_profile_name_of_someone_else() -> None:
    assert floors.identity(_card(profile_name="Charles Babbage"))["result"] == "fail"
    assert floors.identity(_card(profile_name="A. Lovelace"))["result"] == "pass"
    assert floors.identity(_card(profile_name="Ada King"))["result"] == "pass"


def test_change_reverted_on_the_latest_read_lowers_to_fix() -> None:
    card = _card(latest={"id": 9, "content": {"x": 0}, "text": "", "positions": []}, previous={"id": 3, "content": {"x": 0}})
    lowered = _final(card, _answer())
    assert lowered["failed"] == ["event_present"] and lowered["verdict"] == "fix"


def test_a_report_is_not_a_profile_for_the_change_floor() -> None:
    assert floors.event_present(_card(kind="feed:news"))["result"] == "skip"


def test_a_paraphrase_is_no_support_and_holds() -> None:
    lowered = _final(_card(), _answer(quotes=[{"url": "u", "text": "Ada said she quit Analytical Engines"}]))
    assert lowered["failed"] == ["label_supported"] and lowered["verdict"] == "hold"


def test_positions_support_the_first_signal_without_a_quote() -> None:
    ended = {"id": 5, "content": {}, "positions": [{"company": "Analytical Engines Inc.", "current": False, "end": "2026-10"}]}
    assert _final(_card(latest=ended), _answer(quotes=[]))["verdict"] == "confirm"


def test_a_removed_role_entry_supports_a_departure() -> None:
    latest = {"id": 5, "content": {}, "positions": [{"company": "Elsewhere", "current": True}]}
    previous = {"id": 3, "content": {}, "positions": [{"company": "Analytical Engines", "current": True}]}
    assert floors.label_supported(_card(latest=latest, previous=previous), _answer(quotes=[]), texts={})["result"] == "pass"


def test_positions_do_not_support_another_signal() -> None:
    ended = {"id": 5, "content": {}, "positions": [{"company": "Analytical Engines", "current": False}]}
    assert floors.label_supported(_card(signal="looking", latest=ended), _answer(quotes=[]), texts={})["result"] == "fail"


def test_a_duplicate_holds_unless_it_supersedes_the_posted_card() -> None:
    posted = [{"verdict_id": 3, "move_key": "left|difference labs", "at": "2026-11-01T00:00:00Z"}]
    assert _final(_card(posted=posted), _answer())["verdict"] == "hold"
    assert _final(_card(posted=posted, supersedes=[3]), _answer())["verdict"] == "confirm"
    assert _final(_card(posted=[{**posted[0], "move_key": "looking|"}], same_as=3), _answer())["verdict"] == "hold"
    assert _final(_card(posted=[{**posted[0], "move_key": "looking|"}]), _answer())["verdict"] == "confirm"


def test_low_confidence_holds() -> None:
    assert _final(_card(confidence=0.4), _answer())["verdict"] == "hold"
    assert _final(_card(), _answer(confidence=0.5))["failed"] == ["confidence"]


def test_floors_never_raise_a_verdict() -> None:
    assert _final(_card(), _answer("hold"))["verdict"] == "hold"
    assert _final(_card(), _answer("fix"))["verdict"] == "hold"  # nothing for a playbook to repair
    fix = _answer("fix", issues=[{"kind": "missing_link", "detail": "no GitHub"}])
    assert _final(_card(), fix)["verdict"] == "fix"


def test_allow_drop() -> None:
    card, gone = _card(), _answer("drop", drop_reason="change_gone")
    results = floors.run_all(card, gone, texts=TEXTS, floor=0.6)
    assert not floors.allow_drop(card, gone, results)
    assert _final(card, gone)["verdict"] == "hold"
    looking = _card(signal="looking")
    flag = _answer("drop", drop_reason="flag_only")
    assert floors.allow_drop(looking, flag, floors.run_all(looking, flag, texts=TEXTS, floor=0.6))
    duplicate = _answer("drop", drop_reason="duplicate")
    posted = _card(posted=[{"verdict_id": 3, "move_key": "left|difference labs", "at": "2026-11-01"}])
    assert _final(posted, duplicate)["verdict"] == "drop"
    assert _final(_card(), duplicate)["verdict"] == "hold"  # the floor did not confirm the duplicate


def test_fetch_free_floors_skip_the_profile_name() -> None:
    results = floors.run_fetch_free(_card(profile_name="Charles Babbage"), None, floor=0.6)
    assert set(results) == set(floors.FETCH_FREE) and not floors.failed(results)


def test_landing_needs_a_newer_current_role_elsewhere() -> None:
    positions = [{"company": "Analytical Engines", "title": "Researcher", "current": False, "start": "2020-01", "end": "2026-10"},
                 {"company": "Difference Labs", "title": "Founder", "current": True, "start": "2026-11"},
                 {"company": "Royal Society", "title": "Fellow", "current": True, "start": "2019-01"}]
    card = _card(latest={"id": 5, "content": {}, "positions": positions})
    assert floors.landing(card, {"organization": "Difference Labs", "title": "", "since": ""}) == \
        {"organization": "Difference Labs", "title": "Founder", "since": "2026-11"}
    assert floors.landing(card, {"organization": "Royal Society", "title": "", "since": ""}) is None  # held alongside
    assert floors.landing(card, {"organization": "Analytical Engines", "title": "", "since": ""}) is None
    assert floors.landing(card, None) is None


def test_structured_fix_only_from_a_position_record() -> None:
    positions = [{"company": "Analytical Engines", "title": "Principal Researcher", "current": False}]
    card = _card(latest={"id": 5, "content": {}, "positions": positions})
    assert floors.structured_fix(card, {"organization": "", "title": "Principal Researcher"}) == \
        {"organization": "Analytical Engines", "title": "Principal Researcher"}
    assert floors.structured_fix(card, {"organization": "", "title": "Chief Scientist"}) is None


def test_confidence_floor_from_settings(store: Any) -> None:
    assert floors.confidence_floor(store) == floors.DEFAULT_CONFIDENCE
    store.set_setting(floors.SETTING, {"confidence": 0.8})
    assert floors.confidence_floor(store) == 0.8
