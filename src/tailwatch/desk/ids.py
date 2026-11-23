"""The ids people and the desk write: ``#R12`` a card review, ``#I7`` an issue, ``#P3`` a
proposal, ``#G4`` intel, ``#C9`` a candidate, ``#T2`` an engineer task, ``PR 72`` a pull
request. The ``#`` is optional (``post R12``). A bare number in a reply means the one kind of id
the thread's root post carries; when the root carries none or several, the desk asks rather
than guesses. Pure: parsing and resolving, no store."""

from __future__ import annotations

import re
from collections.abc import Mapping

KINDS = {"R": "card review", "I": "issue", "P": "proposal", "G": "intel entry", "C": "candidate", "T": "engineer task",
         "PR": "pull request"}
BARE = "bare"
_PULL = re.compile(r"(?<![A-Za-z0-9])PR\s*#?(\d{1,9})\b", re.IGNORECASE)
_PREFIXED = re.compile(r"(?<![A-Za-z0-9#])#?([RIPGCT])(\d{1,9})\b", re.IGNORECASE)
_NUMBER = re.compile(r"(?<![A-Za-z0-9.:/-])#?(\d{1,9})\b(?![.:/-]\d)")
_DATE = re.compile(r"\b\d{4}-\d{2}(?:-\d{2})?\b")
_URL = re.compile(r"https?://\S+|<[^>]+>")


def parse(text: str) -> dict[str, list[int]]:
    """The ids in ``text`` by kind letter (``R``, ``I``, ``P``, ``G``, ``C``, ``T``, ``PR``) plus
    ``bare`` for numbers with no kind. Each list keeps the order found, without repeats. Dates,
    years, URLs and Slack mentions are never ids."""
    out: dict[str, list[int]] = {}

    def add(kind: str, number: int) -> None:
        bucket = out.setdefault(kind, [])
        if number not in bucket:
            bucket.append(number)

    rest = _URL.sub(" ", _DATE.sub(" ", str(text or "")))
    for match in _PULL.finditer(rest):
        add("PR", int(match.group(1)))
    rest = _PULL.sub(" ", rest)
    for match in _PREFIXED.finditer(rest):
        add(match.group(1).upper(), int(match.group(2)))
    rest = _PREFIXED.sub(" ", rest)
    for match in _NUMBER.finditer(rest):
        number = int(match.group(1))
        if 1900 <= number <= 2100:
            continue
        add(BARE, number)
    return out


def label(kind: str, number: int) -> str:
    """How an id is written back: ``#R12``, ``PR 72``."""
    return f"PR {number}" if kind == "PR" else f"#{kind}{number}"


def root_kinds(root_ids: Mapping[str, list[int]]) -> list[str]:
    return [kind for kind, numbers in root_ids.items() if kind != BARE and numbers]


def resolve(number: int, root_ids: Mapping[str, list[int]]) -> tuple[str, str]:
    """What a bare number means: ``(kind, "")`` when the root post carries one kind of id,
    ``("", question)`` otherwise."""
    kinds = root_kinds(root_ids)
    if len(kinds) == 1:
        return kinds[0], ""
    if not kinds:
        return "", f"{number} names no kind and the post above carries no id; say #R{number}, #P{number} or #I{number}"
    return "", f"{number} could be " + " or ".join(label(k, number) for k in kinds) + "; say which"


def check(message: str, root_ids: Mapping[str, list[int]], kind: str, number: int) -> str:
    """Whether a decision names the id the person meant: '' when it does, else what to ask.
    The person's own message decides: the same kind and number is clear, the number under
    another kind is a mismatch, a bare number means the root post's one kind. A number the
    message does not name at all is the root post's own (a "post it" under a held card)."""
    said = parse(message)
    if number in said.get(kind, []):
        return ""
    other = next((k for k, numbers in said.items() if k not in (BARE, kind) and number in numbers), "")
    if other:
        return f"the message names {label(other, number)}, not {label(kind, number)}; say which is meant"
    if number not in said.get(BARE, []):
        return ""
    resolved, why = resolve(number, root_ids)
    if why:
        return why
    if resolved != kind:
        return f"{number} here means {label(resolved, number)}, not {label(kind, number)}; say which is meant"
    return ""
