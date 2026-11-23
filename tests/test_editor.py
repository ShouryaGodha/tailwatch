"""The editor: a bounded, read-only tool loop whose answer the floors lower."""

from __future__ import annotations

from typing import Any

from tailwatch.agent import loop
from tailwatch.agent.providers.scripted import turn
from tailwatch.desk import editor
from test_verify_support import QUOTE, answer, card, factory, seed_of


def _review(store: Any, made: dict[str, Any]) -> dict[str, Any]:
    return store.open_review(store.verdict(made["verdict_id"]))


def _toolkit(pages: dict[str, str] | None = None, cost: float = 0.0) -> Any:
    toolkits: list[Any] = []
    return factory(pages, toolkits, cost)(None, None, {}, budget=loop.Budget(0.15)), toolkits


def test_edit_reads_and_quotes_then_confirms(store: Any, config: Any, model: Any) -> None:
    made = card(store)
    page = "https://news.example/ada"
    toolkit, _ = _toolkit({page: f"Ada Lovelace: '{QUOTE} to start Difference Labs.'"})
    steps = iter([turn("", ("latest_read", {})), turn("", ("web_read", {"url": page})), answer(url=page)])
    adapter = model(lambda request: next(steps))
    result = editor.edit(store, config, {}, _review(store, made), toolkit=toolkit)
    assert result["verdict"] == "confirm" and result["failed"] == []
    assert [t["tool"] for t in result["transcript"]] == ["latest_read", "web_read"]
    tool_names = {s["name"] for s in adapter.requests[0].tools}
    assert {"latest_read", "judge_input", "prior_cards", "web_read"} <= tool_names
    assert "linkedin_profile" not in tool_names  # the connector is off in the test config
    assert seed_of(adapter.requests[0])["name"] == "Ada Lovelace"
    evidence = [m for m in adapter.requests[1].messages if m.get("role") == "tool"]
    assert evidence[0]["content"].startswith("<evidence")
    assert store.runs(routine="verify_card")[0]["state"] == "done"


def test_a_tool_outside_the_whitelist_is_refused(store: Any, config: Any, model: Any) -> None:
    made = card(store)
    toolkit, _ = _toolkit()
    steps = iter([turn("", ("set_status", {"person_id": 1})), turn("", ("linkedin_profile", {"url": "x"})), answer()])
    model(lambda request: next(steps))
    result = editor.edit(store, config, {}, _review(store, made), toolkit=toolkit)
    assert toolkit.calls == []
    assert result["verdict"] == "confirm"  # the quote is on the stored latest read


def test_the_floors_lower_a_paraphrased_confirm(store: Any, config: Any, model: Any) -> None:
    made = card(store)
    toolkit, _ = _toolkit()
    model(lambda request: answer(quote="Ada told everyone she quit"))
    result = editor.edit(store, config, {}, _review(store, made), toolkit=toolkit)
    assert result["editor_verdict"] == "confirm" and result["verdict"] == "hold"
    assert result["failed"] == ["label_supported"]


def test_linkedin_pull_only_for_a_first_signal_card_with_an_old_read(store: Any, config: Any, model: Any) -> None:
    config.connectors["linkedin"] = config.connector("fake").model_copy(update={"enabled": True})
    left, looking = card(store), card(store, "Grace Hopper", signal="looking")
    assert editor.card_for(store, config, _review(store, left))["allow_linkedin"]
    assert not editor.card_for(store, config, _review(store, looking))["allow_linkedin"]
    person = store.person(left["person_id"])
    source = store.source(store.ensure_source(person["id"], "linkedin", "https://www.linkedin.com/in/ada", cadence_hours=24))
    store.record_check(source, status="ok", content={"positions": []})
    assert not editor.card_for(store, config, _review(store, left))["allow_linkedin"]  # read within the day


def test_a_fix_with_a_correction_no_record_carries_goes_to_the_custodian(store: Any, config: Any, model: Any) -> None:
    made = card(store)
    toolkit, _ = _toolkit()
    model(lambda request: answer("confirm", fields={"organization": "", "title": "Principal Researcher"}))
    result = editor.edit(store, config, {}, _review(store, made), toolkit=toolkit)
    assert result["verdict"] == "fix"
    assert [i["playbook"] for i in result["issues"]] == ["fix_card_fields"]


def test_the_per_card_budget_stops_the_loop(store: Any, config: Any, model: Any) -> None:
    made = card(store)
    toolkit, _ = _toolkit({"https://a.example": "nothing"}, cost=0.2)
    calls = iter([turn("", ("web_read", {"url": "https://a.example"}))])
    adapter = model(lambda request: next(calls, answer("hold")))
    result = editor.edit(store, config, {}, _review(store, made), toolkit=toolkit)
    assert result["stopped"] == "BUDGET" and toolkit.calls == ["web_read"]
    assert "STOP" in adapter.requests[-1].messages[-1]["content"]


def test_the_prompt_names_the_pack_and_the_learned_rules(store: Any, config: Any) -> None:
    store.set_setting(editor.RULES_KEY, ["A conference badge is not a new employer."])
    text = editor.instructions(store, config)
    assert "left" in text and "conference badge" in text and "{{" not in text
