from __future__ import annotations

from tailwatch.connectors._text import (
    has_sentence,
    html_page,
    iso_date,
    link_kind,
    mentions,
    round2,
    sentences,
    unseen,
)


def test_iso_date_reads_every_shape_sources_use() -> None:
    assert iso_date("Mon, 02 Mar 2026 10:00:00 +0000") == "2026-03-02T10:00:00Z"
    assert iso_date("2026-03-02T12:00:00+02:00") == "2026-03-02T10:00:00Z"
    assert iso_date("Fri Oct 02 06:35:51 +0000 2026") == "2026-10-02T06:35:51Z"
    assert iso_date(0) == "1970-01-01T00:00:00Z"
    assert iso_date("2026-03") == "2026-03" and iso_date("nonsense") == "" and iso_date(None) == ""


def test_html_page_drops_code_menus_and_footers_and_masks_volatile_text() -> None:
    page = html_page("<html><head><title>Ada</title><body><nav>Menu</nav><p>Hello there, I am Ada.</p>"
                     "<p>Last updated: 2026-01-02. Visitors: 99</p><script>x()</script><a href='/cv.pdf'>CV</a>",
                     "https://ada.dev/")
    assert page.title == "Ada"
    assert page.text.splitlines() == ["Hello there, I am Ada.", "Last updated: <DATE>. Visitors: <N>", "CV"]
    assert page.links == ["https://ada.dev/cv.pdf"]


def test_sentences_keep_abbreviations_whole() -> None:
    assert sentences("Dr. Ada works at Analytical Engines Inc. today. She left!") == [
        "Dr. Ada works at Analytical Engines Inc. today.", "She left!"]
    assert has_sentence("I have moved to a new site.") and not has_sentence("Loading")


def test_link_kinds_and_mentions() -> None:
    assert link_kind("https://twitter.com/Ada") == ("x", "ada")
    assert link_kind("https://x.com/ada/status/1") == ("", "")
    assert link_kind("https://www.linkedin.com/in/Ada-L/") == ("linkedin", "https://www.linkedin.com/in/ada-l")
    assert link_kind("https://bsky.app/profile/ada.bsky.social") == ("bluesky", "ada.bsky.social")
    assert link_kind("https://t.co/abc") == ("", "")
    found = mentions("see https://github.com/ada and https://ada.dev/.", x_handles=["@Ada", "bot"],
                     exclude={"x": ["bot"]})
    assert found == {"x": ["ada"], "github": ["ada"], "homepage": ["https://ada.dev"]}


def test_unseen_skips_items_that_slid_into_a_full_window() -> None:
    before = [{"id": str(i), "date": f"2026-01-{10 + i:02d}"} for i in range(3)]
    after = [{"id": "9", "date": "2026-02-01"}, *before[1:], {"id": "old", "date": "2025-01-01"}]
    assert [i["id"] for i in unseen(before, after, cap=3)] == ["9"]
    assert [i["id"] for i in unseen(before, after, cap=10)] == ["old", "9"]  # a partial window: everything unseen


def test_round2() -> None:
    assert (round2(42), round2(1234), round2(98765), round2(None)) == (42, 1200, 99000, 0)
