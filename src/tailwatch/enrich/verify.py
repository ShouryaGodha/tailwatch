"""Proof checks on the enricher's answer. They can only lower a verdict, never raise one.

The model reads evidence and says which profiles are the person's; code then checks that the
proof is real, not remembered or guessed:

1. the URL is one a tool returned (or the source itself gave), else **rejected**;
2. the quote is in the text a tool returned for ``quote_url`` (at most 40 words), else
   unverified;
3. the confidence is at least ``confidence_min``, else unverified;
4. the name the profile shows is compatible with the person's (``names.level`` at least
   ``name_level_min``), else unverified, or rejected for a LinkedIn with a different name; a
   cross-link with a proven profile excuses an informal display name;
5. the profile shows one of the expected organizations (the ones left or joined, or the one the
   list holds), or is cross-linked with a proven profile, else unverified; a LinkedIn still
   showing the old organization as current is consistent only while the report is younger
   than ``stale_days``;
6. a page that links a different LinkedIn than the proven one is someone else's: rejected;
7. a URL another person on the list holds is rejected, and the whole candidate goes to review.

"Proven" means verified by these checks in the previous pass, or already on the person's row
(``fill_links``). Three passes let a proof anchor the next one's cross-links.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from tailwatch import links as link_rules
from tailwatch import util
from tailwatch.enrich import names

RANK = {"rejected": 0, "unverified": 1, "verified": 2}
MAX_QUOTE_WORDS = 40
PASSES = 3


def lower(current: str, to: str) -> str:
    """The weaker of two verdicts: a check can only move a verdict down."""
    return to if RANK.get(to, 1) < RANK.get(current, 1) else current


def key(url: str, kind: str = "") -> str:
    url = str(url or "").strip()
    if not url:
        return ""
    return link_rules.key_of(kind or link_rules.guess_kind(url), url) or link_rules.key_of(link_rules.guess_kind(url), url)


def _keys(urls: Iterable[str]) -> set[str]:
    return {k for k in (key(u) for u in urls) if k}


def _work_text(profile: Mapping[str, Any] | None) -> str:
    if not profile:
        return ""
    jobs = " ".join(f"{p.get('title', '')} at {p.get('company', '')}." for p in profile.get("work") or [])
    return f"{profile.get('text', '')} {jobs}"


def _report_age_days(seed: Mapping[str, Any], today: datetime) -> float:
    published = util.parse(str((seed.get("report") or {}).get("published_at") or ""))
    return max(0.0, (today - published).total_seconds() / 86400) if published else 0.0


def check(answer: Mapping[str, Any], seed: Mapping[str, Any], toolkit: Any, *, store: Any, floors: Mapping[str, Any],
          aliases: Mapping[str, set[str]] | None = None, today: datetime | None = None) -> dict[str, Any]:
    """Every link of the answer with its final verdict and the notes saying which check lowered
    it. Returns {links, shared_with, held}."""
    today = today or util.now()
    person = seed.get("person") or {}
    name = str(person.get("name") or "")
    exclude = int(person.get("id") or 0) or None
    expected = [str(o) for o in seed.get("expected_organizations") or [] if str(o or "").strip()]
    anchors = [str(u) for u in (seed.get("known_links") or {}).values() if u]
    given = [str(u) for u in (seed.get("given_links") or {}).values() if u]
    left = str(person.get("left") or "")
    joined = str(person.get("joined") or "")
    floor_conf = float(floors.get("confidence_min", 0.8))
    floor_name = str(floors.get("name_level_min", "initials"))
    stale_days = float(floors.get("stale_days", 180))
    raw = [dict(link) for link in answer.get("links") or [] if isinstance(link, Mapping) and str(link.get("url") or "").strip()]
    holders: dict[str, int] = {}
    held_status: dict[int, str] = {}
    for link in raw:
        url = str(link["url"]).strip()
        holder = store.person_by_link(str(link.get("kind") or link_rules.guess_kind(url)), url) if store is not None else None
        if holder is None and store is not None:
            holder = store.person_by_link(link_rules.guess_kind(url), url)
        if holder and int(holder["id"]) != exclude:
            holders[url] = int(holder["id"])
            held_status[int(holder["id"])] = str(holder["status"])

    proven: set[str] = set(anchors)
    out: list[dict[str, Any]] = []
    for _ in range(PASSES):
        out = []
        proven_keys = _keys(proven)
        proven_linkedin = {k for k in proven_keys if k.startswith("linkedin:")}
        for link in raw:
            url = str(link["url"]).strip()
            kind = str(link.get("kind") or "") or link_rules.guess_kind(url)
            claimed = str(link.get("verdict") or "unverified")
            verdict = claimed if claimed in RANK else "unverified"
            notes: list[str] = [] if claimed in RANK else [f"unknown verdict {claimed!r}"]
            profile = toolkit.profile_for(url)
            own_links = _keys((profile or {}).get("links") or [])
            this_key = key(url, kind)
            cross = bool(own_links & (proven_keys - {this_key}))
            if not cross:
                for anchor in proven:
                    other = toolkit.profile_for(anchor)
                    if other and this_key and this_key in _keys(other.get("links") or []) and key(anchor) != this_key:
                        cross = True
                        break
            if verdict == "verified":
                if not (toolkit.returned(url) or this_key in _keys(given) or this_key in _keys(anchors)):
                    verdict, notes = "rejected", [*notes, "the URL was not returned by any tool"]
                quote = str(link.get("quote") or "")
                quote_url = str(link.get("quote_url") or url)
                if len(quote.split()) > MAX_QUOTE_WORDS:
                    verdict = lower(verdict, "unverified")
                    notes.append(f"the quote is longer than {MAX_QUOTE_WORDS} words")
                elif not toolkit.quote_found(quote, quote_url):
                    verdict = lower(verdict, "unverified")
                    fetched = bool(toolkit.text_for(quote_url))
                    notes.append("the quote is not in the fetched text" + ("" if fetched else " (nothing was fetched from that URL)"))
                try:
                    confidence = float(link.get("confidence") or 0)
                except (TypeError, ValueError):
                    confidence = 0.0
                if confidence < floor_conf:
                    verdict = lower(verdict, "unverified")
                    notes.append(f"confidence {confidence:.2f} is under {floor_conf:.2f}")
                shown_name = str((profile or {}).get("name") or "")
                if shown_name and name:
                    level = names.level(name, shown_name)
                    if not names.at_least(level, floor_name) and not cross:
                        verdict = lower(verdict, "rejected" if kind == "linkedin" and level == "different" else "unverified")
                        notes.append(f"the profile's name {shown_name!r} is {level} from {name!r}")
                text = f"{_work_text(profile)} {toolkit.text_for(url)}"
                shown_org = names.any_org_in(text, expected, aliases)
                if not shown_org and not cross:
                    verdict = lower(verdict, "unverified")
                    notes.append("the profile shows none of " + (", ".join(expected) if expected else "the expected organizations")
                                 + " and no proven profile links it")
                work = (profile or {}).get("work") or []
                if kind == "linkedin" and work and left and seed.get("mode") == "report":
                    at_joined = joined and any(names.org_match(p.get("company"), joined, aliases) for p in work)
                    still_left = any(p.get("current") and names.org_match(p.get("company"), left, aliases) for p in work)
                    age = _report_age_days(seed, today)
                    if still_left and not at_joined and age > stale_days:
                        verdict = lower(verdict, "unverified")
                        notes.append(f"still shows {left} as current {int(age)} days after the report")
            if kind != "linkedin" and proven_linkedin:
                linked = {k for k in own_links if k.startswith("linkedin:")}
                if linked and not linked & proven_linkedin:
                    verdict = lower(verdict, "rejected")
                    notes.append("the page links a different LinkedIn profile")
            if url in holders:
                verdict = lower(verdict, "rejected")
                notes.append(f"already on the list as #{holders[url]}")
            out.append({"kind": kind, "url": url, "verdict": verdict, "claimed": claimed,
                        "confidence": link.get("confidence"), "quote": link.get("quote", ""),
                        "quote_url": link.get("quote_url", ""), "facts": link.get("facts") or {},
                        "reason": link.get("reason", ""), "notes": notes, "cross_linked": cross,
                        "held_by": holders.get(url)})
        now_proven = set(anchors) | {link["url"] for link in out if link["verdict"] == "verified"}
        if now_proven == proven:
            break
        proven = now_proven
    shared = sorted(set(holders.values()))
    return {"links": out, "shared_with": shared, "held": holders,
            "removed": sorted(pid for pid in shared if held_status.get(pid) == "ignored")}


def best_links(checked: Iterable[Mapping[str, Any]], *, verdict: str = "verified") -> dict[str, dict[str, Any]]:
    """One link per kind with the given verdict, the most confident first."""
    out: dict[str, dict[str, Any]] = {}
    for link in checked:
        if link.get("verdict") != verdict:
            continue
        kind = str(link.get("kind") or "")
        try:
            conf = float(link.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        if kind and (kind not in out or conf > float(out[kind].get("confidence") or 0)):
            out[kind] = dict(link)
    return out
