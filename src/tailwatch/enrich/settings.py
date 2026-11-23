"""The enricher's tunables, versioned in settings, and the limits on them.

Every value has a default, so a fresh install needs no rows. A change is validated here first,
whoever makes it (a person on the desk, a maintainer proposal a person applied), and stored as
a new version (``store.set_setting`` keeps the history); a revert is one more version. Limits
exist so a tunable can be moved, never switched off: a confidence floor of 0 or an empty name
check would let the enricher add strangers.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

FLOORS = "ENRICHMENT:FLOORS"
RULES = "ENRICHMENT:RULES"
ALIASES = "ENRICHMENT:ALIASES"
OUT_OF_SCOPE_ROLES = "ENRICHMENT:OUT_OF_SCOPE_ROLES"
LINKS_PER_DAY = "ENRICHMENT:LINKS_PER_DAY"
KEYS = (FLOORS, RULES, ALIASES, OUT_OF_SCOPE_ROLES, LINKS_PER_DAY)
SHORT = {k.split(":", 1)[1].lower(): k for k in KEYS}  # "floors" -> "ENRICHMENT:FLOORS"

DEFAULTS: dict[str, Any] = {
    FLOORS: {"confidence_min": 0.8, "stale_days": 180, "name_level_min": "initials"},
    RULES: [],
    ALIASES: {},
    OUT_OF_SCOPE_ROLES: [],
    LINKS_PER_DAY: 25,
}
FLOOR_RANGES = {"confidence_min": (0.6, 0.95), "stale_days": (30, 365)}
NAME_LEVELS = ("initials", "variant", "exact")
MAX_RULES, MAX_RULE_CHARS = 15, 300
MAX_ALIASES, MAX_ROLES, MAX_ROLE_CHARS = 200, 40, 40
LINKS_PER_DAY_RANGE = (0, 200)


class SettingError(ValueError):
    """A value outside the limits; the message says which limit."""


def key_of(name: str) -> str:
    """The full key for ``floors``, ``FLOORS`` or ``ENRICHMENT:FLOORS``."""
    text = str(name or "").strip()
    if text.upper() in KEYS:
        return text.upper()
    found = SHORT.get(text.lower().removeprefix("enrichment:"))
    if not found:
        raise SettingError(f"unknown setting {name!r}; one of {', '.join(SHORT)}")
    return found


_URL = re.compile(r"https?://|www\.|\b[a-z0-9-]+\.(?:com|org|net|io|ai|dev|co)\b", re.IGNORECASE)
_SLACK = re.compile(r"<[@#!][^>]*>|\b[CUWGD][0-9A-Z]{8,}\b")
_ORDER = re.compile(r"\b(?:always|never|must|do not|don't)\b[^.]{0,40}\b(?:post|send|publish|announce|add|approve|confirm|"
                    r"dismiss|delete|remove|message|notify|mention)\b|\bignore (?:the|all|any|every|previous)\b",
                    re.IGNORECASE)


def scan_text(text: str) -> str:
    """Why a learned rule's text is refused, or ''. A rule says how to read evidence; it never
    carries a link, a Slack id, or an order to post, add or skip people outright."""
    value = " ".join(str(text or "").split())
    if not value:
        return "a rule is a sentence"
    if len(value) > MAX_RULE_CHARS:
        return f"a rule is at most {MAX_RULE_CHARS} characters"
    if _URL.search(value):
        return "a rule carries no URL"
    if _SLACK.search(value):
        return "a rule carries no Slack id"
    if _ORDER.search(value):
        return "a rule says how to read evidence, not what to post, add or skip"
    return ""


def validate(key: str, value: Any) -> Any:
    """The value as it will be stored, or SettingError."""
    key = key_of(key)
    if key == FLOORS:
        if not isinstance(value, Mapping):
            raise SettingError("the floors are an object: confidence_min, stale_days, name_level_min")
        out = dict(DEFAULTS[FLOORS])
        for name, raw in value.items():
            if name in FLOOR_RANGES:
                lo, hi = FLOOR_RANGES[name]
                try:
                    number: float = float(raw) if name == "confidence_min" else int(raw)
                except (TypeError, ValueError) as error:
                    raise SettingError(f"{name} must be a number") from error
                if not lo <= number <= hi:
                    raise SettingError(f"{name} must be between {lo} and {hi}")
                out[name] = number
            elif name == "name_level_min":
                if raw not in NAME_LEVELS:
                    raise SettingError(f"name_level_min is one of {', '.join(NAME_LEVELS)}")
                out[name] = raw
            else:
                raise SettingError(f"unknown floor {name}")
        return out
    if key == RULES:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise SettingError("the learned rules are a list of sentences")
        rules = [" ".join(v.split()) for v in value if v.strip()]
        if len(rules) > MAX_RULES:
            raise SettingError(f"at most {MAX_RULES} learned rules")
        for rule in rules:
            why = scan_text(rule)
            if why:
                raise SettingError(f"{why}: {rule[:80]}")
        return list(dict.fromkeys(rules))
    if key == ALIASES:
        if not isinstance(value, Mapping) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
            raise SettingError("the aliases map one spelling to one organization name")
        aliases = {" ".join(k.split()).casefold(): " ".join(v.split()) for k, v in value.items() if k.strip() and v.strip()}
        if len(aliases) > MAX_ALIASES:
            raise SettingError(f"at most {MAX_ALIASES} aliases")
        if any(_URL.search(k) or _URL.search(v) for k, v in aliases.items()):
            raise SettingError("an alias is a name, not a link")
        return aliases
    if key == OUT_OF_SCOPE_ROLES:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise SettingError("the out-of-scope roles are a list of words")
        roles = [" ".join(v.split()).casefold() for v in value if v.strip()]
        if len(roles) > MAX_ROLES:
            raise SettingError(f"at most {MAX_ROLES} roles")
        if any(len(r) > MAX_ROLE_CHARS for r in roles):
            raise SettingError(f"a role is at most {MAX_ROLE_CHARS} characters")
        return list(dict.fromkeys(roles))
    if key == LINKS_PER_DAY:
        try:
            number = int(value)
        except (TypeError, ValueError) as error:
            raise SettingError("links_per_day is a whole number") from error
        lo, hi = LINKS_PER_DAY_RANGE
        if not lo <= number <= hi:
            raise SettingError(f"links_per_day must be between {lo} and {hi}")
        return number
    raise SettingError(f"unknown setting {key}")  # pragma: no cover - key_of refuses first


def get(store: Any, key: str) -> Any:
    """The current value; a stored value that no longer validates falls back to the default."""
    key = key_of(key)
    default = DEFAULTS[key]
    raw = store.get_setting(key, None) if store is not None else None
    if raw is None:
        return default.copy() if isinstance(default, (dict, list)) else default
    try:
        return validate(key, raw)
    except SettingError:
        return default.copy() if isinstance(default, (dict, list)) else default


def floors(store: Any) -> dict[str, Any]:
    return dict(get(store, FLOORS))


def rules(store: Any) -> list[str]:
    return list(get(store, RULES))


def aliases(store: Any) -> dict[str, str]:
    return dict(get(store, ALIASES))


def out_of_scope_roles(store: Any) -> list[str]:
    return list(get(store, OUT_OF_SCOPE_ROLES))


def links_per_day(store: Any) -> int:
    return int(get(store, LINKS_PER_DAY))


def merged(key: str, current: Any, change: Any) -> Any:
    """What a desk change means for the whole value: a partial floors or aliases object merges
    into the current one (an alias set to '' is removed); a sentence is added to a list; a list
    replaces it."""
    key = key_of(key)
    if key in (FLOORS, ALIASES) and isinstance(change, Mapping):
        out = {**dict(current), **{k: v for k, v in change.items()}}
        if key == ALIASES:
            out = {k: v for k, v in out.items() if str(v).strip()}
        return out
    if key in (RULES, OUT_OF_SCOPE_ROLES) and isinstance(change, str):
        text = change.strip()
        if text.startswith("-"):
            drop = " ".join(text[1:].split()).casefold()
            return [v for v in current if v.casefold() != drop]
        return [*current, text.removeprefix("+").strip()]
    return change


def change(store: Any, key: str, value: Any, *, by: str, note: str = "") -> dict[str, Any]:
    """Validate and store a new version. Returns {key, version, value}."""
    key = key_of(key)
    stored = validate(key, merged(key, get(store, key), value))
    version = store.set_setting(key, stored, by=by, note=note)
    return {"key": key, "version": version, "value": stored}


def revert(store: Any, key: str, version: int, *, by: str) -> dict[str, Any]:
    """Put back the value before ``version`` (a new version records the revert)."""
    key = key_of(key)
    versions = {int(h["version"]) for h in store.setting_history(key, limit=500)}
    if int(version) not in versions:
        raise SettingError(f"{key} has no version {version}")
    if any(v < int(version) for v in versions):
        new = store.revert_setting(key, int(version), by=by)
    else:  # the first version replaced the default
        new = store.set_setting(key, DEFAULTS[key], by=by, note=f"revert of version {version}")
    return {"key": key, "version": new, "value": get(store, key)}


def show(store: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in KEYS:
        row = store.setting_row(key) if store is not None else None
        out[key] = {"value": get(store, key), "version": int(row["version"]) if row else 0,
                    "history": [{"version": h["version"], "by": h["by_whom"], "note": h["note"], "at": h["at"][:16]}
                                for h in (store.setting_history(key, limit=5) if store is not None else [])]}
    return out
