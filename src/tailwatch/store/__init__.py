"""The one writer: every read and write of tailwatch's tables goes through ``Store``."""

from __future__ import annotations

from typing import Any

from tailwatch.store.cards import CardsMixin
from tailwatch.store.custodian import CustodianMixin
from tailwatch.store.enrich import EnrichMixin
from tailwatch.store.maintenance import MaintenanceMixin
from tailwatch.store.review import ReviewMixin
from tailwatch.store.checks import ChecksMixin
from tailwatch.store.core import CoreMixin, LockBusy
from tailwatch.store.db import Database
from tailwatch.store.desk import DeskMixin
from tailwatch.store.people import PeopleMixin

__all__ = ["Database", "LockBusy", "Store", "open_store"]


class Store(CoreMixin, PeopleMixin, ChecksMixin, CardsMixin, DeskMixin, EnrichMixin, ReviewMixin, CustodianMixin,
            MaintenanceMixin):
    def __init__(self, db: Database, config: Any = None) -> None:
        self.db = db
        self.config = config


def open_store(url: str = "sqlite:///:memory:", config: Any = None, *, migrate: bool = True) -> Store:
    db = Database(url)
    if migrate:
        db.migrate()
    return Store(db, config)
