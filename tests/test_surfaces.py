"""The surfaces: the CLI's commands load, init writes a working project, the MCP server lists
and runs its tools, the Slack router answers only what it should, the scheduler keeps receipts."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tailwatch import config as config_module
from tailwatch import scheduler, util
from tailwatch.cli import app
from tailwatch.slack_app import should_answer

runner = CliRunner()


def test_init_writes_a_project_that_doctor_accepts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = runner.invoke(app, ["init", "--preset", "ai-researchers", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "tailwatch.yaml").exists() and (tmp_path / ".env").exists() and (tmp_path / "watchlist.csv").exists()
    monkeypatch.chdir(tmp_path)
    loaded = config_module.load(tmp_path / "tailwatch.yaml")
    assert loaded.signals == "career-moves" and loaded.priority.tiers[0].orgs[0] == "OpenAI"
    doctor = runner.invoke(app, ["doctor", "--config", str(tmp_path / "tailwatch.yaml")])
    assert doctor.exit_code == 0, doctor.output
    assert "career-moves" in doctor.output


@pytest.mark.parametrize("preset", ["ai-researchers", "oss-maintainers", "founders"])
def test_every_preset_is_a_valid_config(preset: str, tmp_path: Path) -> None:
    from tailwatch.agent import prompts
    from tailwatch.pipeline import packs

    path = tmp_path / "tailwatch.yaml"
    path.write_text(prompts.path_of("presets", f"{preset}.yaml").read_text())
    loaded = config_module.load(path)
    assert packs.load(loaded.signals).signals


def test_add_and_people(tmp_path: Path) -> None:
    project = tmp_path / "tailwatch.yaml"
    project.write_text("name: t\ndatabase: sqlite:///t.db\n")
    added = runner.invoke(app, ["add", "https://github.com/ada", "@ada", "--name", "Ada Lovelace", "--config", str(project)])
    assert added.exit_code == 0 and "Added #1" in added.output
    again = runner.invoke(app, ["add", "https://github.com/ADA", "--name", "Ada", "--config", str(project)])
    assert "Already listed as #1" in again.output
    listed = runner.invoke(app, ["people", "--config", str(project)])
    assert "Ada Lovelace" in listed.output


def test_mcp_server_lists_and_runs_tools(store: Any, config: Any) -> None:
    from tailwatch.mcp_server import build

    class Session:
        pass

    session = Session()
    session.store, session.config, session.env = store, config, {}
    server = build(session)

    async def go() -> tuple[list[str], Any]:
        tools = [t.name for t in await server.list_tools()]
        await server.call_tool("add_person", {"name": "Ada", "links": ["https://github.com/ada"]})
        return tools, await server.call_tool("find_people", {"query": "ada"})

    names, found = asyncio.run(go())
    assert {"find_people", "person", "signals", "why", "digest_preview", "add_person", "ask_desk"} <= set(names)
    assert "Ada" in found.content[0].text


def test_slack_router() -> None:
    kwargs = {"bot_user": "UBOT", "desk_channel": "CDESK", "digest_channels": {"CDIG"}, "bot_threads": {"111.1"}}
    assert should_answer({"type": "message", "channel": "CDESK", "user": "U1", "ts": "1"}, **kwargs) == "desk"
    assert should_answer({"type": "message", "channel": "CDESK", "user": "U1", "bot_id": "B"}, **kwargs) == ""
    assert should_answer({"type": "message", "channel": "CDIG", "user": "U1", "ts": "2"}, **kwargs) == ""
    assert should_answer({"type": "app_mention", "channel": "CDIG", "user": "U1"}, **kwargs) == "channel"
    assert should_answer({"type": "message", "channel": "CDIG", "user": "U1", "thread_ts": "111.1"}, **kwargs) == "channel"
    assert should_answer({"type": "message", "channel": "COTHER", "user": "U1"}, **kwargs) == ""
    assert should_answer({"type": "message", "channel": "CDESK", "user": "U1", "subtype": "message_changed"}, **kwargs) == ""


def test_daily_jobs_keep_receipts_and_retry_three_times(store: Any, config: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def boom(store: Any, config: Any, env: Any) -> Any:
        calls.append("pass")
        raise RuntimeError("provider down")

    monkeypatch.setattr(scheduler, "EVERY_TICK", ())
    monkeypatch.setattr(scheduler, "DAILY", (scheduler.Job("pass", "x:y", at="pass_time"),))
    monkeypatch.setattr(scheduler, "resolve", lambda target: boom)
    util.set_now(util.parse("2026-11-20T03:00:00Z"))  # after the 02:00 pass time
    for _ in range(5):
        scheduler.tick(store, config, {}, log=None)
    assert len(calls) == scheduler.MAX_ATTEMPTS
    assert store.job("pass", "2026-11-20")["state"] == "failed"


def test_a_daily_job_waits_for_its_time(store: Any, config: Any) -> None:
    util.set_now(util.parse("2026-11-20T01:00:00Z"))
    assert [job.name for job, _day in scheduler.daily_due(store, config)] == []
    util.set_now(util.parse("2026-11-20T09:05:00Z"))
    assert {job.name for job, _day in scheduler.daily_due(store, config)} >= {"pass", "digest"}
