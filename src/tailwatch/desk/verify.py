"""The verification job and a person's decisions on cards.

``verify_cards`` runs every tick and once more before the digest, under the ``verify`` lock:

1. opens a review (``#R``) for every card the next digest could list (``digest.select`` with
   ``verified_only=False``), and no others;
2. closes as ``superseded`` the pending and fixing reviews whose card the digest would no
   longer list, and any review whose card can never post (judged again, posted, the person no
   longer tracked); a held card stays for a person;
3. verifies a trusted report (``judge.trusted_kind``) by code once the fetch-free floors pass:
   the report is taken as true, but a second card for a posted move is dropped and a link that
   belongs to someone else holds it;
4. runs the editor on at most ``PER_TICK`` pending cards, inside the per-card and daily budgets;
5. writes the outcome: ``verified`` (with a repair or a landing code may apply), ``fixing``
   (issues for the custodian), ``held`` (for a person) or ``dropped`` (a desk drop: a none
   verdict the judge remembers).

``decide`` carries out a person's decision; ``replay``, ``label`` and ``score`` measure the
editor on cards already posted.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from tailwatch import util
from tailwatch.agent import loop
from tailwatch.desk import editor, floors
from tailwatch.pipeline import digest, judge, packs
from tailwatch.store import LockBusy

LOCK = "verify"
LOCK_TTL_S = 1800
PER_TICK = 10
MAX_ROUNDS = 3
MAX_ISSUES = 5
LIVE = ("pending", "fixing", "held")
ACTIONS = ("post", "post_now", "drop", "fix", "hold")
TRUSTED_NOTE = "trusted report: {feed} reported it; taken as true, not checked by the editor"

ToolkitFactory = Callable[..., Any]


class _NoTools:
    """Stands in when the enrichment toolkit is not installed: the editor still has its store reads."""

    EVIDENCE: frozenset[str] = frozenset()

    def __init__(self, budget: loop.Budget) -> None:
        self.budget, self.texts, self.urls, self.usd = budget, {}, set(), 0.0

    def specs(self, _names: Any) -> list[dict[str, Any]]:
        return []

    def call(self, name: str, _args: Mapping[str, Any]) -> Any:
        return {"error": f"{name} is not available"}


def default_toolkit(store: Any, config: Any, env: Mapping[str, str], *, budget: loop.Budget) -> Any:
    try:
        from tailwatch.enrich.tools import Toolkit
    except ImportError:
        return _NoTools(budget)
    return Toolkit(store, config, env, budget=budget)


def _pack(config: Any) -> packs.Pack:
    return packs.load(config.signals)


def _say(review_id: int) -> str:
    return f"#R{review_id}"


# -- the queue ---------------------------------------------------------------------------------


def can_post(store: Any, review: Mapping[str, Any]) -> str:
    """'' when the card may still post, else why not."""
    verdict_id = int(review["verdict_id"])
    row = store.card_row(verdict_id)
    if row is None:
        return "the verdict is gone"
    if not store.is_current(verdict_id):
        return "the check was judged again"
    if row["signal"] == "none" or not row["eligible"]:
        return "the verdict is no longer a signal"
    if row["person_status"] != "tracked":
        return "the person is no longer tracked"
    if store.posted_on(verdict_id):
        return "a digest carried it already"
    return ""


def queue(store: Any, config: Any) -> list[dict[str, Any]]:
    """The cards the next digest could list (one per person), first signal first."""
    pack = _pack(config)
    rows = digest.select(store, config, pack, verified_only=False)["rows"]
    return sorted(rows, key=lambda r: (pack.order(r["signal"]), r["observed_at"], r["id"]))


def expire(store: Any, listed: set[int]) -> int:
    """Close reviews whose card the digest would no longer list (pending, fixing) or that can
    never post (held too)."""
    closed = 0
    for review in store.live_reviews(LIVE):
        why = can_post(store, review)
        if not why and (review["state"] == "held" or int(review["verdict_id"]) in listed):
            continue
        why = why or "the digest would no longer list it (older than the window, or another card speaks for the person)"
        if store.set_review(int(review["id"]), state="superseded", note=why, expect=review["state"]):
            closed += 1
    return closed


# -- outcomes ----------------------------------------------------------------------------------


def _desk_drop(store: Any, review: Mapping[str, Any], why: str, *, by: str = "editor") -> None:
    """A drop by the desk is a person's no: a none verdict on the check (the judge reads it in
    prior_alerts), the review dropped."""
    store.human_verdict(int(review["observation_id"]), int(review["person_id"]), "none", reason=f"dropped by the desk: {why}"[:2000],
                        by=by, judged_by="desk")
    store.set_review(int(review["id"]), state="dropped", note=why)


def _open_issues(store: Any, review: Mapping[str, Any], issues: list[Mapping[str, Any]], *, found_by: str = "editor") -> list[int]:
    opened = []
    for issue in issues[:MAX_ISSUES]:
        try:
            issue_id, _new = store.open_issue(str(issue.get("kind") or "other"), str(issue.get("subject") or f"review:{review['id']}"),
                                              playbook=str(issue.get("playbook") or ""), args=dict(issue.get("args") or {}),
                                              found_by=found_by, note=str(issue.get("detail") or ""), review_id=int(review["id"]))
        except RuntimeError:  # a flood of open issues: the card holds instead
            continue
        opened.append(issue_id)
    return opened


def record_landing(store: Any, card: Mapping[str, Any], landed: Mapping[str, str], review_id: int) -> dict[str, Any]:
    """A departure whose latest read shows a newer current role elsewhere: the roster moves
    there, and a joined note (a none verdict flagged ``landed``) goes on the read that shows it
    when that read is not the card's own check (a verdict there would replace the card)."""
    out: dict[str, Any] = dict(landed)
    person_id = int(card["person_id"])
    note = (f"joined {landed['organization']}" + (f" as {landed['title']}" if landed.get("title") else "")
            + (f" since {landed['since']}" if landed.get("since") else "") + f", found while verifying {_say(review_id)}")
    read_id = int((card.get("latest") or {}).get("id") or 0)
    current = store.current_verdict(read_id) if read_id else None
    if read_id and read_id != int(card["observation_id"]) and (current is None or current["signal"] == "none"):
        out["verdict_id"] = store.save_verdict(read_id, person_id, {
            "signal": "none", "confidence": 1.0, "summary": util.clip(note, 120), "reason": note, "flags": ["landed"],
            "now": {"organization": landed["organization"], "title": landed.get("title") or "", "since": landed.get("since") or ""},
            "eligible": False}, judged_by="desk", by_whom="editor")
    store.update_person(person_id, organization=landed["organization"], title=landed.get("title") or "",
                        since=landed.get("since") or "", by="editor")
    out["note"] = note
    return out


def apply(store: Any, config: Any, review: Mapping[str, Any], card: Mapping[str, Any], result: Mapping[str, Any]) -> str:
    """Write the editor's (lowered) verdict as the review's state. Returns the new state."""
    review_id = int(review["id"])
    answer = {k: v for k, v in result.items() if k != "transcript"}
    answer["transcript"] = list(result.get("transcript") or [])
    answer["card"] = editor.seed(card)
    rounds = int(review.get("rounds") or 0) + 1
    verdict = result["verdict"]
    if verdict == "drop":
        store.set_review(review_id, answer=answer, floors=result["floors"], rounds=rounds)
        _desk_drop(store, review, f"{result.get('drop_reason') or 'dropped'}: {util.clip(result.get('note'), 300)}")
        return "dropped"
    if verdict == "confirm":
        if result.get("repair"):
            repair = result["repair"]
            answer["repair"] = {**repair, "was": {"organization": card.get("organization"), "title": card.get("title")}}
            answer["context"] = f"{repair['title']} at {repair['organization']}" if repair.get("title") else repair["organization"]
        if result.get("landed"):
            answer["landed"] = record_landing(store, card, result["landed"], review_id)
        store.set_review(review_id, state="verified", answer=answer, floors=result["floors"], rounds=rounds,
                         note=util.clip(result.get("note"), 500))
        return "verified"
    if verdict == "fix" and rounds < MAX_ROUNDS:
        answer["issue_ids"] = _open_issues(store, review, list(result.get("issues") or []))
        if answer["issue_ids"]:
            store.set_review(review_id, state="fixing", answer=answer, floors=result["floors"], rounds=rounds,
                             note=util.clip(result.get("reason"), 500))
            return "fixing"
    note = result.get("reason") or result.get("note") or "the editor asked for a person"
    if verdict == "fix":
        note = f"held after {rounds} round(s): {note}"
    store.set_review(review_id, state="held", answer=answer, floors=result["floors"], rounds=rounds, note=util.clip(note, 500))
    return "held"


def verify_trusted(store: Any, config: Any, review: Mapping[str, Any], card: Mapping[str, Any]) -> str:
    """A trusted report: the news is true, but a second card for a posted move is dropped, and
    a link that belongs to someone else holds it. No model, no spend."""
    results = floors.run_fetch_free(card, None, floor=0.0)
    results.pop("confidence", None)
    feed = str(card.get("kind") or "").removeprefix("feed:")
    answer = {"verdict": "confirm", "editor_verdict": "trusted", "quotes": [], "issues": [], "failed": floors.failed(results),
              "note": TRUSTED_NOTE.format(feed=feed), "cost_usd": 0.0, "card": editor.seed(card), "floors": results}
    if "not_duplicate" in answer["failed"]:
        answer["verdict"] = "drop"
        store.set_review(int(review["id"]), answer=answer, floors=results)
        _desk_drop(store, review, f"duplicate: {results['not_duplicate']['detail']}")
        return "dropped"
    if answer["failed"]:
        answer["verdict"] = "hold"
        why = "; ".join(f"{n}: {results[n]['detail']}" for n in answer["failed"])
        store.set_review(int(review["id"]), state="held", answer=answer, floors=results, note=f"trusted report stopped by code: {why}")
        return "held"
    store.set_review(int(review["id"]), state="verified", answer=answer, floors=results, note=answer["note"])
    return "verified"


def _daily_left(store: Any, config: Any) -> float:
    return float(config.budgets.editor_daily) - store.spend_today(("verify_card",), zone=config.schedule.timezone)


def check_one(store: Any, config: Any, env: Mapping[str, str], review: Mapping[str, Any], *,
              toolkit_factory: ToolkitFactory | None = None) -> dict[str, Any]:
    """The editor (or code, for a trusted report) on one review, and its outcome written."""
    card = editor.card_for(store, config, review)
    if card["trusted"]:
        return {"review_id": review["id"], "state": verify_trusted(store, config, review, card), "trusted": True, "cost_usd": 0.0}
    budget = loop.Budget(float(config.budgets.editor_per_card))
    toolkit = (toolkit_factory or default_toolkit)(store, config, env, budget=budget)
    try:
        result = editor.edit(store, config, env, review, toolkit=toolkit, card=card)
    except editor.EditError as error:
        rounds = int(review.get("rounds") or 0) + 1
        state = "held" if rounds >= MAX_ROUNDS else review["state"]
        store.set_review(int(review["id"]), state=state, rounds=rounds, note=f"the editor failed: {util.clip(str(error), 300)}")
        return {"review_id": review["id"], "state": state, "error": str(error)[:300],
                "cost_usd": float(getattr(toolkit, "usd", 0.0) or 0.0)}
    state = apply(store, config, review, card, result)
    return {"review_id": review["id"], "state": state, "verdict": result["verdict"], "editor_verdict": result["editor_verdict"],
            "failed": result["failed"], "cost_usd": result["cost_usd"]}


def verify_cards(store: Any, config: Any, env: Mapping[str, str], *, toolkit_factory: ToolkitFactory | None = None,
                 limit: int = PER_TICK, log: Any = None) -> dict[str, Any]:
    """The job (every tick, and before the digest). Returns counts."""
    counts: dict[str, Any] = {"opened": 0, "checked": 0, "trusted": 0, "verified": 0, "fixing": 0, "held": 0, "dropped": 0,
                              "failed": 0, "superseded": 0, "spent_usd": 0.0, "stopped": ""}
    if store.get_setting("DESK:PAUSED", False):
        counts["stopped"] = "the desk is paused"
        return counts
    try:
        with store.lock(LOCK, ttl_s=LOCK_TTL_S):
            rows = queue(store, config)
            reviews = []
            for row in rows:
                existed = store.review_for(int(row["id"])) is not None
                reviews.append(store.open_review(row))
                counts["opened"] += 0 if existed else 1
            counts["superseded"] = expire(store, {int(r["id"]) for r in rows})
            for review in reviews:
                review = store.review(int(review["id"]))
                if review is None or review["state"] != "pending":
                    continue
                trusted = judge.trusted_kind(str((store.card_row(int(review["verdict_id"])) or {}).get("kind") or ""), config)
                if not trusted:
                    if counts["checked"] >= limit:
                        counts["stopped"] = f"{limit} cards checked this tick; the rest wait for the next"
                        break
                    if _daily_left(store, config) <= 0:
                        counts["stopped"] = f"the editor's daily budget (${config.budgets.editor_daily:.2f}) is spent"
                        _cap_notice(store, config, env)
                        break
                result = check_one(store, config, env, review, toolkit_factory=toolkit_factory)
                counts["trusted" if result.get("trusted") else "checked"] += 1
                counts["spent_usd"] = round(counts["spent_usd"] + float(result.get("cost_usd") or 0), 6)
                if result.get("error"):
                    counts["failed"] += 1
                elif result["state"] in ("verified", "fixing", "held", "dropped"):
                    counts[result["state"]] += 1
    except LockBusy:
        counts["stopped"] = "another verification is running"
    if log:
        log(f"verify: {counts}")
    return counts


def _cap_notice(store: Any, config: Any, env: Mapping[str, str]) -> None:
    try:
        from tailwatch.desk import notices

        notices.say_once(store, config, env, "verify:cap", f"The editor's daily budget (${config.budgets.editor_daily:.2f}) "
                         "is spent; the remaining cards wait for tomorrow (budgets.editor_daily raises it).", tag="card verification")
    except Exception:
        pass


def verify_one(store: Any, config: Any, env: Mapping[str, str], review_id: int, *,
               toolkit_factory: ToolkitFactory | None = None) -> dict[str, Any]:
    """The editor again on one review, on request (pending, held or fixing)."""
    review = store.review(review_id)
    if review is None:
        return {"error": f"no card {_say(review_id)}"}
    if review["state"] not in LIVE:
        return {"error": f"{_say(review_id)} is {review['state']}; only pending, held or fixing cards are checked again"}
    why = can_post(store, review)
    if why:
        return {"error": f"{_say(review_id)} cannot post any more: {why}"}
    if _daily_left(store, config) <= 0:
        return {"error": f"the editor's daily budget (${config.budgets.editor_daily:.2f}) is spent"}
    try:
        with store.lock(LOCK, ttl_s=LOCK_TTL_S, wait_s=120):
            store.set_review(review_id, state="pending")
            return check_one(store, config, env, store.review(review_id), toolkit_factory=toolkit_factory)
    except LockBusy:
        return {"error": "the verification job is running; try again in a minute"}


def requeue(store: Any, review_id: int, *, note: str = "") -> bool:
    """After the custodian's fix: a fixing card goes back to the editor (held after MAX_ROUNDS)."""
    review = store.review(review_id)
    if review is None or review["state"] != "fixing":
        return False
    if int(review.get("rounds") or 0) >= MAX_ROUNDS:
        return store.set_review(review_id, state="held", note=f"held after {review['rounds']} rounds: {note}"[:500], expect="fixing")
    return store.set_review(review_id, state="pending", note=note or "fixed; checked again", expect="fixing")


def post_time_check(store: Any, config: Any, verdict_id: int) -> bool:
    """The gate at post time: a verified card still passes the floors that need no fetch. A
    card a person released passes; a failure puts the review back to pending."""
    review = store.review_for(verdict_id)
    if review is None or review["state"] != "verified":
        return False
    if (review.get("decision") or {}).get("released_by") or (review.get("answer") or {}).get("editor_verdict") == "trusted":
        return True
    card = editor.card_for(store, config, review)
    results = floors.run_fetch_free(card, review.get("answer") or {}, floor=floors.confidence_floor(store))
    bad = floors.failed(results)
    if bad:
        store.set_review(int(review["id"]), state="pending", note=f"stopped at post time: {', '.join(bad)}", expect="verified")
        return False
    return True


# -- a person's decisions ----------------------------------------------------------------------

FIX_PLAYBOOKS = (("wrong person", "mark_wrong_person"), ("someone else", "mark_wrong_person"), ("link", "fill_links"),
                 ("title", "fix_card_fields"), ("organization", "fix_card_fields"), ("company", "fix_card_fields"),
                 ("role", "fix_card_fields"))


def fix_playbook(what: str) -> str:
    lowered = what.lower()
    return next((p for word, p in FIX_PLAYBOOKS if word in lowered), "recheck_person")


def post_now(store: Any, config: Any, env: Mapping[str, str], verdict_id: int, *, by: str,
             sinks: list[Any] | None = None) -> dict[str, Any]:
    """Deliver one card at once through the digest's sinks, with the digest's receipt (so the
    next digest does not carry it again). In the shadow trial it goes to the shadow channel
    without a receipt."""
    from tailwatch.delivery.base import DeliveryRejected
    from tailwatch.delivery.slack import sinks_for

    pack = _pack(config)
    row = store.card_row(verdict_id)
    if row is None:
        return {"error": f"no verdict {verdict_id}"}
    shadow = config.switches.verify == "shadow" and bool(config.delivery.slack.shadow_channel)
    sinks = sinks if sinks is not None else sinks_for(config, env, shadow=shadow)
    review = store.review_for(verdict_id) or {}
    extra = (review.get("answer") or {})
    if extra.get("landed"):
        row = {**row, "landed": extra["landed"]}
    if extra.get("context"):
        row = {**row, "context": extra["context"]}
    signal = pack.signal(row["signal"])
    payload = {"title": f"{config.name} · posted now", "day": f"{util.local_day(config.schedule.timezone)}-now-{verdict_id}",
               "sections": [{"signal": row["signal"], "heading": signal.heading if signal else row["signal"],
                             "mark": signal.mark if signal else "•", "cards": [digest.card(row)]}],
               "notes": [], "verdicts": [{"id": verdict_id, "person_id": row["person_id"], "move_key": digest.move_key(row, pack)}]}
    delivered, refused = [], []
    for sink in sinks:
        try:
            delivered.append(sink.deliver(payload, f"now:{verdict_id}"))
        except DeliveryRejected as error:
            refused.append(str(error))
    if refused and not delivered:
        return {"error": "; ".join(refused)}
    if not shadow:
        store.add_post(verdict_id, int(row["person_id"]), channel=digest.CHANNEL, digest_id=None,
                       move_key=digest.move_key(row, pack), by=by or "desk")
    return {"delivered": [d for d in delivered if d], "shadow": shadow, "errors": refused}


def decide(store: Any, config: Any, env: Mapping[str, str], review_id: int, action: str, *, note: str = "", by: str,
           sinks: list[Any] | None = None) -> dict[str, Any]:
    """A person's decision on a card: post (the next digest), post_now (at once), drop (needs
    why), fix (an issue for the custodian), hold. The caller has checked that ``by`` may write."""
    review = store.review(review_id)
    if review is None:
        return {"error": f"no card {_say(review_id)}"}
    if action not in ACTIONS:
        return {"error": f"the decision is one of {', '.join(ACTIONS)}"}
    if review["state"] in ("dropped", "superseded"):
        return {"error": f"{_say(review_id)} is already {review['state']}"}
    note = note.strip()
    if action in ("drop", "fix") and not note:
        return {"error": f"{action} needs a reason: '{action} {_say(review_id)}: why'"}
    why = can_post(store, review)
    if action in ("post", "post_now") and why:
        return {"error": f"{_say(review_id)} cannot be posted: {why}"}
    decision = {**(review.get("decision") or {}), "action": action, "by": by, "note": note, "at": util.iso()}
    out: dict[str, Any] = {"review_id": review_id, "id": _say(review_id), "action": action}
    if action in ("post", "post_now"):
        if action == "post_now" and store.get_setting("DESK:PAUSED", False):
            return {"error": "the desk is paused; resume it before posting"}
        decision["released_by"] = by
        if not store.set_review(review_id, state="verified", decision=decision, expect=review["state"]):
            return {"error": f"{_say(review_id)} changed meanwhile; look again"}
        store.add_label(int(review["verdict_id"]), "right", note=note or action, by=by)
        out["state"] = "verified"
        if action == "post_now":
            out["posted"] = post_now(store, config, env, int(review["verdict_id"]), by=by, sinks=sinks)
            if out["posted"].get("error"):
                out["error"] = out["posted"]["error"]
        out["said"] = f"{_say(review_id)} " + ("posted now." if action == "post_now" else "released; it goes out with the next digest.")
        return out
    if action == "drop":
        card_row = store.card_row(int(review["verdict_id"])) or {}
        if any(w in note.lower() for w in ("wrong person", "someone else")) and card_row and not judge.is_feed(str(card_row["kind"])):
            store.set_source_status(int(card_row["source_id"]), "wrong_person", f"dropped by {by}: {note}")
            _open_issues(store, review, [{"kind": "wrong_link", "subject": f"source:{card_row['source_id']}", "playbook": "fill_links",
                                          "args": {"person_id": review["person_id"], "kind": card_row["kind"]},
                                          "detail": f"{by} said the {card_row['kind']} source is someone else's"}], found_by="desk")
            out["source"] = {"id": card_row["source_id"], "status": "wrong_person"}
        store.human_verdict(int(review["observation_id"]), int(review["person_id"]), "none", reason=f"{by} dropped it: {note}"[:2000],
                            by=by, judged_by="human")
        store.set_review(review_id, state="dropped", decision=decision, note=f"dropped by {by}: {note}")
        store.add_label(int(review["verdict_id"]), "wrong", note=note, by=by)
        out.update(state="dropped", said=f"{_say(review_id)} dropped ({note}); it will not be posted.")
        return out
    if action == "fix":
        playbook = fix_playbook(note)
        issues = _open_issues(store, review, [{"kind": "human_fix", "subject": f"review:{review_id}", "playbook": playbook,
                                               "args": {"review_id": review_id, "person_id": review["person_id"], "what": note},
                                               "detail": note}], found_by="desk")
        store.set_review(review_id, state="fixing", decision=decision, note=f"fix asked by {by}: {note}")
        out.update(state="fixing", issues=issues, playbook=playbook,
                   said=f"{_say(review_id)}: the fix goes to the custodian ({playbook}); the editor checks the card again after it.")
        return out
    store.set_review(review_id, state="held", decision=decision, note=f"held by {by}" + (f": {note}" if note else ""))
    out.update(state="held", said=f"{_say(review_id)} held.")
    return out


# -- measuring the editor ----------------------------------------------------------------------


def replay(store: Any, config: Any, env: Mapping[str, str], days: int = 30, *, toolkit_factory: ToolkitFactory | None = None,
           limit: int = 100) -> dict[str, Any]:
    """The editor over the cards a digest carried in the last ``days``: what it would have said.
    A card with no review gets one (closed as superseded, since it was posted) holding the
    answer, so labels can be scored against it; an existing review is left as it is."""
    out: dict[str, Any] = {"days": days, "cards": [], "verdicts": {"confirm": 0, "fix": 0, "hold": 0, "drop": 0, "error": 0},
                           "cost_usd": 0.0}
    for row in store.posted_card_rows(since=util.ago(days=days), limit=limit):
        if _daily_left(store, config) <= 0:
            out["stopped"] = "the editor's daily budget is spent"
            break
        existing = store.review_for(int(row["id"]))
        review = existing or store.open_review(row)
        budget = loop.Budget(float(config.budgets.editor_per_card))
        toolkit = (toolkit_factory or default_toolkit)(store, config, env, budget=budget)
        item: dict[str, Any] = {"review_id": review["id"], "verdict_id": row["id"], "name": row["person_name"], "signal": row["signal"]}
        try:
            card = editor.card_for(store, config, review)
            card["posted"] = [p for p in card["posted"] if int(p["verdict_id"]) != int(row["id"])]
            result = editor.edit(store, config, env, review, toolkit=toolkit, card=card)
        except editor.EditError as error:
            item["error"] = str(error)[:300]
            out["verdicts"]["error"] += 1
            out["cards"].append(item)
            continue
        item.update({k: result[k] for k in ("verdict", "editor_verdict", "failed", "note", "cost_usd")})
        out["verdicts"][result["verdict"]] += 1
        out["cost_usd"] = round(out["cost_usd"] + result["cost_usd"], 6)
        if existing is None:
            answer = {k: v for k, v in result.items() if k != "transcript"}
            store.set_review(int(review["id"]), state="superseded", answer={**answer, "replay": True, "card": editor.seed(card)},
                             floors=result["floors"], note="replayed over a posted card")
        out["cards"].append(item)
    return out


def label(store: Any, verdict_id: int, label: str, holdout: bool = False, by: str = "") -> dict[str, Any]:
    """Record whether a card was right; ``holdout`` labels form the fixed scoring set."""
    if store.verdict(verdict_id) is None:
        return {"error": f"no verdict {verdict_id}"}
    try:
        label_id = store.add_label(verdict_id, label, holdout=holdout, by=by)
    except ValueError as error:
        return {"error": str(error)}
    return {"label_id": label_id, "verdict_id": verdict_id, "label": label, "holdout": holdout}


def score(store: Any, *, holdout_only: bool = False) -> dict[str, Any]:
    """Recall (confirmed among the cards labelled right), false pass (confirmed among those
    labelled wrong) and hold rate (not confirmed, among all), over labelled cards the editor
    answered. The newest label of a card counts."""
    newest: dict[int, dict[str, Any]] = {}
    for item in store.labels(holdout_only=holdout_only):
        if item["label"] in ("right", "wrong") and (item.get("answer") or {}).get("verdict"):
            newest[int(item["verdict_id"])] = item
    right = [i for i in newest.values() if i["label"] == "right"]
    wrong = [i for i in newest.values() if i["label"] == "wrong"]

    def confirmed(items: list[dict[str, Any]]) -> int:
        return sum(1 for i in items if i["answer"]["verdict"] == "confirm")

    n = len(newest)
    return {"recall": round(confirmed(right) / len(right), 3) if right else None,
            "false_pass": round(confirmed(wrong) / len(wrong), 3) if wrong else None,
            "hold_rate": round(sum(1 for i in newest.values() if i["answer"]["verdict"] != "confirm") / n, 3) if n else None,
            "n": n}
