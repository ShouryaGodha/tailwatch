"""The verification job, a person's decisions, the replay and the score."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from tailwatch import util
from tailwatch.desk import tools_verification, verify
from tailwatch.pipeline import digest
from test_verify_support import QUOTE, answer, card, factory, seed_of, spend


def _no_model(request: Any) -> Any:
    raise AssertionError("no model call expected")


def _by_name(answers: dict[str, dict[str, Any]]) -> Any:
    return lambda request: answers[seed_of(request)["name"]]


def test_confirm_verifies_and_the_gated_digest_lists_only_verified_cards(store: Any, config: Any, model: Any) -> None:
    card(store, "Ada Lovelace")
    card(store, "Grace Hopper")
    model(_by_name({"Ada Lovelace": answer(), "Grace Hopper": answer("hold", note="the page is ambiguous")}))
    counts = verify.verify_cards(store, config, {}, toolkit_factory=factory())
    assert counts["opened"] == 2 and counts["verified"] == 1 and counts["held"] == 1
    config.switches.verify = "on"
    built = digest.build(store, config, {}, rank=False)
    names = [c["name"] for s in built["sections"] for c in s["cards"]]
    assert names == ["Ada Lovelace"] and built["dropped"] == {"waiting for the editor": 1}
    assert any("held" in n for n in built["notes"])
    # a second tick checks nothing again
    model(_no_model)
    assert verify.verify_cards(store, config, {}, toolkit_factory=factory())["checked"] == 0


def test_a_trusted_report_is_verified_by_code(store: Any, config: Any, model: Any) -> None:
    made = card(store, kind="feed:trusted-news", url="https://news.example/item/1")
    model(_no_model)
    counts = verify.verify_cards(store, config, {}, toolkit_factory=factory())
    review = store.review_for(made["verdict_id"])
    assert counts["trusted"] == 1 and counts["checked"] == 0
    assert review["state"] == "verified" and review["answer"]["editor_verdict"] == "trusted"


def test_a_trusted_report_of_a_posted_move_is_dropped(store: Any, config: Any, model: Any) -> None:
    first = card(store)
    store.add_post(first["verdict_id"], first["person_id"], channel="digest", digest_id=None, move_key="left|difference labs")
    source = store.source(store.ensure_source(first["person_id"], "feed:trusted-news", "https://news.example/2", cadence_hours=24))
    observation = store.record_check(source, status="ok", content={"text": "Ada left"}, changes=[{"label": "new report"}])
    from conftest import verdict

    report = store.save_verdict(observation, first["person_id"],
                                verdict("left", now={"organization": "Difference Labs", "title": "", "since": ""}))
    review = store.open_review(store.verdict(report))
    model(_no_model)
    assert verify.check_one(store, config, {}, review)["state"] == "dropped"
    assert store.current_verdict(observation)["judged_by"] == "desk"


def test_landing_is_recorded_and_the_roster_moves(store: Any, config: Any, model: Any) -> None:
    positions = [{"company": "Analytical Engines", "title": "Researcher", "current": False, "start": "2020-01", "end": "2026-10"},
                 {"company": "Difference Labs", "title": "Founder", "current": True, "start": "2026-11"}]
    made = card(store)
    source = store.source(made["source_id"])
    later = store.record_check(source, status="ok", content={**{"bio": "Founder at Difference Labs"}, "positions": positions})
    model(lambda request: answer(now_at={"organization": "Difference Labs", "title": "Founder", "since": "2026-11"},
                                 quote="", issues=[]) | {"quotes": [{"url": made["url"], "text": "Founder at Difference Labs"}]})
    verify.verify_cards(store, config, {}, toolkit_factory=factory())
    review = store.review_for(made["verdict_id"])
    assert review["state"] == "verified"
    assert review["answer"]["landed"]["organization"] == "Difference Labs"
    person = store.person(made["person_id"])
    assert (person["organization"], person["title"], person["since"]) == ("Difference Labs", "Founder", "2026-11")
    note = store.current_verdict(later)
    assert note["signal"] == "none" and "landed" in note["flags"] and note["judged_by"] == "desk"
    assert store.is_current(made["verdict_id"])  # the card itself is untouched


def test_an_allowed_drop_is_a_desk_none_verdict(store: Any, config: Any, model: Any) -> None:
    made = card(store, signal="looking")
    model(lambda request: answer("drop", drop_reason="flag_only", quote=""))
    counts = verify.verify_cards(store, config, {}, toolkit_factory=factory())
    assert counts["dropped"] == 1
    current = store.current_verdict(made["observation_id"])
    assert current["signal"] == "none" and current["judged_by"] == "desk"
    assert store.review_for(made["verdict_id"])["state"] == "dropped"


def test_a_fix_opens_issues_for_the_custodian(store: Any, config: Any, model: Any) -> None:
    made = card(store)
    model(lambda request: answer("fix", issues=[{"kind": "missing_link", "detail": "no GitHub on record"}]))
    verify.verify_cards(store, config, {}, toolkit_factory=factory())
    review = store.review_for(made["verdict_id"])
    assert review["state"] == "fixing"
    issue = store.issue(review["answer"]["issue_ids"][0])
    assert issue["playbook"] == "fill_links" and issue["found_by"] == "editor" and issue["review_id"] == review["id"]
    assert verify.requeue(store, review["id"]) and store.review(review["id"])["state"] == "pending"


def test_decisions_post_post_now_drop_fix(store: Any, config: Any, model: Any) -> None:
    ids = {name: card(store, name) for name in ("Ada Lovelace", "Grace Hopper", "Hedy Lamarr", "Joan Clarke")}
    model(lambda request: answer("hold"))
    verify.verify_cards(store, config, {}, toolkit_factory=factory())
    review = {name: store.review_for(m["verdict_id"])["id"] for name, m in ids.items()}

    posted = verify.decide(store, config, {}, review["Ada Lovelace"], "post", by="U1")
    assert posted["state"] == "verified" and store.review(review["Ada Lovelace"])["decision"]["released_by"] == "U1"

    class Sink:
        name = "recorder"

        def __init__(self) -> None:
            self.seen: list[Any] = []

        def deliver(self, payload: Any, key: str) -> str:
            self.seen.append(payload)
            return key

    sink = Sink()
    now = verify.decide(store, config, {}, review["Grace Hopper"], "post_now", by="U1", sinks=[sink])
    assert now["posted"]["delivered"] == [f"now:{ids['Grace Hopper']['verdict_id']}"]
    assert sink.seen[0]["sections"][0]["cards"][0]["name"] == "Grace Hopper"
    assert store.posted_on(ids["Grace Hopper"]["verdict_id"])

    assert "error" in verify.decide(store, config, {}, review["Hedy Lamarr"], "drop", by="U1")
    dropped = verify.decide(store, config, {}, review["Hedy Lamarr"], "drop", note="a conference talk, not a move", by="U1")
    assert dropped["state"] == "dropped"
    current = store.current_verdict(ids["Hedy Lamarr"]["observation_id"])
    assert current["signal"] == "none" and current["judged_by"] == "human" and current["by_whom"] == "U1"
    recent = store.recent_events(ids["Hedy Lamarr"]["person_id"], since="0000")
    assert recent[0]["signal"] == "none"  # what the judge reads next time

    fixed = verify.decide(store, config, {}, review["Joan Clarke"], "fix", note="her title is Cryptanalyst", by="U1")
    assert fixed["state"] == "fixing" and fixed["playbook"] == "fix_card_fields"
    assert store.issue(fixed["issues"][0])["review_id"] == review["Joan Clarke"]

    config.switches.verify = "on"
    built = digest.build(store, config, {}, rank=False)
    assert [c["name"] for s in built["sections"] for c in s["cards"]] == ["Ada Lovelace"]
    assert "error" in verify.decide(store, config, {}, review["Hedy Lamarr"], "post", by="U1")


def test_reviews_that_can_no_longer_post_are_superseded(store: Any, config: Any, model: Any) -> None:
    from conftest import verdict

    rejudged, old = card(store, "Ada Lovelace"), card(store, "Grace Hopper")
    for made in (rejudged, old):
        store.open_review(store.verdict(made["verdict_id"]))
    store.save_verdict(rejudged["observation_id"], rejudged["person_id"], verdict("none"))  # judged again
    util.set_now(util.now() + timedelta(hours=config.digest.window_hours + 1))  # out of the window
    model(_no_model)
    counts = verify.verify_cards(store, config, {}, toolkit_factory=factory())
    assert counts["superseded"] == 2
    assert {store.review_for(m["verdict_id"])["state"] for m in (rejudged, old)} == {"superseded"}


def test_the_daily_cap_stops_the_editor(store: Any, config: Any, model: Any) -> None:
    made = card(store)
    spend(store, config.budgets.editor_daily + 1)
    model(_no_model)
    counts = verify.verify_cards(store, config, {}, toolkit_factory=factory())
    assert counts["checked"] == 0 and "daily budget" in counts["stopped"]
    assert store.review_for(made["verdict_id"])["state"] == "pending"


def test_the_tick_checks_at_most_ten_cards(store: Any, config: Any, model: Any) -> None:
    for i in range(12):
        card(store, f"Person{i} Example")
    model(lambda request: answer())
    counts = verify.verify_cards(store, config, {}, toolkit_factory=factory())
    assert counts["checked"] == verify.PER_TICK and counts["opened"] == 12


def test_an_editor_that_keeps_failing_holds_the_card(store: Any, config: Any, model: Any) -> None:
    made = card(store)
    model(lambda request: "not json")
    for _ in range(verify.MAX_ROUNDS):
        verify.verify_cards(store, config, {}, toolkit_factory=factory())
    review = store.review_for(made["verdict_id"])
    assert review["state"] == "held" and "failed" in review["note"]


def test_post_time_check_sends_a_duplicate_back(store: Any, config: Any, model: Any) -> None:
    made = card(store)
    model(lambda request: answer())
    verify.verify_cards(store, config, {}, toolkit_factory=factory())
    assert verify.post_time_check(store, config, made["verdict_id"])
    other = card(store, "Ada Lovelace B", url="https://other.example")
    store.add_post(other["verdict_id"], made["person_id"], channel="digest", digest_id=None, move_key="left|difference labs")
    assert not verify.post_time_check(store, config, made["verdict_id"])
    assert store.review_for(made["verdict_id"])["state"] == "pending"


def test_replay_label_and_score(store: Any, config: Any, model: Any) -> None:
    made = [card(store, n) for n in ("Ada Lovelace", "Grace Hopper", "Hedy Lamarr")]
    for m in made:
        store.add_post(m["verdict_id"], m["person_id"], channel="digest", digest_id=None, move_key=f"left|x{m['verdict_id']}")
    model(_by_name({"Ada Lovelace": answer(), "Grace Hopper": answer(), "Hedy Lamarr": answer("hold")}))
    replayed = verify.replay(store, config, {}, 30, toolkit_factory=factory())
    assert replayed["verdicts"]["confirm"] == 2 and replayed["verdicts"]["hold"] == 1
    assert verify.label(store, made[0]["verdict_id"], "right", True, "U1")["label"] == "right"
    verify.label(store, made[1]["verdict_id"], "wrong", True, "U1")
    verify.label(store, made[2]["verdict_id"], "right", False, "U1")
    assert "error" in verify.label(store, made[2]["verdict_id"], "maybe", False, "U1")
    assert verify.score(store) == {"recall": 0.5, "false_pass": 1.0, "hold_rate": 0.333, "n": 3}
    assert verify.score(store, holdout_only=True)["n"] == 2


def test_desk_tools(store: Any, config: Any, model: Any) -> None:
    from tailwatch.agent.registry import ToolContext, execute

    made = card(store)
    model(lambda request: answer("hold"))
    verify.verify_cards(store, config, {}, toolkit_factory=factory())
    review_id = store.review_for(made["verdict_id"])["id"]
    by_name = {t.name: t for t in tools_verification.TOOLS.tools}
    assert not any(t.writes for t in by_name.values()) and by_name["verify_now"].mutating
    ctx = ToolContext(store=store, config=config, user="local", extra={"toolkit_factory": factory()})
    assert execute(by_name["cards"], ctx, {})["cards"][0]["id"] == f"#R{review_id}"
    full = execute(by_name["card"], ctx, {"review_id": review_id})
    assert full["answer"]["quotes"][0]["text"] == QUOTE and "transcript" not in full["answer"]
    model(lambda request: answer())
    assert execute(by_name["verify_now"], ctx, {"review_id": review_id})["state"] == "verified"
