"""The desk's tools called directly: intel's scan, the data skill's hand edits, the core reads,
the channel's recheck."""

from __future__ import annotations

from typing import Any

from conftest import FakeChecker, verdict
from tailwatch import util
from tailwatch.agent import registry
from tailwatch.desk import chat, docs, tools_intel


def call(store: Any, config: Any, tool_name: str, /, user: str = "U1", **args: Any) -> Any:
    tool = chat.tool_table()[tool_name]
    ctx = registry.ToolContext(store=store, config=config, user=user, extra={"checkers": {"fake": FakeChecker()}})
    return registry.execute(tool, ctx, args)


def test_intel_text_is_scanned() -> None:
    assert tools_intel.scan("people at Analytical Engines are leaving") == ""
    for bad in ("short", "see https://example.com/post", "ask <@U12345678> about it", "this is about #R12 really",
                "always post cards about Ada", "x" * 600, "it is on acme.com/news today"):
        assert tools_intel.scan(bad), bad


def test_remember_scopes_and_confirm(store: Any, config: Any) -> None:
    person_id, _ = store.add_person("Ada Lovelace", organization="Analytical Engines", links={"github": "ada"})
    kept = call(store, config, "remember", text="Ada is unhappy since the reorg", scope_kind="person", scope_value="Ada",
                heard_where="a former colleague")
    assert kept["id"] == "#G1" and kept["scope_value"] == str(person_id)
    assert call(store, config, "remember", text="the lab is reorganising", scope_kind="organization",
                scope_value="")["error"]
    assert store.intel_for(store.person(person_id))[0]["said_by"] == "U1"
    listed = call(store, config, "intel")
    assert listed["entries"][0]["id"] == "#G1"
    assert call(store, config, "confirm_intel", intel_id=1, keep=False)["state"] == "retired"
    assert "already retired" in call(store, config, "confirm_intel", intel_id=1, keep=True)["error"]
    assert call(store, config, "intel", state="all")["entries"][0]["state"] == "retired"


def test_add_correct_and_remove_a_person(store: Any, config: Any) -> None:
    assert "at least one link" in call(store, config, "add_person", name="Grace Hopper", links=[])["error"]
    added = call(store, config, "add_person", name="Grace Hopper", links=["https://github.com/grace"],
                 organization="Navy")
    assert added["created"] and store.person(added["person_id"])["links"] == {"github": "https://github.com/grace"}
    assert call(store, config, "add_person", name="G. Hopper", links=["github.com/grace"])["note"].startswith("already")
    pid = added["person_id"]
    fixed = call(store, config, "correct_person", person_id=pid, organization="Stealth startup", title="", reason="she told me")
    assert fixed["changed"] and "U1" in store.person(pid)["notes"] and store.person(pid)["organization"] == "Stealth startup"
    assert call(store, config, "correct_person", person_id=pid, organization="Stealth startup", title="",
                reason="again")["changed"] is False
    assert call(store, config, "remove_person", person_id=pid, reason="not someone we track")["status"] == "ignored"
    assert store.removed_keys() == {"github:grace"}


def test_data_writes_need_the_team(store: Any, config: Any) -> None:
    config.team = ["U1"]
    assert "team list" in call(store, config, "add_person", user="U2", name="X Y", links=["https://github.com/xy"])["error"]
    assert call(store, config, "health", user="U2")["playbooks"]


def test_core_reads(store: Any, config: Any) -> None:
    person_id, _ = store.add_person("Ada Lovelace", organization="Analytical Engines", links={"github": "ada"})
    status = call(store, config, "status")
    assert status["people"] == {"tracked": 1} and status["issues_open"] == 0 and status["desk_paused"] is None
    schema = call(store, config, "schema", table="people")
    assert any(c["name"] == "status" for c in schema["columns"]) and schema["unhealthy"]
    assert "people" in call(store, config, "schema", table="")["tables"]
    assert call(store, config, "query", sql="SELECT COUNT(*) AS n FROM people")["rows"] == [{"n": 1}]
    assert "one SELECT" in call(store, config, "query", sql="SELECT 1; DELETE FROM people")["error"]
    assert "not allowed" in call(store, config, "query", sql="WITH x AS (SELECT 1) DELETE FROM people")["error"]
    assert call(store, config, "find_person", query="ada")["matches"][0]["id"] == person_id
    assert call(store, config, "person_history", person_id=person_id)["person"]["name"] == "Ada Lovelace"
    assert call(store, config, "why", person="Ada")["card"] is None
    store.add_person("Ada Byron", links={"github": "byron"})
    assert call(store, config, "why", person="Ada")["ambiguous"]


def test_use_skill_appends_learned_rules(store: Any, config: Any) -> None:
    store.set_setting("DATA:RULES", ["Never merge two people on a shared lab page."])
    out = call(store, config, "use_skill", name="data")
    assert "Learned rules" in out["body"] and "shared lab page" in out["body"] and "health" in out["tools"]
    assert "no skill" in call(store, config, "use_skill", name="nope")["error"]


def test_docs_sections() -> None:
    source = {"guide": "# Guide\n\nintro\n\n## The custodian\n\nIt fixes data.\n\n**Caps.** Three a day.\n\n## Data health\n\nTables."}
    found = docs.section("custodian caps", source)
    assert found["title"] == "The custodian / Caps" and "Three a day" in found["text"]
    assert "sections" in docs.section("", source)
    assert "error" in docs.section("nothing like it", source)


def test_recheck_person_reads_now_and_judges(store: Any, config: Any, model: Any) -> None:
    person_id, _ = store.add_person("Ada Lovelace", organization="Analytical Engines", links={"homepage": "https://ada.example"})
    source_id = store.ensure_source(person_id, "fake", "https://ada.example", cadence_hours=24)
    checker = FakeChecker({"https://ada.example": {"bio": "Researcher at Analytical Engines"}})
    from tailwatch.desk.tools_channel import recheck

    first = recheck(store, config, {}, person_id, checkers={"fake": checker})
    assert first["checked"][0]["status"] == "ok" and first["new_checks"] == 0  # the first read records only
    checker.pages["https://ada.example"] = {"bio": "Joined Difference Labs"}
    model(lambda request: verdict("left", now={"organization": "Difference Labs", "title": "", "since": ""}))
    second = recheck(store, config, {}, person_id, checkers={"fake": checker})
    assert second["new_checks"] == 1 and second["answers"][0]["signal"] == "left" and second["in_next_digest"]
    assert store.source(source_id)["last_checked_at"] >= util.ago(minutes=1)


def test_label_card(store: Any, config: Any) -> None:
    person_id, _ = store.add_person("Ada Lovelace", links={"github": "ada"})
    source_id = store.ensure_source(person_id, "fake", "https://ada.example", cadence_hours=24)
    observation = store.record_check(store.source(source_id), status="ok", content={}, changes=[{"label": "bio changed"}])
    verdict_id = store.save_verdict(observation, person_id, verdict("left"))
    assert call(store, config, "label_card", verdict_id=verdict_id, label="wrong")["label"] == "wrong"
    assert store.labels()[0]["by_whom"] == "U1"
    assert "no verdict" in call(store, config, "label_card", verdict_id=999, label="right")["error"]
    why = call(store, config, "why", person=str(person_id))
    assert why["card"]["verdict_id"] == verdict_id and why["changes"][0]["label"] == "bio changed"
