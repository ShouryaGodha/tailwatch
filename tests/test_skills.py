"""Every skill folder parses, uses only Agent Skills keys, and lists exactly its module's tools."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tailwatch.agent import skills
from tailwatch.desk import chat

SKILLS = sorted((Path(__file__).resolve().parent.parent / "skills").glob("*/SKILL.md"))
TOP_KEYS = {"name", "description", "allowed-tools", "metadata"}
META_KEYS = {"writes", "requires", "rules_key", "default", "version"}


@pytest.mark.parametrize("path", SKILLS, ids=[p.parent.name for p in SKILLS])
def test_skill_frontmatter(path: Path) -> None:
    skill = skills.parse(path)
    front = yaml.safe_load(path.read_text(encoding="utf-8").split("---", 2)[1])
    assert set(front) <= TOP_KEYS, f"{path.parent.name}: keys outside the spec: {set(front) - TOP_KEYS}"
    assert skill.description and skill.body and skill.tools
    meta = front.get("metadata") or {}
    assert set(meta) <= META_KEYS and all(isinstance(v, str) for v in meta.values()), meta
    assert meta.get("default") in ("true", "false") and meta.get("version")
    assert set(skill.writes) <= set(skill.tools)


@pytest.mark.parametrize("path", SKILLS, ids=[p.parent.name for p in SKILLS])
def test_allowed_tools_equal_the_registry(path: Path) -> None:
    skill = skills.parse(path)
    table = chat.tool_table()
    owned = {t.name for t in table.values() if t.owner == skill.name}
    if not owned:
        pytest.skip(f"no importable tool module owns the {skill.name} skill yet: {chat.missing_modules()}")
    assert set(skill.tools) == owned


@pytest.mark.parametrize("name", ["intel", "data"])
def test_writes_equal_the_writing_tools(name: str) -> None:
    skill = skills.discover()[name]
    writing = {t.name for t in chat.tool_table().values() if t.owner == name and t.writes}
    assert set(skill.writes) == writing


def test_mine_are_there() -> None:
    names = {p.parent.name for p in SKILLS}
    assert {"intel", "data"} <= names
    found = skills.discover()
    assert found["intel"].default and not found["data"].default
    assert found["intel"].rules_key == "INTEL:RULES" and found["data"].rules_key == "DATA:RULES"
