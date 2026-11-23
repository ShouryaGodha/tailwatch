"""The desk note: the one post a day in the desk channel besides the digest, in a fixed shape
rendered by code from the maintainer's structured answer (never the model's prose for the
layout):

- why this note: the maintainer's summary (or code's one line when it did not answer);
- needs you: held cards worth a person, then proposals, open pull requests and intel past its
  review date, each with its id and the exact reply; at most ten lines;
- done by code: cards verified, dropped by the desk and why, data fixes, issues opened, pull
  requests settled, the watchers' line;
- learned: lessons, wrong grades, what needs the owner;
- the spend line, and how to reply.

The post id is kept under ``DESK:NOTE:<day>`` and the ids the note lists under
``DESK:NOTE_IDS:<post id>`` (``{"R": [12], "P": [3], ...}``), so a reply under the note can name
a bare id and the desk knows which kind it is."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from tailwatch import util
from tailwatch.desk.notices import post_desk

TAG = "desk note"
NOTE_KEY = "DESK:NOTE:"
IDS_KEY = "DESK:NOTE_IDS:"
NEEDS_CAP = 10
VERSIONED = ("RULES", "FLOORS", "ALIASES")
TASK_SETTINGS = ("CODE", "PLAYBOOK", "OTHER")
CAPPED_ROUTINES = {"editor_daily": "verify_card", "enrich_daily": "enrich_person", "engineer_daily": "engineer",
                   "maintainer_daily": "daily_review"}
HOW_TO_REPLY = ("How to reply in this thread: post R12 · drop R12: why · apply P3 · reject P3: why · keep G7 · drop G7: why · "
                "close T4: why. Pull requests are merged on GitHub, never from here.")


def _one_line(text: Any, limit: int) -> str:
    return util.clip(" ".join(str(text or "").split()), limit)


def why_of(review: Mapping[str, Any]) -> str:
    """The reason a card is held or dropped, in the editor's words or the floors that failed."""
    answer = review.get("answer") or {}
    decision = review.get("decision") or {}
    for text in (review.get("note"), answer.get("note") if isinstance(answer, Mapping) else "",
                 answer.get("reason") if isinstance(answer, Mapping) else "", decision.get("why") if isinstance(decision, Mapping) else ""):
        if str(text or "").strip():
            return _one_line(str(text).split(". ")[0], 140)
    return ""


def desk_state(store: Any, config: Any) -> dict[str, Any]:
    """What waits for a person now, read from the store when the note is rendered."""
    held = [r for r in store.reviews(state="held", limit=300) if not r.get("decision")]
    held.sort(key=lambda r: (str(r.get("updated_at") or ""), int(r["id"])))
    waiting = [p for p in store.proposals(state="proposed", limit=200)
               if not (p.get("setting") == "CODE" and p.get("skill") == "engineer")]
    waiting.sort(key=lambda p: (0 if p.get("setting") == "REVERT" else 1, 0 if p.get("urgent") else 1, int(p["id"])))
    return {
        "held": [{"id": int(r["id"]), "person": r.get("person_name") or f"person {r.get('person_id')}", "signal": r.get("signal") or "",
                  "summary": _one_line(r.get("summary"), 120), "why": why_of(r)} for r in held],
        "waiting": [{"id": int(p["id"]), "skill": p.get("skill") or "", "setting": p.get("setting") or "", "op": p.get("op") or "",
                     "text": str((p.get("value") or {}).get("text") or ""), "rationale": _one_line(p.get("rationale"), 160),
                     "urgent": bool(p.get("urgent")), "target": (p.get("value") or {}).get("target"),
                     "why": (p.get("value") or {}).get("why") or ""} for p in waiting],
        "open_tasks": [{"id": int(t["id"]), "pr": t.get("pr_url") or "", "title": ((t.get("value") or {}).get("run") or {}).get("title") or
                        _one_line((t.get("value") or {}).get("text"), 80), "branch": ((t.get("value") or {}).get("run") or {}).get("branch") or "",
                        "review": ((t.get("value") or {}).get("run") or {}).get("review") or ""}
                       for t in store.code_tasks(states=("open",), limit=50)],
        "intel_due": [{"id": int(i["id"]), "text": _one_line(i.get("text"), 100), "by": i.get("said_by") or "",
                       "on": str(i.get("created_at") or "")[:10]} for i in store.intel_due(at=util.iso(), limit=10)],
    }


# -- the lines ------------------------------------------------------------------------------------

def held_line(item: Mapping[str, Any]) -> str:
    move = " · ".join(p for p in (f"{item.get('signal')}: {item.get('summary')}".strip(": "), f"held: {item['why']}" if item.get("why") else "") if p)
    return f"• #R{item['id']} {item['person']} · {move} · reply: post R{item['id']} / drop R{item['id']}: why"


def proposal_line(item: Mapping[str, Any]) -> str:
    pid = item["id"]
    reply = f"reply: apply P{pid} / reject P{pid}: why"
    if item.get("setting") == "REVERT":
        return f"• #P{pid} revert of #P{item.get('target')} proposed: {_one_line(item.get('why') or item.get('text'), 160)} · {reply}"
    if item.get("setting") in TASK_SETTINGS:
        urgent = " (urgent)" if item.get("urgent") else ""
        return (f"• #P{pid} code change{urgent} for {item.get('skill')}: {_one_line(item.get('text'), 300)} · "
                f"reply: apply P{pid} (it becomes an engineer task) / reject P{pid}: why")
    what = f"{item.get('skill')} {str(item.get('setting')).lower()} {item.get('op')}"
    because = f" · {item['rationale']}" if item.get("rationale") else ""
    return f"• #P{pid} {what}: \"{_one_line(item.get('text'), 420)}\"{because} · {reply}"


def task_line(item: Mapping[str, Any]) -> str:
    from tailwatch.desk.engineer import pr_number

    tid = item["id"]
    review = f" · self-review {item['review']}" if item.get("review") else ""
    if item.get("pr"):
        number = pr_number(item["pr"])
        return (f"• #T{tid} PR {number}: {_one_line(item.get('title'), 90)}{review} · {item['pr']} · "
                f"reply: merge PR {number} on GitHub / close T{tid}: why")
    return f"• #T{tid} branch {item.get('branch')}: {_one_line(item.get('title'), 90)}{review} · reply: close T{tid}: why"


def intel_line(item: Mapping[str, Any]) -> str:
    said = f" (said by {item['by']}, {item['on']})" if item.get("by") else f" ({item.get('on')})"
    return f"• #G{item['id']} still true? \"{item['text']}\"{said} · reply: keep G{item['id']} / drop G{item['id']}: why"


def needs(packet: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every item that waits for a person, in the note's order, each with its kind, id and line."""
    desk = packet.get("desk") or {}
    out = [{"kind": "R", "id": h["id"], "line": held_line(h)} for h in desk.get("held") or []]
    out += [{"kind": "P", "id": p["id"], "line": proposal_line(p)} for p in desk.get("waiting") or []]
    out += [{"kind": "T", "id": t["id"], "line": task_line(t)} for t in desk.get("open_tasks") or []]
    out += [{"kind": "G", "id": g["id"], "line": intel_line(g)} for g in desk.get("intel_due") or []]
    return out


def watch_line(watchers: Mapping[str, Any]) -> str:
    kinds = watchers.get("kinds") or {}
    if not kinds:
        return ""
    parts = []
    for kind, counts in sorted(kinds.items()):
        total = sum(int(v) for v in counts.values())
        failed = sum(int(v) for status, v in counts.items() if status != "ok")
        parts.append(f"{kind} {total}" + (f" ({failed} failed)" if failed else ""))
    worst = (watchers.get("errors") or [None])[0]
    tail = f"; the biggest failure: {worst['kind']}, {worst['n']}x: {_one_line(worst['error'], 100)}" if worst else ""
    return "reads since yesterday: " + ", ".join(parts) + tail


def spend_line(spend: Mapping[str, Any]) -> str:
    today = spend.get("today") or {}
    total = sum(float(v) for v in today.values())
    parts = [f"{name.replace('_', ' ')} ${float(v):.2f}" for name, v in sorted(today.items(), key=lambda kv: -float(kv[1])) if float(v) > 0]
    caps = spend.get("caps") or {}
    capped = [f"{CAPPED_ROUTINES[k].replace('_', ' ')} ${float(today.get(CAPPED_ROUTINES[k], 0)):.2f} of ${float(v):.2f}"
              for k, v in caps.items() if k in CAPPED_ROUTINES]
    line = f"Spend today: ${total:.2f}" + (f" ({', '.join(parts[:6])})" if parts else "")
    return line + (f". Daily caps: {', '.join(capped)}." if capped else ".")


def done_lines(packet: Mapping[str, Any], outcome: Mapping[str, Any]) -> list[str]:
    reviews = packet.get("reviews") or {}
    by_state = reviews.get("by_state") or {}
    lines = []
    if reviews.get("total"):
        lines.append(f"{reviews['total']} cards checked: {by_state.get('verified', 0)} verified, {by_state.get('held', 0)} held, "
                     f"{by_state.get('dropped', 0)} dropped")
    dropped = [d for d in reviews.get("dropped") or [] if d.get("why")]
    if dropped:
        lines.append("dropped by the desk: " + "; ".join(f"#R{d['id']} {d.get('person') or ''} ({d['why']})" for d in dropped[:4]))
    issues = packet.get("issues") or {}
    if issues.get("total"):
        lines.append(f"{issues['total']} data issues: {issues.get('changed', 0)} fixed, {issues.get('noop', 0)} needed nothing"
                     + (f", {len(issues.get('failed') or [])} failed" if issues.get("failed") else ""))
    opened = [i for i in outcome.get("issues") or [] if i.get("created")]
    if opened:
        lines.append(f"{len(opened)} issue(s) opened for the custodian: "
                     + ", ".join(f"#I{i['id']} {i['kind']} {i['subject_key']}" for i in opened[:4]))
    settled = outcome.get("settled") or {}
    if settled.get("merged") or settled.get("closed"):
        lines.append("pull requests settled on GitHub: " + ", ".join(
            [f"#T{t} merged" for t in settled.get("merged") or []] + [f"#T{t} closed" for t in settled.get("closed") or []]))
    if outcome.get("expired"):
        lines.append(f"{outcome['expired']} proposal(s) expired after a week without a decision")
    refused = outcome.get("refused") or []
    if refused:
        lines.append(f"{len(refused)} proposal(s) refused by the limits: "
                     + "; ".join(_one_line(r.get("why"), 80) for r in refused[:3]))
    watched = watch_line(packet.get("watchers") or {})
    if watched:
        lines.append(watched)
    return lines


def learned_lines(answer: Mapping[str, Any]) -> list[str]:
    lines = []
    for lesson in (answer.get("lessons") or [])[:4]:
        count = int(lesson.get("count") or 0)
        lines.append(_one_line(lesson.get("lesson"), 200) + (f" ({count}x)" if count > 1 else ""))
    for grade in [g for g in answer.get("grades") or [] if g.get("verdict") == "wrong"][:4]:
        obvious = " (obvious)" if grade.get("kind") == "obvious" else ""
        lines.append(f"{grade.get('agent')} wrong on {_one_line(grade.get('subject'), 30)}{obvious}: {_one_line(grade.get('why'), 160)}")
    for item in (answer.get("needs_owner") or [])[:3]:
        lines.append(f"needs the owner: {_one_line(item.get('what'), 160)}" + (f". {_one_line(item.get('why'), 120)}" if item.get("why") else ""))
    return [line for line in lines if line.strip()]


def fallback_why(packet: Mapping[str, Any], outcome: Mapping[str, Any], waiting: int) -> str:
    bits = []
    if outcome.get("error"):
        bits.append(f"the maintainer did not answer today ({_one_line(outcome['error'], 120)})")
    elif outcome.get("skipped"):
        bits.append(str(outcome["skipped"]))
    elif outcome.get("quiet"):
        bits.append("a quiet day: nothing ran that needed the maintainer")
    bits.append(f"{waiting} item(s) wait for a person" if waiting else "nothing waits for a person")
    text = "; ".join(bits)
    return text[:1].upper() + text[1:] + "."


def sections(packet: Mapping[str, Any], answer: Mapping[str, Any] | None = None, *,
             outcome: Mapping[str, Any] | None = None) -> dict[str, Any]:
    answer, outcome = answer or {}, outcome or {}
    items = needs(packet)
    shown = items if len(items) <= NEEDS_CAP else items[:NEEDS_CAP - 1]
    more = len(items) - len(shown)
    why = _one_line(answer.get("summary"), 400) or fallback_why(packet, outcome, len(items))
    return {"day": str(packet.get("day") or util.local_day()), "why": why, "needs": shown, "more": more,
            "done": done_lines(packet, outcome), "learned": learned_lines(answer), "spend": spend_line(packet.get("spend") or {})}


def _heading(day: str) -> str:
    try:
        when = datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return f"*{TAG} · {day}*"
    return f"*{TAG} · {when.strftime('%A')} {when.day} {when.strftime('%B')}*"


def render_sections(parts: Mapping[str, Any]) -> str:
    lines = [_heading(parts["day"]), f"Why this note: {parts['why']}", "", "Needs you (reply in this thread)"]
    lines += [n["line"] for n in parts["needs"]] or ["• nothing today"]
    if parts.get("more"):
        lines.append(f"• and {parts['more']} more waiting: ask the desk what waits for you")
    lines += ["", "Done by code (no action needed)"]
    lines += [f"• {d}" for d in parts["done"]] or ["• a quiet day"]
    if parts.get("learned"):
        lines += ["", "Learned"] + [f"• {line}" for line in parts["learned"]]
    lines += ["", parts["spend"], "", HOW_TO_REPLY]
    return "\n".join(lines)


def render(packet: Mapping[str, Any], answer: Mapping[str, Any] | None = None, *,
           outcome: Mapping[str, Any] | None = None) -> str:
    """The note's text from the day's packet, the maintainer's answer and what code did."""
    return render_sections(sections(packet, answer, outcome=outcome))


def note_ids(parts: Mapping[str, Any]) -> dict[str, list[int]]:
    ids: dict[str, list[int]] = {}
    for item in parts["needs"]:
        ids.setdefault(item["kind"], []).append(int(item["id"]))
    return ids


def post_note(store: Any, config: Any, env: Mapping[str, str], *, packet: Mapping[str, Any] | None = None,
              answer: Mapping[str, Any] | None = None, outcome: Mapping[str, Any] | None = None,
              force: bool = False) -> dict[str, Any]:
    """Post the day's note once (``force`` posts again). Without a packet it gathers one and
    uses the maintainer's answer of the day from the run log."""
    zone = config.schedule.timezone
    day = util.local_day(zone)
    key = NOTE_KEY + day
    already = store.get_setting(key)
    if already and not force:
        return {"posted": "", "already": already}
    if packet is None:
        from tailwatch.desk import maintainer

        packet = maintainer.gather(store, config, env)
    packet = {**packet, "desk": desk_state(store, config), "day": packet.get("day") or day}
    if answer is None:
        found = store.last_answer("daily_review", since=util.iso(util.local_time(zone).replace(hour=0, minute=0, second=0,
                                                                                               microsecond=0)))
        answer = found if isinstance(found, dict) else None
    parts = sections(packet, answer, outcome=outcome)
    text = render_sections(parts)
    post_id = post_desk(store, config, env, text)
    ids = note_ids(parts)
    store.set_setting(key, post_id, by="desk note", history=False)
    store.set_setting(IDS_KEY + post_id, ids, by="desk note", history=False)
    for pid in ids.get("P", []) + ids.get("T", []):
        row = store.proposal(pid)
        if row and not row.get("thread"):
            store.set_proposal(pid, thread=post_id)
    return {"posted": post_id, "needs": len(parts["needs"]) + int(parts["more"]), "ids": ids, "text": text}
