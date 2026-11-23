"""Skills in the Agent Skills format (agentskills.io): ``skills/<name>/SKILL.md``.

    ---
    name: verification
    description: Check a card before it is posted ...
    allowed-tools: cards card verify_now
    metadata:
      writes: ""
      requires: EXA_API_KEY
      rules_key: VERIFICATION:RULES
      default: "false"
      version: "1"
    ---
    # body ...

Only the six spec fields are used at the top level, so a skill folder also loads in Claude Code
or the Claude API. The desk's prompt carries the index (name and description); ``use_skill``
returns the body and from then on the skill's tools are offered. Learned rules, versioned in
settings under ``rules_key``, are appended to the body at run time and never written into the
file. A skill whose ``requires`` keys are unset is hidden.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from tailwatch.agent import prompts


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    tools: tuple[str, ...]
    body: str
    folder: Path
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def requires(self) -> tuple[str, ...]:
        return tuple(self.metadata.get("requires", "").split())

    @property
    def writes(self) -> tuple[str, ...]:
        return tuple(self.metadata.get("writes", "").split())

    @property
    def rules_key(self) -> str:
        return self.metadata.get("rules_key", "")

    @property
    def default(self) -> bool:
        return self.metadata.get("default", "false").lower() == "true"

    def file(self, name: str) -> str:
        return (self.folder / name).read_text(encoding="utf-8")


class SkillError(RuntimeError):
    pass


def parse(path: Path) -> Skill:
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---"):
        raise SkillError(f"{path} has no frontmatter")
    _, front, body = raw.split("---", 2)
    meta = yaml.safe_load(front) or {}
    name = str(meta.get("name") or "")
    if name != path.parent.name:
        raise SkillError(f"{path}: name {name!r} must match its folder {path.parent.name!r}")
    unknown = set(meta) - {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
    if unknown:
        raise SkillError(f"{path}: frontmatter keys outside the Agent Skills spec: {sorted(unknown)}")
    metadata = {str(k): str(v) for k, v in (meta.get("metadata") or {}).items()}
    return Skill(name=name, description=str(meta.get("description") or "").strip(),
                 tools=tuple(str(meta.get("allowed-tools") or "").split()), body=body.strip(),
                 folder=path.parent, metadata=metadata)


def discover() -> dict[str, Skill]:
    found: dict[str, Skill] = {}
    for root in prompts.roots("skills"):
        for path in sorted(root.glob("*/SKILL.md")):
            skill = parse(path)
            found.setdefault(skill.name, skill)
    return found


def available(env: Mapping[str, str], skills: Mapping[str, Skill] | None = None) -> dict[str, Skill]:
    skills = skills if skills is not None else discover()
    return {n: s for n, s in skills.items() if all(env.get(k) for k in s.requires)}


def index_text(skills: Mapping[str, Skill]) -> str:
    return "\n".join(f"- {s.name}: {s.description}" for s in skills.values())


def body_for(skill: Skill, rules: list[str] | None, values: Mapping[str, Any]) -> str:
    body = prompts.render(skill.body, values) if prompts.placeholders(skill.body) else skill.body
    if rules:
        body += "\n\n# Learned rules (versioned; a person applied each)\n" + "\n".join(f"- {r}" for r in rules)
    return body
