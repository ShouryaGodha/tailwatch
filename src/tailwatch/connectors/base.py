"""The connector contract.

Two kinds of connector:

**Checker** reads one source of one person (a GitHub profile, a homepage, an arXiv author) and
returns a ``Read``. The runner compares it with the previous successful read through the
checker's ``diff``, which returns mechanical ``Change``s ("bio changed", "new paper"), never
judgements. The judge decides what a change means.

Rules every checker keeps (the runner enforces the rest):

- ``gone`` only when the provider says the account or page does not exist (404/410, "user not
  found"). Timeouts, rate limits, 5xx, bot walls and parked domains are ``error``.
- ``guard(before, after)`` names a read that looks like a change but is a failed read (an empty
  experience list when the last read had positions, a homepage that shrank to an empty shell).
  Such a read is recorded as an error and never compared.
- ``credits`` / ``usd`` report what the read cost, so a pass can say what it spent.

**Feed** polls a stream of posts about anyone (an announcer account, a news search, a blog) and
returns ``FeedItem``s after a marker. The pipeline matches each item to tracked people; items
about nobody on the list become candidates.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol, runtime_checkable

from tailwatch.connectors.http import Http


@dataclass
class Read:
    status: str  # ok | error | gone | private
    content: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    raw: Any = None
    credits: float = 0.0
    usd: float = 0.0
    outage: str = ""  # a provider-wide refusal: "out of credits", "no credential", "rate limited"

    @classmethod
    def failed(cls, error: str, *, outage: str = "") -> Read:
        return cls(status="error", error=error, outage=outage)


@dataclass(frozen=True)
class Change:
    label: str
    before: str = ""
    after: str = ""
    content_date: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"label": self.label, "before": self.before, "after": self.after, "content_date": self.content_date}


@dataclass
class CheckContext:
    """What a connector may use: HTTP (injectable), the environment, a response cache and its
    own options from tailwatch.yaml."""

    http: Http
    env: Mapping[str, str] = field(default_factory=dict)
    options: Mapping[str, Any] = field(default_factory=dict)
    cache_get: Callable[[str, str, float | None], Any] = lambda _p, _k, _a: None
    cache_put: Callable[[str, str, Any], None] = lambda _p, _k, _v: None


@runtime_checkable
class Checker(Protocol):
    kind: ClassVar[str]
    label: ClassVar[str]
    cadence_hours: ClassVar[float]
    paid: ClassVar[bool]
    requires: ClassVar[tuple[str, ...]]

    def read(self, url: str, previous: Mapping[str, Any] | None, ctx: CheckContext) -> Read: ...

    def diff(self, before: Mapping[str, Any], after: Mapping[str, Any]) -> list[Change]: ...

    def guard(self, before: Mapping[str, Any], after: Mapping[str, Any]) -> str: ...

    def describe(self, content: Mapping[str, Any]) -> str: ...


class BaseChecker:
    """Defaults for a checker: no guard, a JSON description, a field-by-field diff."""

    kind: ClassVar[str] = ""
    label: ClassVar[str] = ""
    cadence_hours: ClassVar[float] = 24.0
    paid: ClassVar[bool] = False
    requires: ClassVar[tuple[str, ...]] = ()
    fields: ClassVar[tuple[str, ...]] = ()  # top-level fields whose change is an event, "<field> changed"

    def guard(self, before: Mapping[str, Any], after: Mapping[str, Any]) -> str:
        return ""

    def describe(self, content: Mapping[str, Any]) -> str:
        import json

        return json.dumps(dict(content), ensure_ascii=False, default=str)[:4000]

    def diff(self, before: Mapping[str, Any], after: Mapping[str, Any]) -> list[Change]:
        changes = []
        for name in self.fields:
            old, new = before.get(name), after.get(name)
            if (old or "") != (new or ""):
                changes.append(Change(f"{name.replace('_', ' ')} changed", str(old or ""), str(new or "")))
        return changes


@dataclass
class FeedItem:
    id: str
    url: str
    text: str
    author: str = ""
    published_at: str = ""
    mentions: dict[str, list[str]] = field(default_factory=dict)  # kind -> handles/urls the item links
    names: list[str] = field(default_factory=list)  # people the item names, when the feed says so
    raw: Any = None


@runtime_checkable
class Feed(Protocol):
    kind: ClassVar[str]
    requires: ClassVar[tuple[str, ...]]

    def poll(self, marker: str | None, options: Mapping[str, Any], ctx: CheckContext) -> tuple[list[FeedItem], str]: ...
