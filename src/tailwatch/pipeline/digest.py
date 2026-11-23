"""The daily digest: the only message tailwatch sends on its own.

Who is in it:
- the day's cards: current signal verdicts seen within ``digest.window_hours``, of people still
  tracked, that no digest carried;
- one card per person (the pack's first signal first, then the newest);
- one card per move: a person a digest carried within ``digest.repost_days`` is listed again
  only for a different move (a different signal, or another destination; ``same_as`` a posted
  verdict is the same move);
- a signal past its ``stale_after_days`` waits no longer;
- a report whose destination is the organization on record ("left X, now at X") is no move;
- with ``switches.verify: on``, only cards the editor verified (and a person's "post");
- ranked by the ranker, at most ``digest.cap`` people, the rest wait for tomorrow.

Delivery is durable: the day's digest row goes prepared → sending (saved before any send) →
sent. A digest found in ``sending`` (a crash mid-send) is first looked up by its message key in
every sink that can look, and only resent where it is missing. An explicit refusal puts it
back to prepared. The receipts (``posts``) are what "one card per move" reads tomorrow.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from tailwatch import util
from tailwatch.delivery.base import DeliveryRejected, render
from tailwatch.pipeline import packs, ranker

CHANNEL = "digest"
SHADOW = "shadow"
UNLISTED_CAP = 20
LINK_LABELS = {"linkedin": "LinkedIn", "github": "GitHub", "homepage": "Website", "x": "X", "bluesky": "Bluesky",
               "arxiv": "arXiv", "semantic_scholar": "Semantic Scholar", "scholar": "Scholar", "rss": "Blog"}


def move_key(row: Mapping[str, Any], pack: packs.Pack) -> str:
    if pack.move_key == "item":
        return f"{row['signal']}|obs:{row['observation_id']}"
    destination = str((row.get("now") or {}).get("organization") or "").strip().lower()
    return f"{row['signal']}|{destination}"


def is_stale(row: Mapping[str, Any], pack: packs.Pack) -> bool:
    signal = pack.signal(row["signal"])
    if signal is None or not signal.stale_after_days:
        return False
    seen = util.parse(row["observed_at"])
    return seen is not None and seen < util.now() - timedelta(days=signal.stale_after_days)


def no_move(row: Mapping[str, Any], pack: packs.Pack) -> bool:
    if pack.move_key != "destination":
        return False
    before = str((row.get("snapshot") or {}).get("organization") or "").strip().lower()
    after = str((row.get("now") or {}).get("organization") or "").strip().lower()
    return bool(before) and before == after and row["signal"] == pack.names[0]


def reposted(store: Any, row: Mapping[str, Any], pack: packs.Pack, days: int) -> bool:
    if days <= 0:
        return False
    posted = store.posted_moves(row["person_id"], since=util.ago(days=days))
    keys = {p["move_key"] for p in posted}
    ids = {p["verdict_id"] for p in posted}
    return move_key(row, pack) in keys or int(row.get("same_as") or 0) in ids


def released(store: Any, verdict_id: int) -> bool:
    review = store.review_for(verdict_id)
    return bool(review) and review["state"] == "verified"


def person_released(store: Any, verdict_id: int) -> bool:
    review = store.review_for(verdict_id)
    return bool(review) and review["state"] == "verified" and bool((review.get("decision") or {}).get("released_by"))


def select(store: Any, config: Any, pack: packs.Pack, *, channel: str = CHANNEL, verified_only: bool = False) -> dict[str, Any]:
    window = float(config.digest.window_hours or 0)
    since = util.ago(hours=window) if window > 0 else "0000"
    rows = store.waiting_cards(since=since, channel=channel)
    if window > 0:  # a card a person released with "post" reaches the digest whatever its age
        rows += [r for r in store.waiting_cards(since="0000", channel=channel)
                 if r["observed_at"] < since and person_released(store, r["id"])]
    dropped: dict[str, int] = {}

    def drop(reason: str) -> None:
        dropped[reason] = dropped.get(reason, 0) + 1

    superseded = store.superseded_ids(since=util.ago(days=max(90, config.digest.repost_days)))
    kept = []
    for row in rows:
        if row["id"] in superseded:
            drop("superseded")
        elif is_stale(row, pack):
            drop("stale")
        elif no_move(row, pack):
            drop("no move")
        elif reposted(store, row, pack, config.digest.repost_days):
            drop("already posted")
        elif verified_only and not released(store, row["id"]):
            drop("waiting for the editor")
        else:
            kept.append(row)
    by_person: dict[int, dict[str, Any]] = {}
    for row in sorted(kept, key=lambda r: (pack.order(r["signal"]), r["observed_at"]), reverse=False):
        by_person.setdefault(row["person_id"], row)
    return {"rows": list(by_person.values()), "dropped": dropped, "considered": len(rows)}


def card(row: Mapping[str, Any]) -> dict[str, Any]:
    links = {LINK_LABELS.get(k, k): v for k, v in (row.get("person_links") or {}).items() if k != row["kind"]}
    context = row.get("context") or " ".join(x for x in (row.get("person_title"), "at" if row.get("person_title") and row.get("person_org") else "",
                                                          row.get("person_org")) if x)
    if row.get("landed"):
        landed = row["landed"]
        context = f"{context}; now at {landed.get('organization')}" + (f" since {landed['since']}" if landed.get("since") else "")
    return {"person_id": row["person_id"], "verdict_id": row["id"], "name": row["person_name"], "url": row.get("source_url") or "",
            "summary": row.get("summary") or row["signal"], "context": context, "why": row.get("why") or "", "links": links}


def coverage_notes(store: Any) -> list[str]:
    notes = []
    waiting = store.count_pending(older_than=util.ago(hours=2))
    if waiting:
        notes.append(f"{waiting} check(s) still waiting for the judge")
    parked = store.count_parked()
    if parked:
        notes.append(f"{parked} check(s) could not be judged (parked)")
    held = len(store.reviews(state="held", limit=200))
    if held:
        notes.append(f"{held} card(s) held for a person in the desk")
    return notes


def build(store: Any, config: Any, env: Mapping[str, str], *, channel: str = CHANNEL, verified_only: bool | None = None,
          rank: bool = True) -> dict[str, Any]:
    pack = packs.load(config.signals)
    if verified_only is None:
        verified_only = config.switches.verify == "on"
    if verified_only and store.get_setting("DESK:PAUSED", False):
        chosen = {"rows": [], "dropped": {"desk paused": 1}, "considered": 0}
    else:
        chosen = select(store, config, pack, channel=channel, verified_only=verified_only)
    rows, how = ranker.rank(store, config, env, chosen["rows"], pack) if rank else (ranker.fallback(chosen["rows"], pack, config), "fallback")
    listed, waiting = rows[: config.digest.cap], rows[config.digest.cap:]
    day = util.local_day(config.schedule.timezone)
    sections = []
    for signal in pack.signals:
        cards = [card(r) for r in listed if r["signal"] == signal.name]
        if cards:
            sections.append({"signal": signal.name, "heading": signal.heading, "mark": signal.mark, "cards": cards})
    unlisted = [{"text": util.clip(c["text"].replace("\n", " "), 90), "url": c["url"], "candidate_id": c["id"]}
                for c in store.candidates(states=("out_of_scope", "needs_review", "new", "identified"), limit=UNLISTED_CAP * 3)
                if not c.get("digested_at")][:UNLISTED_CAP]
    added = [c for c in store.candidates(states=("added",), limit=50) if not c.get("digested_at")]
    notes = coverage_notes(store)
    if waiting:
        notes.insert(0, f"{len(waiting)} more waiting, ranked again tomorrow")
    if added:
        notes.insert(0, f"Added from reports: {', '.join(util.clip((c['subject'] or {}).get('name') or c['url'], 40) for c in added[:8])}")
    date = util.local_time(config.schedule.timezone).strftime("%A %d %B")
    return {"title": f"{config.name} digest · {date}", "day": day, "sections": sections, "unlisted": unlisted,
            "notes": notes, "footer": f"Every alert: {config.delivery.site_url}" if config.delivery.site_url else "",
            "verdicts": [{"id": r["id"], "person_id": r["person_id"], "move_key": move_key(r, pack)} for r in listed],
            "candidates": [u["candidate_id"] for u in unlisted] + [c["id"] for c in added], "ranked_by": how,
            "dropped": chosen["dropped"], "quiet_line": "Nothing new since the last digest."}


def post(store: Any, config: Any, env: Mapping[str, str], *, sinks: list[Any] | None = None, channel: str = CHANNEL,
         verified_only: bool | None = None, console: Any = None) -> dict[str, Any]:
    """Build and deliver the day's digest once. Returns what happened."""
    from tailwatch.delivery.slack import sinks_for

    sinks = sinks if sinks is not None else sinks_for(config, env, console=console, shadow=channel == SHADOW)
    day = util.local_day(config.schedule.timezone)
    existing = store.digest(channel, day)
    if existing and existing["state"] == "sent":
        return {"state": "already sent", "day": day, "digest_id": existing["id"]}
    if existing and existing["state"] == "sending":
        payload = existing["payload"]
        missing = [s for s in sinks if not s.find(existing["message_key"])]
        record = existing
    else:
        payload = build(store, config, env, channel=channel, verified_only=verified_only)
        record = store.prepare_digest(channel, day, text=render(payload, "markdown"), payload=payload)
        missing = list(sinks)
    store.digest_state(record["id"], "sending")
    delivered, refused = [], []
    for sink in missing:
        try:
            delivered.append(sink.deliver(payload, record["message_key"]))
        except DeliveryRejected as error:
            refused.append(str(error))
    if refused and not delivered and missing:
        store.digest_state(record["id"], "prepared")
        return {"state": "refused", "day": day, "errors": refused}
    store.digest_state(record["id"], "sent", ts=",".join(d for d in delivered if d)[:500])
    for verdict in payload.get("verdicts") or []:
        store.add_post(verdict["id"], verdict["person_id"], channel=channel, digest_id=record["id"], move_key=verdict["move_key"])
    if channel == CHANNEL:
        for candidate_id in payload.get("candidates") or []:
            store.update_candidate(candidate_id, digested_at=util.iso())
    people = sum(len(s["cards"]) for s in payload.get("sections") or [])
    return {"state": "sent", "day": day, "people": people, "digest_id": record["id"], "errors": refused,
            "ranked_by": payload.get("ranked_by"), "dropped": payload.get("dropped")}


def post_morning(store: Any, config: Any, env: Mapping[str, str], **kwargs: Any) -> dict[str, Any]:
    """The scheduled digest: in shadow mode, the verified-only trial goes to the shadow channel
    first (its own receipts), then the real digest exactly as without the editor."""
    result: dict[str, Any] = {}
    if config.switches.verify == "shadow" and config.delivery.slack.shadow_channel and env.get("SLACK_BOT_TOKEN"):
        result["shadow"] = post(store, config, env, channel=SHADOW, verified_only=True)
    result["digest"] = post(store, config, env, **kwargs)
    return result
