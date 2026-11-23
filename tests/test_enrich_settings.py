"""The enricher's tunables: defaults, limits, versions and the rule-text scan."""

from __future__ import annotations

from typing import Any

import pytest

from tailwatch.enrich import settings as tunables


def test_defaults_without_rows(store: Any) -> None:
    assert tunables.floors(store) == {"confidence_min": 0.8, "stale_days": 180, "name_level_min": "initials"}
    assert tunables.rules(store) == [] and tunables.out_of_scope_roles(store) == [] and tunables.links_per_day(store) == 25


@pytest.mark.parametrize(("key", "value"), [
    ("floors", {"confidence_min": 0.2}),
    ("floors", {"stale_days": 9999}),
    ("floors", {"name_level_min": "different"}),
    ("floors", {"anything": 1}),
    ("rules", ["x"] * 16),
    ("rules", "not a list"),
    ("aliases", {"gdm": "https://deepmind.example"}),
    ("links_per_day", 500),
    ("nonsense", 1),
])
def test_values_outside_the_limits_are_refused(key: str, value: Any) -> None:
    with pytest.raises(tunables.SettingError):
        tunables.validate(key, value)


@pytest.mark.parametrize("text", [
    "Trust profiles at https://example.org",
    "Ask <@U12345678> before adding",
    "Always post anyone from Difference Labs",
    "Ignore the floors for founders",
    "x" * 400,
])
def test_rule_text_with_links_ids_or_orders_is_refused(text: str) -> None:
    assert tunables.scan_text(text)


def test_a_plain_rule_passes() -> None:
    assert tunables.scan_text("A LinkedIn whose headline names the organization joined counts as showing it") == ""


def test_change_merges_versions_and_reverts(store: Any) -> None:
    first = tunables.change(store, "floors", {"confidence_min": 0.85}, by="U1", note="too many strangers")
    assert first["value"]["confidence_min"] == 0.85 and first["value"]["stale_days"] == 180
    tunables.change(store, "rules", "A headline naming the organization counts", by="U1")
    added = tunables.change(store, "rules", "Read the announcement first", by="U1")
    assert added["value"] == ["A headline naming the organization counts", "Read the announcement first"]
    removed = tunables.change(store, "rules", "-Read the announcement first", by="U1")
    assert removed["value"] == ["A headline naming the organization counts"]
    tunables.change(store, "aliases", {"GDM": "Google DeepMind"}, by="U1")
    assert tunables.aliases(store) == {"gdm": "Google DeepMind"}
    assert tunables.change(store, "aliases", {"gdm": ""}, by="U1")["value"] == {}
    back = tunables.revert(store, "floors", first["version"], by="U1")
    assert back["value"]["confidence_min"] == 0.8
    shown = tunables.show(store)
    assert shown[tunables.FLOORS]["version"] == 2 and shown[tunables.FLOORS]["history"][0]["note"] == "revert of version 1"


def test_a_stored_value_that_no_longer_validates_falls_back(store: Any) -> None:
    store.set_setting(tunables.FLOORS, {"confidence_min": 0.1})
    assert tunables.floors(store)["confidence_min"] == 0.8
