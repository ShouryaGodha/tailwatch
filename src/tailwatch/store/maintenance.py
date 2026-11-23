"""Store methods owned by the maintenance area. Add queries here, never raw SQL elsewhere."""

from __future__ import annotations

from typing import Any

from tailwatch.store.db import Database


class MaintenanceMixin:
    db: Database
    config: Any
