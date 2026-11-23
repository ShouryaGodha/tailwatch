"""Name compatibility and organization matching for the proof checks."""

from __future__ import annotations

import pytest

from tailwatch.enrich import names


@pytest.mark.parametrize(("given", "found", "level"), [
    ("Ada Lovelace", "ada  lovelace", "exact"),
    ("Ada Lovelace", "Ada Lovelace, PhD", "exact"),
    ("José Núñez", "Jose Nunez", "exact"),
    ("Ada King Lovelace", "Ada Lovelace", "variant"),       # a middle name dropped
    ("William Gates", "Bill Gates", "variant"),              # a nickname
    ("Wei Zhang", "Zhang Wei", "variant"),                   # the order swapped
    ("María García López", "María García", "variant"),       # a compound surname cut
    ("Ada Lovelace", "A. Lovelace", "initials"),             # the first name an initial
    ("Ada Lovelace", "Ada L.", "initials"),                  # the surname shortened to an initial
    ("Wei Zhang", "Zhang W.", "initials"),                   # swapped, with an initial
    ("Ada Lovelace", "Ada Byron", "different"),
    ("Ada Lovelace", "Grace Hopper", "different"),
    ("Ada Lovelace", "B. Lovelace", "different"),
    ("Ada", "Ada Lovelace", "different"),
    ("", "Ada Lovelace", "different"),
])
def test_name_levels(given: str, found: str, level: str) -> None:
    assert names.level(given, found) == level


def test_floors_order_the_levels() -> None:
    assert names.compatible("Ada Lovelace", "A. Lovelace", "initials")
    assert not names.compatible("Ada Lovelace", "A. Lovelace", "variant")
    assert names.compatible("Ada Lovelace", "Ada King Lovelace", "variant")
    assert not names.at_least("exact", "nonsense")


def test_organizations_in_text() -> None:
    table = names.alias_table({"Google DeepMind": ["GDM"]}, {"deepmind": "Google DeepMind"})
    assert names.org_in_text("Research Scientist at DeepMind", "Google DeepMind", table)
    assert names.org_in_text("now @ GDM", "Google DeepMind", table)
    assert names.org_in_text("Researcher, Google DeepMind (London)", "Google DeepMind")
    assert names.org_in_text("Fan of @analyticalengines", "Analytical Engines")
    assert not names.org_in_text("working on metadata", "Meta")
    assert not names.org_in_text("Research at a lab", "Research")
    assert names.org_match("Analytical Engines Inc", "Analytical Engines")
    assert names.org_match("GDM", "DeepMind", table)
    assert not names.org_match("Analytical Engines", "Difference Labs")
    assert names.any_org_in("joined Difference Labs", ["Analytical Engines", "Difference Labs"]) == "Difference Labs"
