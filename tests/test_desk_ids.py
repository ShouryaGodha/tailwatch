"""Ids by kind, and what a bare number means under a root post."""

from __future__ import annotations

from tailwatch.desk import ids


def test_parse_kinds_with_and_without_hash() -> None:
    found = ids.parse("post #R12 and R15, apply P3, keep g4, close PR 72 and pr #73, C9 T2 #I7")
    assert found["R"] == [12, 15] and found["P"] == [3] and found["G"] == [4]
    assert found["PR"] == [72, 73] and found["C"] == [9] and found["T"] == [2] and found["I"] == [7]
    assert "bare" not in found


def test_dates_years_urls_and_mentions_are_not_ids() -> None:
    found = ids.parse("seen 2026-11-04 in 2025, see https://x.com/a/status/123 <@U12345678> at 12:30 for $0.50")
    assert found == {}


def test_bare_numbers_and_words_with_letters() -> None:
    found = ids.parse("post 12, not top3 or R2D2")
    assert found == {"bare": [12]}


def test_resolve_by_the_root_posts_one_kind() -> None:
    root = ids.parse("Held cards: #R12 Ada, #R15 Grace")
    assert ids.resolve(12, root) == ("R", "")
    kind, why = ids.resolve(12, ids.parse("#R12 and #P3"))
    assert kind == "" and "#R12 or #P12" in why
    kind, why = ids.resolve(12, {})
    assert kind == "" and "say" in why


def test_check_a_decision_against_the_message() -> None:
    root = ids.parse("#R12 #R15")
    assert ids.check("post R12", root, "R", 12) == ""
    assert ids.check("post 12", root, "R", 12) == ""
    assert "#P12" in ids.check("apply P12", root, "R", 12)
    assert ids.check("post it", root, "R", 12) == ""  # the root post's own card
    assert "could be" in ids.check("post 12", ids.parse("#R12 #P12"), "R", 12)
    assert "means #R12" in ids.check("apply 12", root, "P", 12)


def test_label() -> None:
    assert ids.label("R", 12) == "#R12" and ids.label("PR", 72) == "PR 72"
