"""The desk's enrichment skill: its tools, the permission check on the writing ones, and the
SKILL.md frontmatter matching the toolbox."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tailwatch.agent import registry, skills
from tailwatch.agent.registry import ToolContext
from tailwatch.enrich import tools_desk

SKILL = Path(__file__).resolve().parent.parent / "skills" / "enrichment" / "SKILL.md"


def tools() -> dict[str, registry.Tool]:
    return registry.build(("tailwatch.enrich.tools_desk",))


def call(store: Any, config: Any, user: str, name: str, **args: Any) -> Any:
    return registry.execute(tools()[name], ToolContext(store=store, config=config, user=user), args)


def test_skill_frontmatter_matches_the_toolbox() -> None:
    skill = skills.parse(SKILL)
    built = tools()
    assert set(skill.tools) == {t.name for t in tools_desk.TOOLS.tools} == set(built)
    assert set(skill.writes) == {name for name, tool in built.items() if tool.writes}
    assert skill.rules_key == "ENRICHMENT:RULES" and skill.default and skill.requires == ()
    assert built["mention"].evidence and not built["mentions"].evidence


def test_writing_tools_need_a_person_on_the_team_list(store: Any, config: Any) -> None:
    candidate_id, _ = store.add_candidate("news", "1", text="Ada Lovelace joined Difference Labs",
                                          subject={"identified": {"name": "Ada Lovelace"}}, state="needs_review")
    config.team = ["U1"]
    refused = call(store, config, "U2", "dismiss_mention", candidate_id=candidate_id, reason="wrong person")
    assert "team list" in refused["error"] and store.candidate(candidate_id)["state"] == "needs_review"
    assert "team list" in call(store, config, "U2", "settings_change", key="floors", value='{"confidence_min": 0.9}',
                               note="x")["error"]
    listed = call(store, config, "U2", "mentions")
    assert listed["candidates"][0]["id"] == f"#C{candidate_id}"  # anyone may ask
    done = call(store, config, "U1", "dismiss_mention", candidate_id=f"#C{candidate_id}", reason="wrong person")
    assert done["state"] == "dismissed"


def test_settings_tools(store: Any, config: Any) -> None:
    changed = call(store, config, "local", "settings_change", key="floors", value='{"confidence_min": 0.85}', note="stricter")
    assert changed["value"]["confidence_min"] == 0.85 and changed["version"] == 1
    refused = call(store, config, "local", "settings_change", key="floors", value='{"confidence_min": 0.1}', note="")
    assert "between" in refused["error"]
    rule = call(store, config, "local", "settings_change", key="rules", value="A headline naming the organization counts",
                note="the team said so")
    assert rule["value"] == ["A headline naming the organization counts"]
    shown = call(store, config, "local", "settings_show")
    assert shown["settings"]["floors"]["value"]["confidence_min"] == 0.85
    reverted = call(store, config, "local", "settings_revert", key="floors", version=1)
    assert reverted["value"]["confidence_min"] == 0.8


def test_mention_shows_the_verdicts(store: Any, config: Any) -> None:
    candidate_id, _ = store.add_candidate("news", "1", text="Ada Lovelace joined Difference Labs", state="needs_review",
                                          subject={"identified": {"name": "Ada Lovelace", "joined": "Difference Labs"}})
    store.update_candidate(candidate_id, enrichment={"links": [{"kind": "github", "url": "https://github.com/adal",
                                                                "verdict": "unverified", "notes": ["quote missing"]}],
                                                     "outcome": "needs_review", "reason": "github not proven", "usd": 0.04})
    shown = call(store, config, "U9", "mention", candidate_id=candidate_id)
    assert shown["links"][0]["notes"] == ["quote missing"] and shown["usd"] == 0.04 and shown["joined"] == "Difference Labs"
    assert "not a candidate id" in call(store, config, "U9", "mention", candidate_id="abc")["error"]
