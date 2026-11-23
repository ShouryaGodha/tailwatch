"""No private identifiers in the repository: tailwatch is a clean-room project, and this test
keeps it that way. The patterns are assembled at run time so this file does not match itself."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "dist", "build", "digests", "tailwatch-demo"}
TEXT_SUFFIXES = {".py", ".md", ".yaml", ".yml", ".toml", ".json", ".csv", ".txt", ".sql", ".cfg", ".ini", ".example", ""}


def _patterns() -> list[re.Pattern[str]]:
    words = [
        "micro" + "forge", "remote" + "_codex", "remote" + "-codex", "tanay" + "j10", "di" + "nq",
        "byte" + "dance", "periodic" + " labs", "ten" + "uo", "sourcing" + ".micro",
    ]
    patterns = [re.compile(re.escape(w), re.IGNORECASE) for w in words]
    patterns.append(re.compile(r"\b" + "Mi" + r"ke\b"))  # a product name; the lowercase nickname is fine
    patterns.append(re.compile(r"\b" + "Et" + r"ched\b"))
    patterns.append(re.compile(r"\bxox[bpa]-[0-9A-Za-z-]{10,}"))  # Slack tokens
    patterns.append(re.compile(r"\bsk-ant-[0-9A-Za-z_-]{10,}"))  # Anthropic keys
    patterns.append(re.compile(r"\bgh[po]_[0-9A-Za-z]{20,}"))  # GitHub tokens
    return patterns


def _files() -> list[Path]:
    out = []
    for path in ROOT.rglob("*"):
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts) or not path.is_file():
            continue
        if path.suffix in TEXT_SUFFIXES and path.stat().st_size < 2_000_000 and path.name != ".env":
            out.append(path)
    return out


def test_no_private_identifiers_or_secrets() -> None:
    hits = []
    for path in _files():
        if path.resolve() == Path(__file__).resolve():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in _patterns():
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                hits.append(f"{path.relative_to(ROOT)}:{line}: {match.group(0)!r}")
    assert not hits, "private identifiers found:\n" + "\n".join(hits[:50])
