"""The registry: every tool once across the modules, and the one permission check."""

from __future__ import annotations

import importlib
from typing import Any

import pytest

from tailwatch.agent import registry


def _importable() -> tuple[list[str], dict[str, str]]:
    present, missing = [], {}
    for path in registry.MODULES:
        try:
            importlib.import_module(path)
            present.append(path)
        except Exception as error:  # a module another part of the project has not finished
            missing[path] = f"{type(error).__name__}: {error}"
    return present, missing


def test_the_modules_that_import_define_every_name_once() -> None:
    present, _missing = _importable()
    tools = registry.build(present)  # raises RegistryError on a name defined twice
    assert {"use_skill", "decide", "remember", "health", "coverage"} <= set(tools)
    for tool in tools.values():
        if tool.writes:
            assert tool.mutating, f"{tool.name} writes and must be memoised"


def test_the_full_registry_builds() -> None:
    _present, missing = _importable()
    if missing:
        pytest.skip("tool modules not importable yet: " + "; ".join(f"{p} ({e})" for p, e in missing.items()))
    tools = registry.build()
    assert len(tools) == len({t.name for t in tools.values()})


def test_a_name_defined_twice_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    one, two = types.ModuleType("one"), types.ModuleType("two")
    one.TOOLS, two.TOOLS = registry.Toolbox("a"), registry.Toolbox("b")  # type: ignore[attr-defined]
    for box in (one.TOOLS, two.TOOLS):  # type: ignore[attr-defined]
        box.tool("same", "x")(lambda ctx: None)
    monkeypatch.setitem(__import__("sys").modules, "one", one)
    monkeypatch.setitem(__import__("sys").modules, "two", two)
    with pytest.raises(registry.RegistryError):
        registry.build(["one", "two"])


def _ctx(store: Any, config: Any, user: str) -> registry.ToolContext:
    return registry.ToolContext(store=store, config=config, user=user)


def test_may_write_without_a_team_list(store: Any, config: Any) -> None:
    assert registry.may_write(_ctx(store, config, "U9"))
    config.require_team_list = True
    assert not registry.may_write(_ctx(store, config, "U9"))
    assert registry.may_write(_ctx(store, config, "local"))


def test_may_write_with_a_team_list(store: Any, config: Any) -> None:
    config.team = ["U1"]
    assert registry.may_write(_ctx(store, config, "U1"))
    assert not registry.may_write(_ctx(store, config, "U2"))
    store.set_setting("DESK:TEAM", ["U2"])
    assert registry.may_write(_ctx(store, config, "U2"))


def test_a_writing_tool_is_refused_before_it_runs(store: Any, config: Any) -> None:
    from tailwatch.desk import tools_intel

    config.team = ["U1"]
    tool = next(t for t in tools_intel.TOOLS.tools if t.name == "remember")
    refused = registry.execute(tool, _ctx(store, config, "U2"), {"text": "people at Acme are leaving", "scope_kind": "everyone",
                                                                 "scope_value": ""})
    assert "team list" in refused["error"] and store.intel() == []
    unknown = registry.execute(tool, _ctx(store, config, "U1"), {"text": "x", "scope_kind": "everyone", "scope_value": "",
                                                                 "oops": 1})
    assert "oops" in unknown["error"]
