"""Small shared helpers: time as ISO-8601 UTC text, stable hashes and JSON."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

_now_override: datetime | None = None


def set_now(value: datetime | None) -> None:
    """Freeze the clock (tests and the demo)."""
    global _now_override
    _now_override = value


def now() -> datetime:
    return _now_override or datetime.now(UTC)


def iso(value: datetime | None = None) -> str:
    value = value or now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def ago(**delta: float) -> str:
    return iso(now() - timedelta(**delta))


def ahead(**delta: float) -> str:
    return iso(now() + timedelta(**delta))


def local_day(zone: str = "UTC", at: datetime | None = None) -> str:
    return (at or now()).astimezone(ZoneInfo(zone)).strftime("%Y-%m-%d")


def local_time(zone: str = "UTC", at: datetime | None = None) -> datetime:
    return (at or now()).astimezone(ZoneInfo(zone))


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def dumps(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True, ensure_ascii=False)


def loads(text: Any, default: Any = None) -> Any:
    if text is None or text == "":
        return default
    if not isinstance(text, (str, bytes)):
        return text
    try:
        return json.loads(text)
    except ValueError:
        return default


def clip(text: Any, limit: int) -> str:
    text = "" if text is None else str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"
