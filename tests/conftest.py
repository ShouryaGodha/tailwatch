"""Shared fixtures: an in-memory store, a config, a scripted model, a fake checker. No test
reaches the network or spends money."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import pytest

from tailwatch import config as config_module
from tailwatch import util
from tailwatch.agent import model as model_client
from tailwatch.agent.providers.scripted import ScriptedAdapter, as_reply
from tailwatch.connectors.base import BaseChecker, Change, CheckContext, Read
from tailwatch.store import open_store


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_client, "_sleep", lambda _s: None)
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "SLACK_BOT_TOKEN", "TWITTERAPI_IO_KEY", "APIFY_API_KEY", "EXA_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    yield
    util.set_now(None)
    model_client._adapters.pop("scripted", None)


@pytest.fixture
def config(tmp_path: Any) -> config_module.Config:
    return config_module.load(None, overrides={
        "name": "Test", "base_dir": str(tmp_path), "database": "sqlite:///:memory:",
        "models": {"default": "scripted:test"},
        "priority": {"tiers": [{"name": "frontier", "orgs": ["Analytical Engines"]}], "trusted_feeds": ["trusted-news"]},
        "delivery": {"console": False, "markdown_dir": "digests"},
        "connectors": {"fake": {"enabled": True}},
    })


@pytest.fixture
def store(config: config_module.Config) -> Any:
    return open_store("sqlite:///:memory:", config)


@pytest.fixture
def model() -> Callable[[Callable[[Any], Any]], ScriptedAdapter]:
    """``model(answer)`` installs a scripted provider answering every request with ``answer(request)``."""

    def install(answer: Callable[[Any], Any]) -> ScriptedAdapter:
        adapter = ScriptedAdapter(answer)
        model_client.register_adapter("scripted", adapter)
        return adapter

    return install


def verdict(signal: str = "none", **overrides: Any) -> dict[str, Any]:
    base = {"signal": signal, "confidence": 0.9, "summary": f"{signal} summary", "context": "Researcher at Analytical Engines",
            "reason": "test", "wrong_person": False, "now": {"organization": "Analytical Engines", "title": "Researcher", "since": ""},
            "category": "researcher", "eligible": True, "priority": 70, "priority_reason": "", "signal_date": "",
            "same_as": 0, "supersedes": [], "flags": []}
    return {**base, **overrides}


class FakeChecker(BaseChecker):
    """Reads whatever ``pages[url]`` holds now; ``pages[url] = None`` means gone."""

    kind = "fake"
    label = "Fake"
    cadence_hours = 24
    fields = ("bio", "organization")

    def __init__(self, pages: dict[str, Any] | None = None) -> None:
        self.pages = pages if pages is not None else {}

    def read(self, url: str, previous: Mapping[str, Any] | None, ctx: CheckContext) -> Read:
        page = self.pages.get(url, "missing")
        if page is None:
            return Read(status="gone", error="404")
        if page == "missing":
            return Read.failed("no fixture")
        if isinstance(page, Read):
            return page
        return Read(status="ok", content=dict(page))

    def diff(self, before: Mapping[str, Any], after: Mapping[str, Any]) -> list[Change]:
        return super().diff(before, after)


__all__ = ["FakeChecker", "as_reply", "verdict"]
