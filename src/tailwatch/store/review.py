"""Store methods owned by the review area. Add queries here, never raw SQL elsewhere."""

from __future__ import annotations

from typing import Any

from tailwatch.store.db import Database


class ReviewMixin:
    db: Database
    config: Any
