"""Prompt files and the placeholders code fills in.

Prompts live in ``prompts/`` (and skills in ``skills/``) at the repository root; an installed
wheel carries them under ``tailwatch/_bundled``. ``TAILWATCH_HOME`` or a project's own
``prompts/`` folder (next to ``tailwatch.yaml``) overrides a file by name, so a user can change
what the judge looks for without forking the package.

``{{NAME}}`` placeholders are filled from code (``render``); an unknown name is an error, never
a silent blank.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Z0-9_]+)\s*\}\}")
PACKAGE = Path(__file__).resolve().parent.parent


class PromptError(RuntimeError):
    pass


def roots(kind: str) -> list[Path]:
    """Folders searched for ``kind`` (prompts, skills, presets), first match wins."""
    found: list[Path] = []
    for base in (os.environ.get("TAILWATCH_HOME"), os.getcwd()):
        if base:
            path = Path(base) / kind
            if path.is_dir():
                found.append(path)
    repo = PACKAGE.parent.parent / kind  # src/tailwatch -> repo root
    bundled = PACKAGE / "_bundled" / kind
    for path in (repo, bundled):
        if path.is_dir() and path not in found:
            found.append(path)
    return found


def path_of(kind: str, name: str) -> Path:
    for root in roots(kind):
        candidate = root / name
        if candidate.exists():
            return candidate
    raise PromptError(f"no {kind} file named {name} (looked in {', '.join(str(r) for r in roots(kind)) or 'nowhere'})")


def text(name: str) -> str:
    return path_of("prompts", name).read_text(encoding="utf-8")


def render(template: str, values: Mapping[str, object]) -> str:
    def fill(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise PromptError(f"the prompt asks for {{{{{key}}}}} and nothing fills it")
        return str(values[key])

    return _PLACEHOLDER.sub(fill, template)


def placeholders(template: str) -> set[str]:
    return set(_PLACEHOLDER.findall(template))
