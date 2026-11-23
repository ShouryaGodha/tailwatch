"""The chat entry point with a scripted model calling real tools: skills loading, the permission
check, resuming a message by its key, silence, and the channel mode."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from tailwatch.agent.providers.scripted import turn
from tailwatch.agent.types import ModelError
from tailwatch.desk import chat

NOTE = "people at Analytical Engines are leaving"


def scripted(model: Callable[..., Any], *turns: Any) -> Any:
    queue = list(turns)

    def answer(_request: Any) -> Any:
        if not queue:
            raise AssertionError("the model was called more often than scripted")
        return queue.pop(0)

    return model(answer)


def names(request: Any) -> set[str]:
    return {t["name"] for t in request.tools}


def tool_results(request: Any) -> list[str]:
    return [m["content"] for m in request.messages if m.get("role") == "tool"]


def ask(store: Any, config: Any, text: str, **kw: Any) -> dict[str, Any]:
    kw.setdefault("user", "U1")
    kw.setdefault("channel", "cli")
    return chat.respond(store, config, {}, text=text, **kw)


def test_use_skill_loads_its_tools_on_the_next_step(store: Any, config: Any, model: Any) -> None:
    adapter = scripted(model, turn("", ("use_skill", {"name": "data"})), turn("", ("health", {})), turn("All healthy."))
    out = ask(store, config, "is the data healthy?")
    first, second = adapter.requests[0], adapter.requests[1]
    assert "use_skill" in names(first) and "remember" in names(first)  # intel is a default skill
    assert "health" not in names(first) and "health" in names(second)
    assert '"skill": "data"' in tool_results(second)[0]
    assert out["state"] == "done" and out["answer"] == "All healthy." and "data" in out["skills"]
    assert "Skills" in first.system and "- data:" in first.system
    row = store.run(out["run_id"])
    assert row["routine"] == "desk" and row["state"] == "done"


def test_a_tool_of_an_unloaded_skill_is_not_run(store: Any, config: Any, model: Any) -> None:
    adapter = scripted(model, turn("", ("remove_person", {"person_id": 1, "reason": "x"})), turn("Not here."))
    ask(store, config, "remove person 1")
    assert "use_skill first" in tool_results(adapter.requests[1])[0]


def test_a_writing_tool_is_refused_for_someone_not_on_the_team(store: Any, config: Any, model: Any) -> None:
    config.team = ["U1"]
    adapter = scripted(model, turn("", ("remember", {"text": NOTE, "scope_kind": "everyone", "scope_value": ""})),
                       turn("Only a person on the team list can keep intel."))
    out = ask(store, config, f"FYI {NOTE}", user="U2")
    assert "team list" in tool_results(adapter.requests[1])[0]
    assert store.intel() == [] and out["state"] == "done"
    assert '"user_may_decide": false' in adapter.requests[0].messages[0]["content"]


def test_a_retried_message_resumes_and_never_writes_twice(store: Any, config: Any, model: Any) -> None:
    call = ("remember", {"text": NOTE, "scope_kind": "everyone", "scope_value": ""})
    scripted(model, turn("", call), ModelError("bad request", status=400))
    first = ask(store, config, f"FYI {NOTE}", message_key="slack:C1:1.0")
    assert first["state"] == "failed" and len(store.intel()) == 1
    assert store.run_by_key("slack:C1:1.0")["checkpoint"]["memo"]

    adapter = scripted(model, turn("", call), turn("Kept as #G1."))
    again = ask(store, config, f"FYI {NOTE}", message_key="slack:C1:1.0")
    assert again["state"] == "done" and again["run_id"] == first["run_id"]
    assert len(store.intel()) == 1  # the memo answered the second call
    assert any("#G1" in m for m in tool_results(adapter.requests[0]))  # resumed with the earlier result
    lines = store.thread_lines(chat.thread_key("cli", "", "U1"))
    assert [line["who"] for line in lines] == ["U1", "bot"]  # the message was remembered once

    duplicate = ask(store, config, f"FYI {NOTE}", message_key="slack:C1:1.0")
    assert duplicate["duplicate"] and duplicate["answer"] == "Kept as #G1." and len(adapter.requests) == 2


def test_no_reply_is_silent(store: Any, config: Any, model: Any) -> None:
    scripted(model, turn("NO_REPLY"))
    out = ask(store, config, "thanks all!", message_key="k1")
    assert out["state"] == "silent" and out["answer"] == ""
    assert store.run(out["run_id"])["state"] == "silent"
    assert [line["who"] for line in store.thread_lines(chat.thread_key("cli", "", "U1"))] == ["U1"]


def test_the_digest_channel_offers_the_channel_tools(store: Any, config: Any, model: Any) -> None:
    adapter = scripted(model, turn("Hello."))
    out = ask(store, config, "why Ada?", channel="C-DIGEST", thread="1.0")
    expected = {"coverage", "recent_alerts", "recheck_person", "propose_status", "apply_status", "label_card",
                "find_person", "person_history", "why", "status"}
    assert names(adapter.requests[0]) == expected
    assert store.run(out["run_id"])["routine"] == "channel"


def test_the_desk_channel_is_the_desk(store: Any, config: Any, model: Any) -> None:
    config.delivery.slack.desk_channel = "C-DESK"
    adapter = scripted(model, turn("Hi."))
    ask(store, config, "status?", channel="C-DESK", thread="2.0")
    assert "use_skill" in names(adapter.requests[0])


def test_a_status_change_takes_two_messages(store: Any, config: Any, model: Any) -> None:
    person_id, _ = store.add_person("Ada Lovelace", organization="Analytical Engines", links={"github": "ada"})
    thread = {"channel": "C-DIGEST", "thread": "9.0"}
    propose = ("propose_status", {"person": "Ada Lovelace", "status": "left", "reason": "she joined Difference Labs"})
    too_soon = ("apply_status", {})
    adapter = scripted(model, turn("", propose), turn("", too_soon), turn("Mark Ada as left? Reply yes."))
    ask(store, config, "Ada left, stop tracking her as tracked", message_key="m1", **thread)
    assert "not answered" in tool_results(adapter.requests[2])[-1]
    assert store.person(person_id)["status"] == "tracked"

    adapter = scripted(model, turn("", ("apply_status", {"person": "Ada"})), turn("Done: Ada is left."))
    ask(store, config, "yes", message_key="m2", **thread)
    assert '"pending"' in adapter.requests[0].messages[0]["content"]
    assert store.person(person_id)["status"] == "left"
    assert '"applied": true' in tool_results(adapter.requests[1])[0]

    adapter = scripted(model, turn("", ("apply_status", {})), turn("Nothing pending."))
    ask(store, config, "yes", message_key="m3", **thread)
    assert "no pending" in tool_results(adapter.requests[1])[0]


def test_a_bare_number_means_the_root_posts_kind(store: Any, config: Any, model: Any) -> None:
    config.delivery.slack.desk_channel = "C-DESK"
    issue_id, _ = store.open_issue("broken_source", "source:1", found_by="test")
    chat.record_post(store, "C-DESK", "5.0", f"Needs you: #I{issue_id} a broken page")
    chat.record_post(store, "C-DESK", "6.0", f"Held: #R{issue_id} Ada")
    call = ("decide", {"kind": "I", "id": issue_id, "action": "close", "note": "fine"})
    adapter = scripted(model, turn("", call), turn("That number is a card here; which did you mean?"))
    ask(store, config, f"close {issue_id}", channel="C-DESK", thread="6.0")
    assert f'"R": [{issue_id}]' in adapter.requests[0].messages[0]["content"]
    assert "means #R" in tool_results(adapter.requests[1])[0] and store.issue(issue_id)["state"] == "open"

    scripted(model, turn("", call), turn("Closed."))
    ask(store, config, f"close {issue_id}", channel="C-DESK", thread="5.0")
    assert store.issue(issue_id)["state"] == "wontfix"


@pytest.mark.parametrize("action,state", [("close", "wontfix")])
def test_decide_on_an_issue(store: Any, config: Any, model: Any, action: str, state: str) -> None:
    issue_id, _ = store.open_issue("broken_source", "source:1", found_by="test")
    scripted(model, turn("", ("decide", {"kind": "I", "id": issue_id, "action": action})), turn("Done."),
             turn("", ("decide", {"kind": "I", "id": issue_id, "action": "retry"})), turn("Open again."))
    ask(store, config, f"{action} I{issue_id}")
    assert store.issue(issue_id)["state"] == state
    ask(store, config, f"retry I{issue_id}")
    assert store.issue(issue_id)["state"] == "open" and store.issue(issue_id)["attempts"] == 0


def test_the_budget_stops_a_message(store: Any, config: Any, model: Any) -> None:
    config.budgets.desk_per_message = 0.000001
    adapter = scripted(model, turn("", ("status", {}), tokens=(100000, 100)), turn("Stopped, here is what I have."))
    config.pricing = {"scripted:test": (10.0, 1.0, 10.0)}
    out = ask(store, config, "status?")
    assert out["answer"] and out["stopped"] == "BUDGET" and len(adapter.requests) == 2
