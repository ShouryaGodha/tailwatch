"""The desk note: a fixed layout rendered by code, at most ten needs-you lines, each with its id
and the exact reply; posted once a day with its post id and the ids it lists kept."""

from __future__ import annotations

from typing import Any

import pytest

from tailwatch import util
from tailwatch.desk import note, notices


@pytest.fixture
def posts() -> Any:
    sent: list[str] = []

    def poster(text: str, thread: str) -> str:
        sent.append(text)
        return f"ts-{len(sent)}"

    notices.set_poster(poster)
    yield sent
    notices.set_poster(None)


def held(n: int) -> dict[str, Any]:
    return {"id": n, "person": f"Person {n}", "signal": "left", "summary": "left Analytical Engines", "why": "no dated post"}


def packet(**desk: Any) -> dict[str, Any]:
    return {"day": "2026-11-23", "desk": {"held": [], "waiting": [], "open_tasks": [], "intel_due": [], **desk},
            "reviews": {"total": 4, "by_state": {"verified": 2, "held": 1, "dropped": 1},
                        "dropped": [{"id": 9, "person": "Grace Hopper", "why": "a duplicate of a posted move"}]},
            "issues": {"total": 3, "changed": 2, "noop": 1, "failed": []},
            "watchers": {"kinds": {"github": {"ok": 10, "error": 2}}, "errors": [{"kind": "github", "n": 2, "error": "HTTP 403"}]},
            "spend": {"today": {"judge": 0.5, "daily_review": 0.25}, "caps": {"maintainer_daily": 2.0, "editor_daily": 10.0}}}


ANSWER = {"summary": "Two cards wait for you and the GitHub watcher was refused twice.",
          "lessons": [{"lesson": "Cards held for a stale profile read", "count": 3, "example_ids": ["R1"]}],
          "grades": [{"agent": "judge", "subject": "V12", "verdict": "wrong", "kind": "obvious", "why": "a flag alone"},
                     {"agent": "editor", "subject": "R3", "verdict": "right", "kind": "pattern", "why": "fine"}],
          "proposals": [], "issues": [], "needs_owner": [{"what": "The GitHub token is refused", "why": "every read fails"}]}


def test_the_layout_in_order_with_every_reply(posts: Any) -> None:
    text = note.render(packet(
        held=[held(12)],
        waiting=[{"id": 3, "skill": "verification", "setting": "RULES", "op": "add", "text": "Read the dates.", "rationale": "two holds"},
                 {"id": 4, "skill": "judge", "setting": "OTHER", "op": "set", "text": "Say a reworded bio is no move.", "urgent": True},
                 {"id": 5, "skill": "verification", "setting": "REVERT", "op": "revert", "text": "revert #P2", "target": 2,
                  "why": "holdout recall 90 to 70"}],
        open_tasks=[{"id": 4, "pr": "https://github.com/acme/tw/pull/72", "title": "Say a reworded bio is no move", "review": "ship"}],
        intel_due=[{"id": 7, "text": "the lab is hiring", "by": "sam", "on": "2026-10-20"}]),
        ANSWER, outcome={"issues": [{"id": 8, "kind": "other", "subject_key": "person:1", "created": True}],
                         "settled": {"merged": [2], "closed": []}, "expired": 1})
    lines = text.splitlines()
    assert lines[0] == "*desk note · Monday 23 November*"
    assert lines[1] == "Why this note: Two cards wait for you and the GitHub watcher was refused twice."
    order = [lines.index(h) for h in ("Needs you (reply in this thread)", "Done by code (no action needed)", "Learned")]
    assert order == sorted(order)
    assert "reply: post R12 / drop R12: why" in text
    assert "#P3 verification rules add: \"Read the dates.\"" in text and "reply: apply P3 / reject P3: why" in text
    assert "#P4 code change (urgent) for judge" in text and "it becomes an engineer task" in text
    assert "#P5 revert of #P2 proposed: holdout recall 90 to 70" in text
    assert "#T4 PR 72" in text and "reply: merge PR 72 on GitHub / close T4: why" in text
    assert "reply: keep G7 / drop G7: why" in text
    assert "4 cards checked: 2 verified, 1 held, 1 dropped" in text
    assert "#R9 Grace Hopper (a duplicate of a posted move)" in text
    assert "3 data issues: 2 fixed, 1 needed nothing" in text and "#I8 other person:1" in text
    assert "#T2 merged" in text and "1 proposal(s) expired" in text
    assert "reads since yesterday: github 12 (2 failed); the biggest failure: github, 2x: HTTP 403" in text
    assert "Cards held for a stale profile read (3x)" in text and "judge wrong on V12 (obvious): a flag alone" in text
    assert "editor wrong" not in text and "needs the owner: The GitHub token is refused" in text
    assert "Spend today: $0.75 (judge $0.50, daily review $0.25)" in text and "daily review $0.25 of $2.00" in text
    assert lines[-1].startswith("How to reply in this thread: post R<n> · drop R<n>: why")
    needs = text.split("Needs you (reply in this thread)\n")[1].split("\n\n")[0].splitlines()
    assert len(needs) == 6 and all(line.startswith("• #") for line in needs)
    assert held_index(lines) < proposal_index(lines)


def held_index(lines: list[str]) -> int:
    return next(i for i, line in enumerate(lines) if line.startswith("• #R"))


def proposal_index(lines: list[str]) -> int:
    return next(i for i, line in enumerate(lines) if line.startswith("• #P"))


def test_at_most_ten_needs_you_lines() -> None:
    text = note.render(packet(held=[held(n) for n in range(1, 16)]))
    needs = text.split("Needs you (reply in this thread)\n")[1].split("\n\n")[0].splitlines()
    assert len(needs) == 10
    assert needs[-1] == "• and 6 more waiting: ask the desk what waits for you"
    assert sum(1 for line in needs if line.startswith("• #R")) == 9


def test_ten_items_fit_without_an_overflow_line() -> None:
    text = note.render(packet(held=[held(n) for n in range(1, 11)]))
    needs = text.split("Needs you (reply in this thread)\n")[1].split("\n\n")[0].splitlines()
    assert len(needs) == 10 and "more waiting" not in text


def test_without_an_answer_code_says_why() -> None:
    text = note.render(packet(), None, outcome={"error": "the model did not answer"})
    assert "Why this note: The maintainer did not answer today (the model did not answer); nothing waits for a person." in text
    assert "• nothing today" in text and "Learned" not in text
    quiet = note.render({"day": "2026-11-23"}, None, outcome={"quiet": True})
    assert "a quiet day" in quiet and "• a quiet day" in quiet


def test_post_note_once_a_day_keeps_the_post_id_and_the_ids(store: Any, config: Any, posts: Any) -> None:
    person_id, _ = store.add_person("Ada Lovelace")
    source_id = store.ensure_source(person_id, "fake", "https://ada.example", cadence_hours=24)
    observation = store.record_check(store.source(source_id), status="ok", content={}, changes=[{"label": "bio changed"}])
    verdict_id = store.save_verdict(observation, person_id, {"signal": "left", "summary": "left", "confidence": 0.9})
    review = store.open_review(store.verdict(verdict_id))
    store.set_review(int(review["id"]), state="held", note="no dated post")
    pid = store.add_proposal("RULES", skill="verification", value={"text": "Read the dates."}, evidence=["R1", "R2"])
    posted = note.post_note(store, config, {}, packet={"day": util.local_day()})
    assert posted["posted"] == "ts-1" and len(posts) == 1
    assert f"#R{review['id']}" in posts[0] and f"#P{pid}" in posts[0]
    assert store.get_setting(f"{note.NOTE_KEY}{util.local_day()}") == "ts-1"
    assert store.get_setting(f"{note.IDS_KEY}ts-1") == {"R": [int(review["id"])], "P": [pid]}
    assert store.proposal(pid)["thread"] == "ts-1"
    again = note.post_note(store, config, {}, packet={"day": util.local_day()})
    assert again["already"] == "ts-1" and len(posts) == 1
    assert note.post_note(store, config, {}, packet={"day": util.local_day()}, force=True)["posted"] == "ts-2"
    assert not store.setting_history(f"{note.NOTE_KEY}{util.local_day()}")
    from tailwatch.desk import chat, ids

    root = chat.root_of(store.thread_lines(chat.thread_key(config.delivery.slack.desk_channel, "ts-1")))
    found = {k: v for k, v in ids.parse(root).items() if k != ids.BARE}
    assert found == {"R": [int(review["id"])], "P": [pid]}  # the how-to line adds no ids of its own


def test_post_note_gathers_the_packet_when_none_is_given(store: Any, config: Any, posts: Any) -> None:
    posted = note.post_note(store, config, {})
    assert posted["posted"] == "ts-1" and "*desk note ·" in posts[0] and "Spend today: $0.00." in posts[0]
