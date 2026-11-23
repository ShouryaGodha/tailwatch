"""Which connectors exist. Built-ins are listed here; a separate package adds its own through
the ``tailwatch.connectors`` entry point group (``name = "package.module:ClassName"``)."""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from importlib.metadata import entry_points
from typing import Any

CHECKERS = {
    "github": "tailwatch.connectors.github:GitHubChecker",
    "homepage": "tailwatch.connectors.homepage:HomepageChecker",
    "arxiv": "tailwatch.connectors.arxiv:ArxivChecker",
    "semantic_scholar": "tailwatch.connectors.semantic_scholar:SemanticScholarChecker",
    "bluesky": "tailwatch.connectors.bluesky:BlueskyChecker",
    "rss": "tailwatch.connectors.rss:RssChecker",
    "x": "tailwatch.connectors.x:XChecker",
    "linkedin": "tailwatch.connectors.linkedin:LinkedInChecker",
}
FEEDS = {
    "hackernews": "tailwatch.connectors.feeds.hackernews:HackerNewsFeed",
    "rss_feed": "tailwatch.connectors.feeds.rss_feed:RssFeed",
    "bluesky_account": "tailwatch.connectors.feeds.bluesky_account:BlueskyAccountFeed",
    "x_account": "tailwatch.connectors.feeds.x_account:XAccountFeed",
}
# Free sources are on unless the config turns them off; paid ones are off until turned on.
DEFAULT_ENABLED = {"github", "homepage", "arxiv", "semantic_scholar", "bluesky", "rss"}


def _load(path: str) -> Any:
    module, _, name = path.partition(":")
    return getattr(importlib.import_module(module), name)


def _plugins() -> dict[str, str]:
    try:
        return {ep.name: ep.value for ep in entry_points(group="tailwatch.connectors")}
    except Exception:
        return {}


def checker_classes() -> dict[str, type]:
    out: dict[str, type] = {}
    for kind, path in {**CHECKERS, **_plugins()}.items():
        cls = _load(path)
        if getattr(cls, "kind", None) and hasattr(cls, "read"):
            out[kind] = cls
    return out


def feed_classes() -> dict[str, type]:
    out: dict[str, type] = {}
    for kind, path in {**FEEDS, **_plugins()}.items():
        cls = _load(path)
        if hasattr(cls, "poll"):
            out[kind] = cls
    return out


def enabled_checkers(config: Any, env: Mapping[str, str]) -> dict[str, Any]:
    """Checker instances the config turns on and whose required keys are set, with the config's
    cadence applied."""
    out: dict[str, Any] = {}
    for kind, cls in checker_classes().items():
        settings = config.connector(kind) if config is not None else None
        enabled = settings.enabled if settings is not None else kind in DEFAULT_ENABLED
        if not enabled or any(not env.get(key) for key in getattr(cls, "requires", ())):
            continue
        checker = cls()
        if settings is not None and settings.cadence_hours:
            checker.cadence_hours = settings.cadence_hours
        out[kind] = checker
    return out


def status(config: Any, env: Mapping[str, str]) -> list[dict[str, Any]]:
    """One row per connector: on, off, or missing a key (for ``tailwatch doctor``)."""
    rows = []
    for kind, cls in {**checker_classes(), **feed_classes()}.items():
        settings = config.connector(kind) if config is not None and kind in checker_classes() else None
        missing = [k for k in getattr(cls, "requires", ()) if not env.get(k)]
        rows.append({"kind": kind, "type": "feed" if kind in FEEDS else "checker", "paid": bool(getattr(cls, "paid", False)),
                     "enabled": settings.enabled if settings is not None else kind in DEFAULT_ENABLED or kind in FEEDS,
                     "missing": missing, "label": getattr(cls, "label", kind)})
    return rows
