"""The floors under a card: deterministic checks after the editor answers. They can only lower
its verdict (confirm to fix or hold, a drop to hold), never raise it.

Five floors:

- identity: the source is not marked someone else's, no other listed person holds the card's
  links or its source, and the profile's name (when a read gives one) fits the person's;
- event present: for a profile source, the change is still on the latest read (a reverted page
  fails; a rendered diff that is not a verbatim run of the read proves nothing either way);
- label supported: a verbatim quote from text a tool fetched or a stored read, or, for the
  pack's first signal, the read's structured positions (the role at the card's organization
  ended, or the entry the previous read listed is gone from the latest one);
- not a duplicate: no posted card for the same move within ``digest.repost_days`` unless the
  judge said this verdict supersedes it;
- confidence: the judge's and the editor's at or above the floor (setting
  ``VERIFICATION:FLOORS``, default 0.6).

Identity, duplicate and confidence need no fetch, so they are run again at post time
(``run_fetch_free``). Everything here is pure: the card dict (``desk.editor.card_for``) holds
what the floors read.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

FLOORS = ("identity", "event_present", "label_supported", "not_duplicate", "confidence")
FETCH_FREE = ("identity", "not_duplicate", "confidence")
SETTING = "VERIFICATION:FLOORS"
DEFAULT_CONFIDENCE = 0.6
MIN_QUOTE_CHARS = 12
PLAYBOOKS = frozenset({"fill_links", "mark_wrong_person", "recheck_person", "fix_card_fields"})
# The playbook that can repair an issue of each kind; a kind without one holds the card.
PLAYBOOK_FOR = {"wrong_person": "mark_wrong_person", "shared_link": "fill_links", "missing_link": "fill_links",
                "wrong_link": "fill_links", "change_gone": "recheck_person", "stale_read": "recheck_person",
                "card_fields": "fix_card_fields"}
ITEM_KINDS = ("arxiv", "semantic_scholar", "rss", "scholar", "openreview")  # lists of works, not profiles
RANK = {"confirm": 3, "fix": 2, "hold": 1}
_ORG_SUFFIXES = {"inc", "llc", "ltd", "limited", "corp", "corporation", "co", "gmbh", "plc", "the", "sa", "ag"}


def _pass(detail: str = "") -> dict[str, str]:
    return {"result": "pass", "detail": detail}


def _fail(detail: str, issue: str = "") -> dict[str, str]:
    return {"result": "fail", "detail": detail, "issue": issue}


def _skip(detail: str) -> dict[str, str]:
    return {"result": "skip", "detail": detail}


def confidence_floor(store: Any) -> float:
    """The floor from settings: a number, or ``{"confidence": n}``."""
    value = store.get_setting(SETTING, None) if store is not None else None
    if isinstance(value, Mapping):
        value = value.get("confidence")
    try:
        return float(value) if value is not None else DEFAULT_CONFIDENCE
    except (TypeError, ValueError):
        return DEFAULT_CONFIDENCE


# -- text and names ----------------------------------------------------------------------------


def norm_text(text: Any) -> str:
    """Lower case, letters and digits only, single spaces: what "verbatim" survives (quotes,
    dashes and line breaks differ between a page and a copy of it)."""
    return " ".join(re.sub(r"[^0-9a-z]+", " ", str(text or "").lower()).split())


def quote_in(quote: str, text: str) -> bool:
    needle = norm_text(quote.replace("…", " ").replace("...", " "))
    return len(needle) >= MIN_QUOTE_CHARS and needle in norm_text(text)


def verbatim_quotes(answer: Mapping[str, Any], texts: Mapping[str, str]) -> list[dict[str, str]]:
    """The answer's quotes found in the text of their url, or else in any text held."""
    found = []
    for quote in answer.get("quotes") or []:
        text, url = str((quote or {}).get("text") or ""), str((quote or {}).get("url") or "")
        if not text:
            continue
        if quote_in(text, texts.get(url, "")) or any(quote_in(text, t) for t in texts.values()):
            found.append({"url": url, "text": text})
    return found


def alias_map(aliases: Mapping[str, list[str]] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for canonical, others in (aliases or {}).items():
        for name in [canonical, *(others or [])]:
            out[norm_org(name)] = norm_org(canonical)
    return out


def norm_org(name: Any) -> str:
    return " ".join(w for w in norm_text(name).split() if w not in _ORG_SUFFIXES)


def org_match(a: Any, b: Any, aliases: Mapping[str, str] | None = None) -> bool:
    """The same organization: equal once normalised, the same alias, or one name's words all
    inside the other's ("DeepMind" and "Google DeepMind")."""
    left, right = norm_org(a), norm_org(b)
    if not left or not right:
        return False
    table = aliases or {}
    left, right = table.get(left, left), table.get(right, right)
    if left == right:
        return True
    small, large = sorted((left.split(), right.split()), key=len)
    return bool(small) and set(small) <= set(large)


def names_compatible(listed: str, seen: str) -> bool:
    """Whether a profile's name can be the listed person's: a shared word, a nickname prefix
    ("Alex" and "Alexander"), or initials with the surname. Missing names are compatible."""
    a, b = norm_text(listed).split(), norm_text(seen).split()
    if not a or not b:
        return True
    if set(a) & set(b):
        return True
    for x in a:
        for y in b:
            if min(len(x), len(y)) >= 3 and (x.startswith(y) or y.startswith(x)):
                return True
    return False


# -- the five floors ---------------------------------------------------------------------------


def _issue_kinds(answer: Mapping[str, Any] | None) -> set[str]:
    return {str((i or {}).get("kind") or "") for i in (answer or {}).get("issues") or []}


def identity(card: Mapping[str, Any], answer: Mapping[str, Any] | None = None) -> dict[str, str]:
    if str(card.get("source_status") or "") == "wrong_person":
        return _fail("the source is marked as someone else's", "wrong_person")
    holders = {u: pid for u, pid in (card.get("holders") or {}).items() if pid and int(pid) != int(card.get("person_id") or 0)}
    if holders:
        url, pid = next(iter(holders.items()))
        return _fail(f"{url} belongs to person #{pid} on the list", "shared_link")
    if "wrong_person" in _issue_kinds(answer):
        return _fail("the editor found the profile is someone else's", "wrong_person")
    seen = str(card.get("profile_name") or "")
    if seen and not names_compatible(str(card.get("name") or ""), seen):
        return _fail(f"the profile's name is {seen!r}, the person is {card.get('name')!r}", "wrong_person")
    return _pass("the source and links are this person's" + (f" (profile name {seen!r})" if seen else ""))


def is_profile(kind: str) -> bool:
    return not kind.startswith("feed:") and kind not in ITEM_KINDS


def event_present(card: Mapping[str, Any], answer: Mapping[str, Any] | None = None) -> dict[str, str]:
    kind = str(card.get("kind") or "")
    if not is_profile(kind):
        return _skip("a report or a list of works, not a profile")
    answer = answer or {}
    if answer.get("drop_reason") == "change_gone" or "change_gone" in _issue_kinds(answer):
        return _fail("the editor found the change gone from the latest read", "change_gone")
    latest = card.get("latest") or {}
    if not latest:
        return _skip("no read of the source is stored")
    if int(latest.get("id") or 0) == int(card.get("observation_id") or 0):
        return _pass("the latest read is the read the judge saw")
    if latest.get("content") == card.get("judged_content"):
        return _pass("the latest read is unchanged since the read the judge saw")
    previous = card.get("previous") or {}
    if previous and latest.get("content") == previous.get("content"):
        return _fail("the latest read is back to what it was before the change", "change_gone")
    probes = [str(e.get("after") or "") for e in card.get("events") or [] if str(e.get("after") or "").strip()]
    text = str(latest.get("text") or "") + "\n" + str(latest.get("raw") or "")
    if probes and all(quote_in(" ".join(p.split()[:25]), text) or len(norm_text(p)) < MIN_QUOTE_CHARS for p in probes):
        return _pass("the change is on the latest read")
    return _skip("the latest read differs; the rendered change is not a verbatim run of it")


def _positions_at(positions: list[Mapping[str, Any]], organization: str, aliases: Mapping[str, str] | None) -> list[Mapping[str, Any]]:
    return [p for p in positions if org_match(p.get("company"), organization, aliases)]


def label_supported(card: Mapping[str, Any], answer: Mapping[str, Any], *, texts: Mapping[str, str],
                    aliases: Mapping[str, str] | None = None) -> dict[str, str]:
    quoted = verbatim_quotes(answer, texts)
    if quoted:
        return _pass(f"quoted: {quoted[0]['text'][:160]}")
    if card.get("signal") == card.get("first_signal"):
        organization = str(card.get("organization") or "")
        latest = list((card.get("latest") or {}).get("positions") or [])
        before = list((card.get("previous") or {}).get("positions") or [])
        if organization and latest:
            there = _positions_at(latest, organization, aliases)
            if there and not any(p.get("current") for p in there):
                return _pass(f"the latest read's positions show the role at {organization} ended")
            if not there and _positions_at(before, organization, aliases):
                return _pass(f"the role at {organization} is gone from the latest read's positions (a removed role is a departure)")
    if answer.get("quotes"):
        return _fail("no quote is verbatim in the text fetched or stored")
    return _fail("no verbatim quote and no position record behind the label")


def _ids(value: Any) -> set[int]:
    if isinstance(value, (list, tuple, set)):
        return {int(v) for v in value if v}
    return {int(value)} if value else set()


def not_duplicate(card: Mapping[str, Any]) -> dict[str, str]:
    """``card["posted"]`` holds the person's posted cards within the repost window."""
    own = int(card.get("verdict_id") or 0)
    posted = [p for p in card.get("posted") or [] if int(p.get("verdict_id") or 0) != own]
    same = [p for p in posted if p.get("move_key") == card.get("move_key")
            or int(p.get("verdict_id") or 0) in _ids(card.get("same_as"))]
    if not same:
        return _pass()
    superseded = _ids(card.get("supersedes")) & {int(p["verdict_id"]) for p in same}
    if superseded:
        return _pass(f"supersedes the posted card of verdict {min(superseded)}")
    return _fail(f"a card for the same move was posted on {str(same[0].get('at') or '')[:10]}", "duplicate")


def confidence(card: Mapping[str, Any], answer: Mapping[str, Any] | None, *, floor: float) -> dict[str, str]:
    judge = float(card.get("confidence") or 0)
    if judge < floor:
        return _fail(f"the judge's confidence {judge:.2f} is under {floor:.2f}")
    if answer is not None:
        editor = float(answer.get("confidence") or 0)
        if editor < floor:
            return _fail(f"the editor's confidence {editor:.2f} is under {floor:.2f}")
        return _pass(f"judge {judge:.2f}, editor {editor:.2f}")
    return _pass(f"judge {judge:.2f}")


def run_all(card: Mapping[str, Any], answer: Mapping[str, Any], *, texts: Mapping[str, str], floor: float,
            aliases: Mapping[str, str] | None = None) -> dict[str, dict[str, str]]:
    return {"identity": identity(card, answer), "event_present": event_present(card, answer),
            "label_supported": label_supported(card, answer, texts=texts, aliases=aliases),
            "not_duplicate": not_duplicate(card), "confidence": confidence(card, answer, floor=floor)}


def run_fetch_free(card: Mapping[str, Any], answer: Mapping[str, Any] | None, *, floor: float) -> dict[str, dict[str, str]]:
    """The floors that need no fetch: identity (without the profile's name), duplicate and
    confidence. Run before a trusted report is verified and again at post time. With no
    ``answer`` only the judge's confidence is checked."""
    bare = {k: v for k, v in card.items() if k != "profile_name"}
    return {"identity": identity(bare, answer), "not_duplicate": not_duplicate(card), "confidence": confidence(card, answer, floor=floor)}


def failed(results: Mapping[str, Mapping[str, str]]) -> list[str]:
    return [name for name, r in results.items() if r.get("result") == "fail"]


def playbook_of(result: Mapping[str, str]) -> str:
    return PLAYBOOK_FOR.get(str(result.get("issue") or ""), "")


# -- lowering ----------------------------------------------------------------------------------


def allow_drop(card: Mapping[str, Any], answer: Mapping[str, Any], results: Mapping[str, Mapping[str, str]]) -> bool:
    """A drop stands without a person only for a signal other than the pack's first (looking,
    say), or as a duplicate the duplicate floor confirms. Any other drop becomes a hold."""
    if str(answer.get("verdict")) != "drop":
        return False
    if card.get("signal") != card.get("first_signal"):
        return True
    return str(answer.get("drop_reason") or "") == "duplicate" and "not_duplicate" in failed(results)


def editor_repairs(answer: Mapping[str, Any]) -> list[str]:
    """The playbooks the editor's own issues ask for (unknown kinds have none)."""
    out = [PLAYBOOK_FOR.get(str((i or {}).get("kind") or ""), "") for i in answer.get("issues") or []]
    fields = answer.get("fields") or {}
    if fields.get("organization") or fields.get("title"):
        out.append("fix_card_fields")
    return [p for p in out if p]


def lower(answer: Mapping[str, Any], results: Mapping[str, Mapping[str, str]], *, drop_allowed: bool = False) -> dict[str, Any]:
    """The editor's answer with its verdict lowered by the floors. A confirm with failures is a
    fix when every failure has a playbook, else a hold; a fix stays a fix only when something
    can repair it; a hold stays a hold; a drop stands only when ``drop_allowed``."""
    said = str(answer.get("verdict") or "hold")
    if said not in (*RANK, "drop"):
        said = "hold"
    bad = failed(results)
    repairable = bool(bad) and all(playbook_of(results[name]) in PLAYBOOKS for name in bad)
    if said == "drop":
        final = "drop" if drop_allowed else "hold"
    elif said == "hold":
        final = "hold"
    elif bad:
        final = "fix" if repairable else "hold"
    elif said == "fix":
        final = "fix" if editor_repairs(answer) else "hold"
    else:
        final = "confirm"
    if said in RANK and final in RANK and RANK[final] > RANK[said]:  # never: kept as an assertion of the rule
        final = said
    return {**answer, "verdict": final, "editor_verdict": said, "failed": bad}


# -- what code may change, from structured position records only -------------------------------


def _norm_title(text: Any) -> str:
    return norm_text(text)


def structured_fix(card: Mapping[str, Any], fields: Mapping[str, Any] | None, *,
                   aliases: Mapping[str, str] | None = None) -> dict[str, str] | None:
    """The card's organization and title in the latest read's own words, when a position
    record there carries what the editor names. None when no record says so or the card
    already says it."""
    org = str((fields or {}).get("organization") or "").strip()
    title = str((fields or {}).get("title") or "").strip()
    positions = list((card.get("latest") or {}).get("positions") or [])
    if not (org or title) or not positions:
        return None
    want = org or str(card.get("organization") or "")
    found = _positions_at(positions, want, aliases) if want else positions
    if title:
        found = [p for p in found if _norm_title(p.get("title")) == _norm_title(title)]
    if not found:
        return None
    found = sorted(found, key=lambda p: bool(p.get("current")))  # the role the card is about (an ended one) first
    chosen = found[0]
    out = {"organization": str(chosen.get("company") or want), "title": str(chosen.get("title") or title)}
    if out["organization"] == str(card.get("organization") or "") and out["title"] == str(card.get("title") or ""):
        return None
    return out


def landing(card: Mapping[str, Any], now_at: Mapping[str, Any] | None, *,
            aliases: Mapping[str, str] | None = None) -> dict[str, str] | None:
    """Where a departed person went: the editor's ``now_at`` names a current position of the
    latest read at another organization, begun no earlier than the departure. Its organization,
    title and start in the read's words; None otherwise (the same employer, a role held
    alongside, nothing current there)."""
    org = str((now_at or {}).get("organization") or "").strip()
    positions = list((card.get("latest") or {}).get("positions") or [])
    left = str(card.get("organization") or "")
    if not org or not positions or org_match(org, left, aliases):
        return None
    current = [p for p in _positions_at(positions, org, aliases) if p.get("current")]
    if not current:
        return None
    ended = max((str(p.get("end") or "") for p in _positions_at(positions, left, aliases) if not p.get("current")), default="")
    chosen = max(current, key=lambda p: str(p.get("start") or ""))
    since = str(chosen.get("start") or (now_at or {}).get("since") or "")
    if ended and since and since[:len(ended)] < ended[:len(since)]:
        return None
    return {"organization": str(chosen.get("company") or org), "title": str(chosen.get("title") or (now_at or {}).get("title") or ""),
            "since": since}
