"""Every registered connector keeps the contract in ``connectors.base``."""

from __future__ import annotations

import json

import pytest

from tailwatch.connectors import registry
from tailwatch.connectors.base import Checker, Feed

CHECKERS = registry.checker_classes()
FEEDS = registry.feed_classes()

SAMPLE = {"name": "Ada Lovelace", "headline": "Scientist", "bio": "Engines.", "organization": "Analytical Engines",
          "title": "Scientist", "location": "London", "website": "https://ada.dev", "flags": {"hireable": True},
          "positions": [{"title": "Scientist", "company": "Analytical Engines", "start": "2024-03", "end": "",
                         "current": True}],
          "posts": [{"id": "1001", "text": "Hello", "date": "2026-01-01T00:00:00Z", "url": "https://example.com/1"}],
          "papers": [{"id": "2601.00001", "title": "A paper", "date": "2026-01-01T00:00:00Z", "url": "",
                      "venue": "", "authors": ["Ada Lovelace"]}],
          "repos": [{"name": "engine", "description": "", "created": "2026-01-01T00:00:00Z", "url": "", "stars": 1}],
          "releases": [], "links": [], "text": "I am a scientist at Analytical Engines in London today."}


def test_every_built_in_is_registered() -> None:
    assert set(registry.CHECKERS) <= set(CHECKERS)
    assert set(registry.FEEDS) <= set(FEEDS)


@pytest.mark.parametrize("kind", sorted(registry.CHECKERS))
def test_checker_attributes(kind: str) -> None:
    cls = CHECKERS[kind]
    checker = cls()
    assert isinstance(checker, Checker)
    assert cls.kind == kind and cls.label and cls.cadence_hours > 0
    assert isinstance(cls.paid, bool) and isinstance(cls.requires, tuple)
    assert bool(cls.requires) == cls.paid  # paid sources say which key they need; free ones need none


@pytest.mark.parametrize("kind", sorted(registry.CHECKERS))
def test_diff_of_identical_content_is_empty(kind: str) -> None:
    checker = CHECKERS[kind]()
    assert checker.diff(SAMPLE, json.loads(json.dumps(SAMPLE))) == []
    assert checker.diff({}, {}) == []
    assert checker.guard(SAMPLE, SAMPLE) == ""
    text = checker.describe(SAMPLE)
    assert isinstance(text, str) and 0 < len(text) <= 4000


@pytest.mark.parametrize("kind", sorted(registry.FEEDS))
def test_feed_attributes(kind: str) -> None:
    cls = FEEDS[kind]
    assert isinstance(cls(), Feed)
    assert cls.kind == kind and isinstance(cls.requires, tuple)
