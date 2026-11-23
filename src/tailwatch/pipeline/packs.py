"""Signal packs: what the judge looks for. A pack is ``prompts/packs/<name>.yaml`` (the signals,
categories, gates, how cards are deduplicated) plus a rules file in plain words. The judge's
prompt and its answer schema are both built from the pack, so a new use of tailwatch is a new
pack, not a code change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from tailwatch.agent import prompts
from tailwatch.util import sha


@dataclass(frozen=True)
class Signal:
    name: str
    heading: str
    mark: str
    describe: str
    old_after_days: int = 0
    stale_after_days: int = 0


@dataclass(frozen=True)
class Pack:
    name: str
    version: str
    description: str
    signals: tuple[Signal, ...]
    categories: dict[str, str]
    priority_categories: tuple[str, ...]
    gate_labels: tuple[str, ...]
    move_key: str
    rules: str
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.signals)

    def signal(self, name: str) -> Signal | None:
        return next((s for s in self.signals if s.name == name), None)

    def order(self, name: str) -> int:
        return self.names.index(name) if name in self.names else len(self.names)

    @property
    def prompt_version(self) -> str:
        judge = prompts.text("judge.md")
        return f"{self.name}-v{self.version}-{sha(judge + self.rules)[:8]}"


def load(name_or_path: str) -> Pack:
    path = Path(name_or_path)
    if not path.suffix:
        path = prompts.path_of("prompts", f"packs/{name_or_path}.yaml")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rules_file = data.get("rules_file")
    rules = (path.parent / rules_file).read_text(encoding="utf-8") if rules_file else ""
    signals = tuple(Signal(name=str(s["name"]), heading=str(s.get("heading") or s["name"].title()),
                           mark=str(s.get("mark") or "•"), describe=" ".join(str(s.get("describe") or "").split()),
                           old_after_days=int(s.get("old_after_days") or 0), stale_after_days=int(s.get("stale_after_days") or 0))
                    for s in data.get("signals") or [])
    if not signals:
        raise ValueError(f"{path}: a pack needs at least one signal")
    if "none" in {s.name for s in signals}:
        raise ValueError(f"{path}: 'none' is reserved")
    categories = {str(k): " ".join(str(v).split()) for k, v in (data.get("categories") or {"other": "any"}).items()}
    known = {"name", "version", "description", "signals", "categories", "priority_categories", "gate_labels", "move_key", "rules_file"}
    return Pack(name=str(data.get("name") or path.stem), version=str(data.get("version") or "1"),
                description=" ".join(str(data.get("description") or "").split()), signals=signals, categories=categories,
                priority_categories=tuple(data.get("priority_categories") or ()), gate_labels=tuple(data.get("gate_labels") or ()),
                move_key=str(data.get("move_key") or "destination"), rules=rules.strip(),
                extra={k: v for k, v in data.items() if k not in known})


def judge_schema(pack: Pack) -> dict[str, Any]:
    """The judge's answer, strict: every field present, nothing else."""
    text = {"type": "string"}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["signal", "confidence", "summary", "context", "reason", "wrong_person", "now", "category", "eligible",
                     "priority", "priority_reason", "signal_date", "same_as", "supersedes", "flags"],
        "properties": {
            "signal": {"type": "string", "enum": [*pack.names, "none"]},
            "confidence": {"type": "number"},
            "summary": text, "context": text, "reason": text,
            "wrong_person": {"type": "boolean"},
            "now": {"type": "object", "additionalProperties": False, "required": ["organization", "title", "since"],
                    "properties": {"organization": text, "title": text, "since": text}},
            "category": {"type": "string", "enum": list(pack.categories)},
            "eligible": {"type": "boolean"},
            "priority": {"type": "integer"},
            "priority_reason": text,
            "signal_date": text,
            "same_as": {"type": "integer"},
            "supersedes": {"type": "array", "items": {"type": "integer"}},
            "flags": {"type": "array", "items": {"type": "string"}},
        },
    }


def priority_text(config: Any) -> str:
    tiers = config.priority.tiers if config is not None else []
    if not tiers:
        return "No list is set; judge every organization on its merits."
    lines = [f"- {t.name}: {', '.join(t.orgs)}" for t in tiers]
    aliases = config.priority.aliases
    if aliases:
        lines.append("Also known as: " + "; ".join(f"{k} = {', '.join(v)}" for k, v in aliases.items()))
    return "\n".join(lines)


def judge_instructions(pack: Pack, config: Any) -> str:
    return prompts.render(prompts.text("judge.md"), {
        "PACK_DESCRIPTION": pack.description,
        "SIGNAL_NAMES": ", ".join(pack.names),
        "SIGNAL_LIST": "\n".join(f"- {s.name}: {s.describe}" for s in pack.signals),
        "PACK_RULES": pack.rules,
        "PRIORITY_ORGS": priority_text(config),
        "CATEGORY_NAMES": ", ".join(pack.categories),
        "CATEGORY_LIST": " ".join(f"{k}: {v}." for k, v in pack.categories.items()),
    })
