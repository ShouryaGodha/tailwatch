"""The project's own documentation, served a section at a time to the desk: every ``docs/*.md``
split on its ``##`` headings, and each bold-led paragraph inside a section (``**The gate.**``)
as a subsection. A whole file would cost the model more than the answer is worth."""

from __future__ import annotations

import re
from pathlib import Path

from tailwatch.agent import prompts

MAX_CHARS = 6000
_HEADING = re.compile(r"^#{1,3} (.+)$", re.MULTILINE)
_BOLD = re.compile(r"^\*\*([^*]+?)\.?\*\*", re.MULTILINE)


def files() -> list[Path]:
    found: dict[str, Path] = {}
    for root in prompts.roots("docs"):
        for path in sorted(root.glob("*.md")):
            found.setdefault(path.name, path)
    return list(found.values())


def sections(sources: dict[str, str] | None = None) -> dict[str, str]:
    """``{title: body}`` for every heading of every file and every bold-led paragraph under
    one (``<heading> / <lead>``). A title found in two files is prefixed with the file's name."""
    if sources is None:
        sources = {p.stem: p.read_text(encoding="utf-8") for p in files()}
    out: dict[str, str] = {}
    for stem, source in sources.items():
        heads = list(_HEADING.finditer(source))
        for i, match in enumerate(heads):
            end = heads[i + 1].start() if i + 1 < len(heads) else len(source)
            title, body = match.group(1).strip(), source[match.end():end].strip()
            key = title if title not in out else f"{stem}: {title}"
            out[key] = body
            bolds = list(_BOLD.finditer(body))
            for j, bold in enumerate(bolds):
                b_end = bolds[j + 1].start() if j + 1 < len(bolds) else len(body)
                out[f"{key} / {bold.group(1).strip()}"] = body[bold.start():b_end].strip()
    return out


def section(name: str, sources: dict[str, str] | None = None) -> dict[str, object]:
    """The best match for ``name`` by its words, ``{title, text, also}``; an empty or unknown
    name answers with the titles."""
    table = sections(sources)
    if not table:
        return {"error": "there is no documentation in docs/ yet"}
    wanted = [w for w in re.split(r"\W+", name.casefold()) if w]
    if not wanted:
        return {"sections": list(table)}
    scored = []
    for title in table:
        key = title.casefold()
        hits = sum(1 for w in wanted if w in key)
        if hits:
            scored.append((hits / len(wanted), -len(title), title))
    if not scored:
        return {"error": f"no section matches '{name}'", "sections": list(table)[:60]}
    scored.sort(reverse=True)
    title = scored[0][2]
    body = table[title]
    return {"title": title, "text": body[:MAX_CHARS] + ("\n…" if len(body) > MAX_CHARS else ""),
            "also": [t for _, _, t in scored[1:6]]}
