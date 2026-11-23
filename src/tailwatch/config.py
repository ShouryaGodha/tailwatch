"""``tailwatch.yaml`` (what to watch, how, where to report) and ``.env`` (secrets).

Everything has a default, so ``tailwatch demo`` and ``tailwatch init`` work from nothing. The
config is validated once at load and handed around as a ``Config``; secrets stay in the
environment mapping and never enter the config object.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from tailwatch.agent.model import DEFAULT_MODEL


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Models(Strict):
    default: str = DEFAULT_MODEL
    judge: str = ""
    editor: str = ""
    enricher: str = ""
    ranker: str = ""
    desk: str = ""
    maintainer: str = ""
    identifier: str = ""
    engineer: str = ""

    def for_(self, role: str) -> str:
        return str(getattr(self, role, "") or self.default)


class Tier(Strict):
    name: str
    orgs: list[str] = Field(default_factory=list)


class Priority(Strict):
    tiers: list[Tier] = Field(default_factory=list)
    aliases: dict[str, list[str]] = Field(default_factory=dict)
    trusted_feeds: list[str] = Field(default_factory=list)  # feed names whose reports skip the editor


class PersonEntry(Strict):
    name: str
    organization: str = ""
    title: str = ""
    links: dict[str, str] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    notes: str = ""


class Watchlist(Strict):
    people: list[PersonEntry] = Field(default_factory=list)
    imports: list[str] = Field(default_factory=list)  # CSV or YAML files, relative to the config


class Connector(Strict):
    enabled: bool = True
    cadence_hours: float | None = None
    options: dict[str, Any] = Field(default_factory=dict)


class Feed(Strict):
    kind: str
    name: str = ""
    options: dict[str, Any] = Field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.name or f"{self.kind}:{next(iter(self.options.values()), '')}"


class Slack(Strict):
    digest_channel: str = ""
    desk_channel: str = ""
    shadow_channel: str = ""


class Delivery(Strict):
    console: bool = True
    markdown_dir: str = "digests"
    slack: Slack = Field(default_factory=Slack)
    site_url: str = ""  # a link printed under the digest, when you host a page of alerts


class Schedule(Strict):
    timezone: str = "UTC"
    digest_time: str = "09:00"
    pass_time: str = "02:00"
    maintenance_time: str = "09:30"
    tick_minutes: int = 30


class Budgets(Strict):
    editor_per_card: float = 0.15
    editor_daily: float = 10.0
    enrich_per_person: float = 0.20
    enrich_daily: float = 15.0
    engineer_per_task: float = 6.0
    engineer_daily: float = 20.0
    maintainer_daily: float = 2.0
    desk_per_message: float = 0.50


class Digest(Strict):
    cap: int = 10
    window_hours: float = 26
    repost_days: int = 30
    stale_signal_days: int = 14
    old_move_days: int = 180


class Switches(Strict):
    verify: Literal["off", "shadow", "on"] = "shadow"
    rank: bool = True
    engineer: bool = False
    candidates: bool = True


class Engineer(Strict):
    repo: str = ""  # owner/name on GitHub; empty means a local checkout only
    base: str = "main"
    allowed_paths: list[str] = Field(default_factory=lambda: ["src/tailwatch/", "prompts/", "skills/", "tests/", "docs/"])
    protected_paths: list[str] = Field(default_factory=lambda: [".github/", "src/tailwatch/desk/engineer.py",
                                                                 "src/tailwatch/desk/engineer_runner.py", ".env"])
    backend: Literal["auto", "claude", "native"] = "auto"


class Config(Strict):
    name: str = "tailwatch"
    database: str = "sqlite:///tailwatch.db"
    signals: str = "career-moves"
    models: Models = Field(default_factory=Models)
    priority: Priority = Field(default_factory=Priority)
    watchlist: Watchlist = Field(default_factory=Watchlist)
    connectors: dict[str, Connector] = Field(default_factory=dict)
    feeds: list[Feed] = Field(default_factory=list)
    delivery: Delivery = Field(default_factory=Delivery)
    schedule: Schedule = Field(default_factory=Schedule)
    budgets: Budgets = Field(default_factory=Budgets)
    digest: Digest = Field(default_factory=Digest)
    switches: Switches = Field(default_factory=Switches)
    engineer: Engineer = Field(default_factory=Engineer)
    team: list[str] = Field(default_factory=list)
    require_team_list: bool = False
    pricing: dict[str, tuple[float, float, float]] = Field(default_factory=dict)
    base_dir: str = "."

    @field_validator("database")
    @classmethod
    def _db(cls, value: str) -> str:
        if not value.startswith(("sqlite://", "postgresql://", "postgres://")):
            raise ValueError("database must be sqlite:///path or postgresql://...")
        return value

    def connector(self, kind: str) -> Connector:
        from tailwatch.connectors.registry import DEFAULT_ENABLED

        found = self.connectors.get(kind)
        if found is not None:
            return found
        return Connector(enabled=kind in DEFAULT_ENABLED)

    def path(self, relative: str) -> Path:
        path = Path(relative)
        return path if path.is_absolute() else Path(self.base_dir) / path

    def tier_orgs(self) -> dict[str, list[str]]:
        return {t.name: list(t.orgs) for t in self.priority.tiers}


def find_config(start: str | os.PathLike[str] | None = None) -> Path | None:
    explicit = os.environ.get("TAILWATCH_CONFIG")
    if explicit:
        return Path(explicit)
    here = Path(start or os.getcwd()).resolve()
    for folder in (here, *here.parents):
        candidate = folder / "tailwatch.yaml"
        if candidate.exists():
            return candidate
    return None


def load(path: str | os.PathLike[str] | None = None, *, overrides: Mapping[str, Any] | None = None) -> Config:
    found = Path(path) if path else find_config()
    data: dict[str, Any] = {}
    base = Path(os.getcwd())
    if found is not None:
        data = yaml.safe_load(Path(found).read_text(encoding="utf-8")) or {}
        base = Path(found).resolve().parent
    data = {**data, **(overrides or {})}
    if os.environ.get("TAILWATCH_DATABASE") and not (overrides or {}).get("database"):
        data["database"] = os.environ["TAILWATCH_DATABASE"]  # a hosted database in CI or the cloud
    data.setdefault("base_dir", str(base))
    config = Config.model_validate(data)
    db = config.database
    if db.startswith("sqlite:///") and not db.startswith("sqlite:////") and db != "sqlite:///:memory:":
        config.database = "sqlite:///" + str(config.path(db.removeprefix("sqlite:///")))
    return config


def load_env(path: str | os.PathLike[str] | None = None, *, into: dict[str, str] | None = None) -> dict[str, str]:
    """The process environment plus ``.env`` (values already set in the process win)."""
    env = dict(os.environ if into is None else into)
    candidate = Path(path) if path else (find_config() or Path("tailwatch.yaml")).resolve().parent / ".env"
    if candidate.exists():
        for line in candidate.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip().removeprefix("export ").strip()
            value = value.strip().strip('"').strip("'")
            env.setdefault(key, value)
    return env
