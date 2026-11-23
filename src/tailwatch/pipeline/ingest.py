"""Getting people onto the list: inline in tailwatch.yaml, a CSV or YAML file, or one URL.

CSV columns: ``name, organization, title, tags`` plus any link kind (``github, x, homepage,
linkedin, arxiv, semantic_scholar, bluesky, rss, scholar``) or a ``links`` column of
space-separated URLs whose kind is guessed. Nobody is listed twice (any shared link counts) and
nobody removed earlier comes back.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml

from tailwatch import links as link_rules


def entries_from_file(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() in (".yaml", ".yml"):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        return list(data.get("people", data) if isinstance(data, dict) else data)
    rows = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            row = {str(k or "").strip().lower(): str(v or "").strip() for k, v in row.items()}
            links = {k: row[k] for k in link_rules.KINDS if row.get(k)}
            for url in row.get("links", "").split():
                links.setdefault(link_rules.guess_kind(url), url)
            rows.append({"name": row.get("name", ""), "organization": row.get("organization", ""), "title": row.get("title", ""),
                         "tags": [t.strip() for t in row.get("tags", "").replace(";", ",").split(",") if t.strip()], "links": links})
    return rows


def add_entries(store: Any, entries: Iterable[Mapping[str, Any]], *, because: str) -> dict[str, int]:
    counts = {"added": 0, "already": 0, "removed_before": 0, "skipped": 0}
    for entry in entries:
        name = str(entry.get("name") or "").strip()
        if not name:
            counts["skipped"] += 1
            continue
        try:
            _id, created = store.add_person(name, organization=str(entry.get("organization") or ""), title=str(entry.get("title") or ""),
                                            links=dict(entry.get("links") or {}), tags=list(entry.get("tags") or []),
                                            notes=str(entry.get("notes") or ""), added_because=because)
        except PermissionError:
            counts["removed_before"] += 1
            continue
        counts["added" if created else "already"] += 1
    return counts


def sync_watchlist(store: Any, config: Any) -> dict[str, int]:
    """Add everyone the config lists (inline and imported files). Never removes anyone."""
    total = {"added": 0, "already": 0, "removed_before": 0, "skipped": 0}
    batches = [([p.model_dump() for p in config.watchlist.people], "config")]
    for name in config.watchlist.imports:
        path = config.path(name)
        if path.exists():
            batches.append((entries_from_file(path), f"import: {path.name}"))
    for entries, because in batches:
        for key, value in add_entries(store, entries, because=because).items():
            total[key] += value
    return total


def from_url(url: str) -> dict[str, str]:
    kind = link_rules.guess_kind(url)
    return {kind: url}
