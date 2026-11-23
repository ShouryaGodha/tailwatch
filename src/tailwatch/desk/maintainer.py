"""The maintainer: once a day it reads what the desk did and answers once. It applies nothing.

``run_daily`` (the scheduler's maintenance job): proposals older than a week expire; engineer
tasks whose pull request a person merged or closed on GitHub are settled; the day's packet is
gathered (reviews by outcome and the floors that failed, held cards and decisions, issues and
what the playbooks did, candidates, job receipts, the run log by routine with cost and frequent
errors, the watchers' reads, a sample of the judge's verdicts with their prompt versions, the
ranker's last answer, spend against the caps, the holdout score, learned rules nobody used for
two weeks, and ``current``: what the agents run on now, so it never proposes what exists); one
tool-less ``daily_review`` call answers with a summary, lessons, grades, proposals, issues and
what needs the owner. Code then holds the proposals to the limits (``check_proposal``): each
cites two of the day's ids, or one it graded an obvious mistake; evidence only from samples
judged under an older judge prompt is refused; a rule carries no URL, no Slack id and no order
about posting (``scan_text``); five proposals and ten issues a day; a proposal expires in a week.
Then the desk note is posted.

A person decides (``decide``, called by the desk's core ``decide`` tool): apply writes the
versioned setting ``<SKILL>:<SETTING>`` (a CODE, PLAYBOOK or OTHER proposal becomes an engineer
task instead), reject needs a why, revert puts back the version before, close drops an engineer
task's pull request. An applied change whose holdout score fell by ten points is proposed for
revert the next day (``suggest_reverts``); code never reverts on its own."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from tailwatch import util
from tailwatch.agent import loop, prompts, routines
from tailwatch.desk import note
from tailwatch.desk.engineer_runner import looks_secret

ROUTINE = "daily_review"
PROMPT = "maintenance/agent.md"
RULES_KEY = "MAINTENANCE:RULES"
WINDOW_HOURS = 26
MAX_TOKENS = 16000
MIN_EVIDENCE = 2
MIN_EVIDENCE_OBVIOUS = 1
PROPOSALS_PER_DAY = 5
ISSUES_PER_DAY = 10
REVERT_POINTS = 10.0
REVERT_WINDOW_DAYS = 2
MIN_LABELLED = 10  # below this a ten-point move is one card, not a signal
UNUSED_RULE_DAYS = 14
RULE_CHARS = 400
MAX_RULES = 15
PROMPT_CAP = 20_000
SKILLS = ("judge", "verification", "ranking", "data", "enrichment", "maintenance", "engineer", "desk")
SETTINGS = ("RULES", "FLOORS", "ALIASES", "PLAYBOOK", "OTHER", "CODE")
VERSIONED = ("RULES", "FLOORS", "ALIASES")
TASK_SETTINGS = ("CODE", "PLAYBOOK", "OTHER")
SETTINGS_FOR: dict[str, tuple[str, ...]] = {
    "judge": (),  # the judge changes through its prompt and the pack's rules: a pull request
    "verification": ("RULES", "FLOORS"), "ranking": ("RULES",), "data": ("RULES",),
    "enrichment": ("RULES", "ALIASES", "FLOORS"), "maintenance": ("RULES",), "engineer": ("RULES",), "desk": ("RULES",)}
USAGE_TRACKED = ("verification",)  # the editor records the learned rules it used (answer.rules_used)
GRADE_AGENTS = ("judge", "editor", "ranker", "enricher", "custodian", "engineer", "watcher")
ISSUE_SUBJECTS = ("person", "source", "review")

_S: dict[str, Any] = {"type": "string"}
_IDS: dict[str, Any] = {"type": "array", "items": {"type": "string"}}


def _object(**properties: Any) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "required": list(properties), "properties": properties}


SCHEMA = _object(
    summary=_S,
    lessons={"type": "array", "items": _object(lesson=_S, count={"type": "integer"}, example_ids=_IDS)},
    grades={"type": "array", "items": _object(agent={"type": "string", "enum": list(GRADE_AGENTS)}, subject=_S,
                                              verdict={"type": "string", "enum": ["right", "wrong", "unsure"]},
                                              kind={"type": "string", "enum": ["obvious", "pattern"]}, why=_S)},
    proposals={"type": "array", "items": _object(skill={"type": "string", "enum": list(SKILLS)},
                                                 setting={"type": "string", "enum": list(SETTINGS)},
                                                 op={"type": "string", "enum": ["add", "remove", "replace", "set"]},
                                                 value=_S, rationale=_S, evidence=_IDS, urgent={"type": "boolean"})},
    issues={"type": "array", "items": _object(kind=_S, subject_key=_S, playbook=_S, note=_S)},
    needs_owner={"type": "array", "items": _object(what=_S, why=_S)},
)


# -- what a learned rule may say ----------------------------------------------------------------

_URL = re.compile(r"https?://|\bwww\.|\b[\w-]+\.(?:com|org|net|io|ai|dev|co|app|xyz)(?:/|\b)", re.IGNORECASE)
_SLACK = re.compile(r"<[@#!][A-Z0-9][^>]*>|\b[UWCGD](?=[A-Z0-9]*\d)[A-Z0-9]{8,11}\b")
_ORDER = re.compile(
    r"\b(?:always|must)\s+(?:post|confirm|publish|send|approve|verify)\b"
    r"|\bnever\s+(?:post|publish|send)\b"
    r"|\b(?:post|confirm|publish|send|ignore|drop|approve)\s+(?:every|all|any)\b"
    r"|\bignore\s+(?:(?:the|all|any|previous|earlier|your|above|these)\s+){0,3}(?:instructions|rules|floors|prompts?|evidence)\b",
    re.IGNORECASE)


def scan_text(text: str) -> str:
    """Why a learned rule may not say this ('' when it may): a URL, a Slack id, an order about
    posting, a secret, an empty or overlong rule. One scan for every writer of a learned rule."""
    clean = " ".join(str(text or "").split())
    if not clean:
        return "a rule says something"
    if len(clean) > RULE_CHARS:
        return f"a rule is at most {RULE_CHARS} characters"
    if looks_secret(clean):
        return "a rule never holds a secret"
    if _URL.search(clean):
        return "a rule carries no URL or domain"
    if _SLACK.search(clean):
        return "a rule carries no Slack id"
    if _ORDER.search(clean):
        return "a rule never orders what to post, confirm or ignore outright"
    return ""


# -- ids ----------------------------------------------------------------------------------------

_KIND_WORDS = {"review": "R", "card": "R", "issue": "I", "proposal": "P", "verdict": "V", "candidate": "C", "task": "T",
               "intel": "G"}


def norm_id(raw: Any) -> str:
    """'#R12', 'r12', 'review:12' -> 'R12'; 'person:5' stays; anything else as written."""
    text = str(raw or "").strip().lstrip("#").strip()
    found = re.fullmatch(r"([A-Za-z])\s*#?(\d+)", text)
    if found:
        return found.group(1).upper() + found.group(2)
    found = re.fullmatch(r"([A-Za-z]+)\s*[:#]\s*(\d+)", text)
    if found:
        word = found.group(1).lower()
        if word in _KIND_WORDS:
            return _KIND_WORDS[word] + found.group(2)
        return f"{word}:{found.group(2)}"
    return text


def known_ids(packet: Mapping[str, Any]) -> set[str]:
    """The ids the day's packet carries: what a proposal may cite."""
    ids: set[str] = set()
    reviews = packet.get("reviews") or {}
    ids |= {f"R{i}" for i in reviews.get("ids") or []}
    ids |= {f"R{h['id']}" for h in packet.get("held") or []} | {f"R{d['id']}" for d in packet.get("decisions") or []}
    ids |= {f"I{i['id']}" for i in (packet.get("issues") or {}).get("list") or []}
    ids |= {str(s["id"]) for s in packet.get("judge_samples") or []}
    ids |= {f"C{c['id']}" for c in (packet.get("candidates") or {}).get("recent") or []}
    ids |= {f"person:{r['person_id']}" for r in packet.get("ranker") or [] if r.get("person_id")}
    ids |= {f"P{p['id']}" for p in packet.get("proposals") or []}
    ids |= {f"T{t['task']}" for t in (packet.get("engineer") or {}).get("tasks") or []}
    return ids


def stale_ids(packet: Mapping[str, Any]) -> set[str]:
    """Judge samples judged under an older prompt than the judge runs on now."""
    current = str((packet.get("current") or {}).get("judge_prompt_version") or "")
    return {str(s["id"]) for s in packet.get("judge_samples") or []
            if current and s.get("prompt_version") and s["prompt_version"] != current}


def obvious_ids(answer: Mapping[str, Any], known: set[str], stale: set[str]) -> set[str]:
    """Subjects the maintainer graded an obvious mistake, among the day's fresh ids."""
    return {norm_id(g.get("subject")) for g in answer.get("grades") or []
            if g.get("verdict") == "wrong" and g.get("kind") == "obvious"} & (known - stale)


# -- the day's packet -----------------------------------------------------------------------------

def day_start(config: Any) -> str:
    return util.iso(util.local_time(config.schedule.timezone).replace(hour=0, minute=0, second=0, microsecond=0))


def failed_floors(review: Mapping[str, Any]) -> list[str]:
    """The floors a review failed, whichever shape the editor recorded them in."""
    floors = review.get("floors") or {}
    if isinstance(floors, Mapping):
        if isinstance(floors.get("failed"), list):
            return [str(f) for f in floors["failed"]]
        out = []
        for name, value in floors.items():
            if value is False or (isinstance(value, Mapping) and (value.get("ok") is False or value.get("passed") is False)):
                out.append(str(name))
        return out
    if isinstance(floors, list):
        return [str(f.get("name")) for f in floors if isinstance(f, Mapping) and f.get("ok") is False]
    return []


def holdout_score(store: Any) -> dict[str, Any] | None:
    """The editor's score on the labelled holdout (``tailwatch.desk.verify.score``), or None."""
    try:
        from tailwatch.desk import verify
    except ImportError:
        return None
    score = getattr(verify, "score", None)
    if score is None:
        return None
    found: Any = None
    for kwargs in ({"holdout_only": True}, {"holdout": True}, {}):
        try:
            found = score(store, **kwargs)
            break
        except TypeError:
            continue
        except Exception:  # a score that cannot be computed is no reason to skip the day
            return None
    return dict(found) if isinstance(found, Mapping) else None


def current_setup(store: Any, config: Any) -> dict[str, Any]:
    """What the agents run on now: the judge's prompt version, the judge's and the ranker's
    prompts as rendered today, and every learned rule, floor and alias set."""
    from tailwatch.pipeline import packs, ranker

    out: dict[str, Any] = {"judge_prompt_version": "", "prompts": {}, "settings": {}}
    try:
        pack = packs.load(config.signals)
        out["judge_prompt_version"] = pack.prompt_version
        out["prompts"]["judge"] = util.clip(packs.judge_instructions(pack, config), PROMPT_CAP)
        out["prompts"]["ranker"] = util.clip(ranker.instructions(store, config, pack), PROMPT_CAP)
    except Exception as error:  # a prompt that cannot be read is said, never a failed review
        out["prompts"]["error"] = f"not readable: {type(error).__name__}: {util.clip(error, 160)}"
    for skill, settings in SETTINGS_FOR.items():
        for setting in settings:
            row = store.setting_row(f"{skill.upper()}:{setting}")
            if row and row.get("value"):
                out["settings"][f"{skill.upper()}:{setting}"] = {"value": row["value"], "version": row["version"]}
    return out


def unused_rules(store: Any, config: Any) -> list[dict[str, Any]]:
    """Learned rules at least two weeks old that no answer cited in two weeks (only for skills
    whose answers record the rules they used)."""
    cutoff = util.ago(days=UNUSED_RULE_DAYS)
    used: set[str] = set()
    for row in store.review_answers(since=cutoff):
        for item in (row.get("answer") or {}).get("rules_used") or []:
            used.add(str(item).strip())
    out = []
    for skill in USAGE_TRACKED:
        key = f"{skill.upper()}:RULES"
        rules = [str(r) for r in store.get_setting(key, []) or []]
        history = list(reversed(store.setting_history(key, limit=500)))  # oldest first
        for number, rule in enumerate(rules, 1):
            added = next((h["at"] for h in history if rule in (h.get("value") or [])), "")
            if not added or added > cutoff or str(number) in used or rule in used:
                continue
            out.append({"key": key, "number": number, "rule": rule, "since": added[:10]})
    return out


def _runs(store: Any, since: str) -> list[dict[str, Any]]:
    by_routine: dict[str, dict[str, Any]] = {}
    for row in store.runs_by_routine(since=since):
        item = by_routine.setdefault(row["routine"], {"routine": row["routine"], "runs": 0, "by_state": {}, "cost_usd": 0.0,
                                                      "model_calls": 0, "unpriced": False, "errors": []})
        item["runs"] += int(row["n"])
        item["by_state"][row["state"]] = int(row["n"])
        item["cost_usd"] = round(item["cost_usd"] + float(row["usd"] or 0), 4)
        item["model_calls"] += int(row["model_calls"] or 0)
        item["unpriced"] = item["unpriced"] or not int(row["priced"] if row["priced"] is not None else 1)
    for row in store.run_errors(since=since):
        if row["routine"] in by_routine and len(by_routine[row["routine"]]["errors"]) < 3:
            by_routine[row["routine"]]["errors"].append({"error": util.clip(row["error"] or f"stopped {row['stopped']}", 200),
                                                         "stopped": row["stopped"], "n": int(row["n"])})
    return list(by_routine.values())


def _watchers(store: Any, since: str) -> dict[str, Any]:
    kinds: dict[str, dict[str, int]] = {}
    errors = []
    for row in store.watch_stats(since=since):
        kinds.setdefault(row["kind"], {})[row["status"]] = int(row["n"])
        if row["status"] != "ok" and row.get("sample_error"):
            errors.append({"kind": row["kind"], "status": row["status"], "n": int(row["n"]), "error": util.clip(row["sample_error"], 200)})
    errors.sort(key=lambda e: -e["n"])
    return {"kinds": kinds, "errors": errors[:8]}


def _ranker(store: Any, since: str) -> list[dict[str, Any]]:
    answer = store.last_answer("rank_cards", since=since)
    ranked = answer.get("ranked") if isinstance(answer, Mapping) else None
    return [{k: r.get(k) for k in ("person_id", "score", "worth", "strength", "why")} for r in (ranked or [])[:30]
            if isinstance(r, Mapping)]


def _spend(store: Any, config: Any) -> dict[str, Any]:
    budgets = config.budgets
    return {"today": store.spend_by_routine(since=day_start(config)),
            "caps": {"maintainer_daily": budgets.maintainer_daily, "editor_daily": budgets.editor_daily,
                     "enrich_daily": budgets.enrich_daily, "engineer_daily": budgets.engineer_daily}}


def gather(store: Any, config: Any, env: Mapping[str, str] | None = None, *, since: str = "") -> dict[str, Any]:
    """The day's packet: everything the maintainer reads, and what the note renders."""
    from tailwatch.desk import engineer

    zone = config.schedule.timezone
    since = since or util.ago(hours=WINDOW_HOURS)
    today = util.local_day(zone)
    reviews = store.reviews(since=since, limit=500)
    held = [{"id": int(r["id"]), "person": r.get("person_name"), "signal": r.get("signal"), "summary": util.clip(r.get("summary"), 200),
             "why": note.why_of(r), "floors_failed": failed_floors(r)} for r in reviews if r["state"] == "held"]
    dropped = [{"id": int(r["id"]), "person": r.get("person_name"), "why": note.why_of(r),
                "by": (r.get("decision") or {}).get("by") if isinstance(r.get("decision"), Mapping) else ""}
               for r in reviews if r["state"] == "dropped"]
    issues = store.issues_since(since=since)
    candidates = store.candidates_since(since=since)
    jobs = [j for day in sorted({today, util.local_day(zone, util.now() - timedelta(days=1))}) for j in store.jobs_on(day)]
    packet: dict[str, Any] = {
        "day": today, "since": since,
        "reviews": {"total": len(reviews), "by_state": dict(Counter(r["state"] for r in reviews)),
                    "floors_failed": dict(Counter(f for r in reviews for f in failed_floors(r))),
                    "dropped": dropped[:15], "ids": [int(r["id"]) for r in reviews][:300]},
        "held": held[:25],
        "decisions": [{"id": int(r["id"]), "person": r.get("person_name"), "state": r["state"], "decision": r.get("decision")}
                      for r in store.decided_reviews(since=since, limit=40)],
        "issues": {"total": len(issues), "by_kind": dict(Counter(i["kind"] for i in issues)),
                   "by_state": dict(Counter(i["state"] for i in issues)),
                   "changed": sum(1 for i in issues if (i.get("result") or {}).get("changed")),
                   "noop": sum(1 for i in issues if i["state"] == "fixed" and not (i.get("result") or {}).get("changed")),
                   "failed": [{"id": int(i["id"]), "kind": i["kind"], "subject_key": i["subject_key"], "note": util.clip(i.get("note"), 160),
                               "error": util.clip((i.get("result") or {}).get("error") or (i.get("result") or {}).get("refused"), 160)}
                              for i in issues if i["state"] in ("failed", "wontfix")][:15],
                   "list": [{"id": int(i["id"]), "kind": i["kind"], "subject_key": i["subject_key"], "state": i["state"],
                             "playbook": i.get("playbook") or ""} for i in issues][:40]},
        "candidates": {"total": len(candidates), "by_state": dict(Counter(c["state"] for c in candidates)),
                       "recent": [{"id": int(c["id"]), "feed": c["feed"], "state": c["state"], "reason": util.clip(c.get("reason"), 120)}
                                  for c in candidates][:20]},
        "jobs": [{"job": j["job"], "day": j["day"], "state": j["state"], "attempts": j["attempts"], "error": util.clip(j.get("error"), 300)}
                 for j in jobs],
        "runs": _runs(store, since),
        "watchers": _watchers(store, since),
        "judge_samples": [{"id": f"V{s['id']}", "person": s.get("person_name"), "organization": s.get("organization"),
                           "signal": s["signal"], "confidence": s.get("confidence"), "summary": util.clip(s.get("summary"), 240),
                           "reason": util.clip(s.get("reason"), 300), "category": s.get("category"), "eligible": s.get("eligible"),
                           "flags": s.get("flags"), "prompt_version": s.get("prompt_version") or "", "events": s.get("events")}
                          for s in store.judge_samples(since=since, limit=15)],
        "ranker": _ranker(store, since),
        "spend": _spend(store, config),
        "score": holdout_score(store),
        "unused_rules": unused_rules(store, config),
        "proposals": [{"id": int(p["id"]), "skill": p["skill"], "setting": p["setting"], "op": p["op"], "state": p["state"],
                       "text": util.clip((p.get("value") or {}).get("text"), 200), "decided_by": p.get("decided_by") or ""}
                      for p in store.recent_proposals(since=util.ago(days=7), limit=30)],
        "engineer": {"tasks": engineer.tasks(store, "all", limit=15)},
        "current": current_setup(store, config),
    }
    return packet


def quiet(packet: Mapping[str, Any]) -> bool:
    """Nothing happened that the maintainer could learn from: no call is worth paying for."""
    return not ((packet.get("reviews") or {}).get("total") or (packet.get("issues") or {}).get("total")
                or packet.get("judge_samples") or (packet.get("candidates") or {}).get("total")
                or any(j["state"] == "failed" for j in packet.get("jobs") or [])
                or any(r["routine"] != ROUTINE for r in packet.get("runs") or []))


# -- the one model call -------------------------------------------------------------------------

def instructions(store: Any) -> str:
    template = prompts.path_of("skills", PROMPT).read_text(encoding="utf-8")
    text = prompts.render(template, {"PROPOSALS_PER_DAY": PROPOSALS_PER_DAY, "ISSUES_PER_DAY": ISSUES_PER_DAY,
                                     "MIN_EVIDENCE": MIN_EVIDENCE, "REVERT_POINTS": int(REVERT_POINTS),
                                     "SKILLS": ", ".join(SKILLS), "RULE_CHARS": RULE_CHARS,
                                     "SKILL_SETTINGS": "; ".join(f"{k}: {', '.join(v) or 'none (OTHER or CODE only)'}"
                                                                 for k, v in SETTINGS_FOR.items())})
    rules = store.get_setting(RULES_KEY, []) if store is not None else []
    if rules:
        text += "\n\n# Learned rules (versioned; a person applied each)\n" + "\n".join(f"- {r}" for r in rules)
    return text


def seed(packet: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in packet.items() if k != "desk"}


def ask(store: Any, config: Any, env: Mapping[str, str], packet: Mapping[str, Any], *, budget: float) -> dict[str, Any] | None:
    spec = routines.spec_for(ROUTINE, config, instructions=instructions(store), schema=SCHEMA, budget=loop.Budget(limit=budget),
                             effort="high", max_tokens=MAX_TOKENS, final_call=False)
    run = routines.run_routine(store, spec, seed(packet), subject=str(packet.get("day") or ""), env=env)
    return run.answer if isinstance(run.answer, dict) else None


# -- proposals: written, never applied here ----------------------------------------------------------

def setting_key(skill: str, setting: str) -> str:
    return f"{skill.upper()}:{setting}"


def _pair(text: str, sep: str) -> tuple[str, str]:
    left, found, right = text.partition(sep)
    if not found or not left.strip() or not right.strip():
        raise ValueError(f"written '{'old rule => new rule' if sep == '=>' else 'name = value'}'")
    return left.strip(), right.strip()


def changed_value(current: Any, setting: str, op: str, text: str) -> Any:
    """The setting's new value after one change, or ValueError saying why it cannot be made."""
    text = " ".join(str(text or "").split())
    if setting == "RULES":
        rules = [str(r) for r in (current or [])]
        if op == "add":
            if text in rules:
                raise ValueError("that rule is already in place")
            if len(rules) >= MAX_RULES:
                raise ValueError(f"{MAX_RULES} rules at most; replace one instead")
            return [*rules, text]
        if op == "remove":
            if text not in rules:
                raise ValueError("there is no such rule to remove")
            return [r for r in rules if r != text]
        if op == "replace":
            old, new = _pair(text, "=>")
            if old not in rules:
                raise ValueError("the rule to replace is not there")
            if new in rules:
                raise ValueError("the new rule is already in place")
            return [new if r == old else r for r in rules]
        raise ValueError("a rule is added, removed or replaced")
    if setting == "FLOORS":
        values = dict(current or {})
        if op == "remove":
            if text not in values:
                raise ValueError(f"no floor named {text}")
            values.pop(text)
            return values
        if op != "set":
            raise ValueError("a floor is set ('name = value') or removed ('name')")
        name, raw = _pair(text, "=")
        try:
            number: Any = float(raw)
        except ValueError:
            number = raw  # a level such as name_level_min = exact; the setting's owner validates it
        if values.get(name) == number:
            raise ValueError("that is already the floor")
        return {**values, name: number}
    if setting == "ALIASES":
        values = dict(current or {})
        spelling, name = _pair(text, "=")
        key = spelling.casefold()
        if op == "add":
            if values.get(key) == name:
                raise ValueError("that alias is already in place")
            return {**values, key: name}
        if op == "remove":
            if key not in values:
                raise ValueError("there is no such alias")
            values.pop(key)
            return values
        raise ValueError("an alias is added or removed ('spelling = Name')")
    raise ValueError(f"{setting} is not a versioned setting")


def _empty(setting: str) -> Any:
    return [] if setting == "RULES" else {}


def current_value(store: Any, key: str, setting: str) -> Any:
    """A setting's value now. The enricher's tunables carry defaults of their own; a floor
    stored as a bare number is the confidence floor."""
    if key.startswith("ENRICHMENT:"):
        try:
            from tailwatch.enrich import settings as tunables

            return tunables.get(store, key)
        except (ImportError, ValueError):
            pass
    value = store.get_setting(key, None)
    if value is None:
        return _empty(setting)
    if setting == "FLOORS" and isinstance(value, (int, float)):
        return {"confidence": float(value)}
    return value


def validated(key: str, value: Any) -> Any:
    """The value as it will be stored; ValueError when the owner of the setting refuses it."""
    if key.startswith("ENRICHMENT:"):
        try:
            from tailwatch.enrich import settings as tunables
        except ImportError:
            return value
        return tunables.validate(key, value)
    return value


def check_proposal(store: Any, change: Mapping[str, Any], packet: Mapping[str, Any], *, known: set[str], stale: set[str],
                   obvious: set[str], waiting: list[Mapping[str, Any]]) -> str:
    """Why a proposal may not be written ('' when it may)."""
    skill, setting, op = str(change.get("skill") or ""), str(change.get("setting") or ""), str(change.get("op") or "")
    value = " ".join(str(change.get("value") or "").split())
    if skill not in SKILLS:
        return f"unknown skill {skill}"
    if setting not in SETTINGS:
        return f"unknown setting {setting}"
    if setting in VERSIONED and setting not in SETTINGS_FOR[skill]:
        if skill == "judge":
            return "a judge mistake is fixed in the judge's prompt or the pack's rules: propose OTHER naming the paragraph"
        return f"{skill} has no {setting}"
    if not value:
        return "say what should change"
    if looks_secret(value):
        return "the value looks like it holds a secret"
    key = setting_key(skill, setting)
    unused_removal = setting == "RULES" and op == "remove" and any(
        u["key"] == key and u["rule"] == value for u in packet.get("unused_rules") or [])
    if not unused_removal:
        cited = {norm_id(i) for i in change.get("evidence") or []} & known
        if cited and cited <= stale:
            return "its evidence was all judged under an older judge prompt, which the current one may already fix"
        need = MIN_EVIDENCE_OBVIOUS if cited & obvious else MIN_EVIDENCE
        if len(cited) < need:
            return f"a proposal cites at least {need} of the day's ids" + (" (one when graded an obvious mistake)" if need > 1 else "")
    if setting == "RULES":
        if op not in ("add", "remove", "replace"):
            return "a rule is added, removed or replaced"
        if op == "replace":
            try:
                texts = [_pair(value, "=>")[1]]
            except ValueError:
                return "a replace is written 'old rule => new rule'"
        else:
            texts = [] if op == "remove" else [value]
        for text in texts:
            why = scan_text(text)
            if why:
                return why
    if setting in VERSIONED:
        try:
            validated(key, changed_value(current_value(store, key, setting), setting, op, value))
        except ValueError as error:
            return str(error)
    elif len(value) < 12:
        return "a code change says what to change and why, in a sentence or two"
    for other in waiting:
        if (other.get("skill"), other.get("setting"), other.get("op")) == (skill, setting, op) and \
                " ".join(str((other.get("value") or {}).get("text") or "").split()) == value:
            return f"already proposed as #P{other['id']}"
    return ""


def propose(store: Any, config: Any, answer: Mapping[str, Any], packet: Mapping[str, Any], *,
            dry_run: bool = False) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Every proposal the limits allow is written as proposed; the rest is reported with why."""
    known, stale = known_ids(packet), stale_ids(packet)
    obvious = obvious_ids(answer, known, stale)
    made_today = store.proposals_by_since(by="maintainer", since=day_start(config))
    waiting = store.proposals(state="proposed", limit=200)
    written: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    for change in answer.get("proposals") or []:
        if not isinstance(change, Mapping):
            continue
        summary = {k: change.get(k) for k in ("skill", "setting", "op", "value")}
        why = check_proposal(store, change, packet, known=known, stale=stale, obvious=obvious, waiting=waiting)
        if not why and made_today + len(written) >= PROPOSALS_PER_DAY:
            why = f"{PROPOSALS_PER_DAY} proposals a day"
        if why:
            refused.append({**summary, "why": why})
            continue
        skill, setting = str(change["skill"]), str(change["setting"])
        if setting == "CODE" and skill == "engineer":
            skill = "desk"  # skill engineer with setting CODE is how an engineer task is stored
        value = {"text": " ".join(str(change["value"]).split())}
        if setting in VERSIONED:
            value["key"] = setting_key(skill, setting)
        evidence = [norm_id(i) for i in change.get("evidence") or []][:12]
        pid = None if dry_run else store.add_proposal(setting, skill=skill, op=str(change["op"]), value=value,
                                                      rationale=util.clip(change.get("rationale"), 600), evidence=evidence,
                                                      urgent=bool(change.get("urgent")), by="maintainer")
        written.append({"id": pid, **summary, "skill": skill, "urgent": bool(change.get("urgent"))})
        waiting.append({"id": pid, "skill": skill, "setting": setting, "op": change["op"], "value": value})
    return written, refused


def _playbooks() -> set[str] | None:
    """The custodian's playbook names, or None when the playbooks are not installed."""
    try:
        from tailwatch.desk import playbooks
    except ImportError:
        return None
    names = getattr(playbooks, "KNOWN", None)
    return set(names) if names else None


def subject_args(store: Any, subject_key: str) -> dict[str, int] | None:
    """The ids a subject key names ('person:12', 'source:7', 'review:9'), or None when it names
    no row (a playbook could only crash on it)."""
    kind, _, raw = str(subject_key or "").partition(":")
    if kind not in ISSUE_SUBJECTS or not raw.strip().isdigit():
        return None
    number = int(raw.strip())
    if kind == "review":
        review = store.review(number)
        return {"review_id": number, "person_id": int(review["person_id"])} if review else None
    row = store.person(number) if kind == "person" else store.source(number)
    if not row:
        return None
    return {"person_id": number} if kind == "person" else {"source_id": number, "person_id": int(row["person_id"])}


def open_issues(store: Any, config: Any, answer: Mapping[str, Any], *, dry_run: bool = False) -> list[dict[str, Any]]:
    """The issues the maintainer opens for the custodian, ten a day at most."""
    opened = store.issues_opened(found_by="maintainer", since=day_start(config))
    playbooks = _playbooks()
    out: list[dict[str, Any]] = []
    for item in answer.get("issues") or []:
        if not isinstance(item, Mapping):
            continue
        if opened + sum(1 for o in out if o.get("created")) >= ISSUES_PER_DAY:
            out.append({"kind": item.get("kind"), "subject_key": item.get("subject_key"), "skipped": f"{ISSUES_PER_DAY} issues a day"})
            continue
        kind = util.clip(" ".join(str(item.get("kind") or "other").split()), 60)
        subject_key = " ".join(str(item.get("subject_key") or "").split())
        args = subject_args(store, subject_key)
        if args is None:
            out.append({"kind": kind, "subject_key": subject_key, "skipped": "the subject names no row"})
            continue
        playbook = str(item.get("playbook") or "")
        if playbooks is not None and playbook not in playbooks:
            playbook = ""
        if dry_run:
            out.append({"kind": kind, "subject_key": subject_key, "playbook": playbook, "created": False, "dry_run": True})
            continue
        try:
            issue_id, created = store.open_issue(kind, subject_key, playbook=playbook, args=args, found_by="maintainer",
                                                 note=util.clip(item.get("note"), 600), review_id=args.get("review_id"))
        except RuntimeError as error:  # a flood: refuse the rest
            out.append({"kind": kind, "subject_key": subject_key, "skipped": str(error)})
            break
        out.append({"id": issue_id, "kind": kind, "subject_key": subject_key, "playbook": playbook, "created": created})
    return out


# -- reverts: proposed, never made by code ---------------------------------------------------------

def _points(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number * 100 if abs(number) <= 1 else number


def score_drop(before: Mapping[str, Any], after: Mapping[str, Any]) -> str:
    """How the holdout score got worse by REVERT_POINTS or more ('' when it did not)."""
    for name, worse_when_lower in (("score", True), ("recall", True), ("accuracy", True), ("false_pass", False)):
        was, now = _points(before.get(name)), _points(after.get(name))
        if was is None or now is None:
            continue
        fall = (was - now) if worse_when_lower else (now - was)
        if fall >= REVERT_POINTS:
            return f"holdout {name.replace('_', ' ')} {was:.0f} to {now:.0f}"
    return ""


def suggest_reverts(store: Any, config: Any, packet: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Changes applied in the last two days whose holdout score fell by ten points since: each
    proposed for revert once (a REVERT proposal a person applies). Nothing is reverted here."""
    score = packet.get("score")
    labelled = (score or {}).get("n", (score or {}).get("labelled"))
    if not score or (labelled is not None and int(labelled) < MIN_LABELLED):
        return []
    targets = {int((p.get("value") or {}).get("target") or 0) for p in store.proposals(state="all", setting="REVERT", limit=500)}
    out = []
    for proposal in store.applied_since(since=util.ago(days=REVERT_WINDOW_DAYS)):
        applied = (proposal.get("value") or {}).get("applied") or {}
        if proposal["setting"] not in VERSIONED or int(proposal["id"]) in targets or not applied.get("score"):
            continue
        why = score_drop(applied["score"], score)
        if not why:
            continue
        value = {"target": int(proposal["id"]), "key": applied["key"], "version": applied["version"], "why": why,
                 "text": f"revert #P{proposal['id']} ({applied['key']} version {applied['version']})"}
        pid = store.add_proposal("REVERT", skill=proposal["skill"], op="revert", value=value, evidence=[f"P{proposal['id']}"],
                                 rationale=f"the holdout score fell since #P{proposal['id']} was applied: {why}", by="maintainer")
        out.append({"id": pid, "target": int(proposal["id"]), "why": why})
    return out


# -- a person's decision ------------------------------------------------------------------------------

def decide(store: Any, config: Any, env: Mapping[str, str], proposal_id: int, action: str, *, note: str = "",
           by: str) -> dict[str, Any]:
    """A person's apply, reject, revert or close of #P (or #T for close). The permission check
    (a person on the team list) is the calling tool's."""
    from tailwatch.desk import engineer

    action = str(action or "").strip().lower()
    proposal = store.proposal(int(proposal_id))
    if proposal is None:
        return {"error": f"no proposal #P{proposal_id}"}
    pid = int(proposal["id"])
    if engineer.is_task(proposal):
        if action in ("close", "reject"):
            return engineer.close_task(store, config, env, pid, reason=note, by=by)
        return {"error": f"#T{pid} is an engineer task: its pull request is merged on GitHub; close T{pid}: why drops it"}
    if action == "close":
        return {"error": f"#P{pid} is a proposal: apply P{pid} or reject P{pid}: why"}
    if action == "apply":
        return _apply(store, config, env, proposal, by=by, note=note)
    if action == "reject":
        if not str(note or "").strip():
            return {"error": f"say why: reject P{pid}: why"}
        if not store.decide_proposal(pid, "rejected", by=by, note=note, expect="proposed"):
            return {"error": f"#P{pid} is {proposal['state']}, not waiting"}
        return {"proposal": pid, "state": "rejected", "note": note}
    if action == "revert":
        return _revert(store, proposal, by=by, note=note)
    return {"error": "the decision is apply, reject, revert or close"}


def _apply(store: Any, config: Any, env: Mapping[str, str], proposal: Mapping[str, Any], *, by: str, note: str) -> dict[str, Any]:
    from tailwatch.desk import engineer

    pid, setting = int(proposal["id"]), str(proposal["setting"])
    value = dict(proposal.get("value") or {})
    if proposal["state"] != "proposed":
        return {"error": f"#P{pid} is {proposal['state']}, not waiting"}
    if setting == "REVERT":
        target = store.proposal(int(value.get("target") or 0))
        if target is None:
            return {"error": f"#P{pid} names no proposal to revert"}
        done = _revert(store, target, by=by, note=note or f"#P{pid}")
        if done.get("error"):
            return done
        store.decide_proposal(pid, "applied", by=by, note=note, expect="proposed")
        return {**done, "proposal": pid, "reverted": int(target["id"])}
    if setting in TASK_SETTINGS:
        text = f"{value.get('text') or ''} (proposal #P{pid}: {proposal.get('rationale') or 'no rationale given'})"
        started = engineer.start_task(store, config, env, text, by=by, thread=str(proposal.get("thread") or ""), source=pid)
        if started.get("error"):
            return started
        store.decide_proposal(pid, "applied", by=by, note=note, expect="proposed", value={**value, "task": started["task"]})
        return {"proposal": pid, "state": "applied", "task": started["task"], "parked": started.get("parked"),
                "message": started["message"]}
    key = str(value.get("key") or setting_key(str(proposal["skill"]), setting))
    try:
        new = validated(key, changed_value(current_value(store, key, setting), setting, str(proposal["op"]),
                                           str(value.get("text") or "")))
    except ValueError as error:
        return {"error": f"#P{pid} cannot be applied: {error}"}
    if setting == "RULES":
        for rule in new:
            why = scan_text(rule)
            if why:
                return {"error": f"#P{pid} cannot be applied: {why}"}
    score = holdout_score(store)
    version = store.set_setting(key, new, by=by, note=util.clip(f"#P{pid} applied: {proposal.get('rationale') or ''}", 500))
    applied = {"key": key, "version": version, "score": score, "at": util.iso()}
    if not store.decide_proposal(pid, "applied", by=by, note=note, expect="proposed", value={**value, "applied": applied}):
        return {"error": f"#P{pid} was decided by someone else meanwhile"}
    return {"proposal": pid, "state": "applied", "key": key, "version": version, "value": new}


def _revert(store: Any, proposal: Mapping[str, Any], *, by: str, note: str) -> dict[str, Any]:
    pid = int(proposal["id"])
    value = dict(proposal.get("value") or {})
    applied = value.get("applied") or {}
    if proposal["state"] != "applied" or not applied.get("key"):
        return {"error": f"#P{pid} is {proposal['state']}; only an applied change to a setting is reverted"}
    key, version = str(applied["key"]), int(applied["version"])
    try:
        new_version = store.revert_setting(key, version, by=by)
    except ValueError:  # the change was the setting's first value: reverting it empties the setting
        new_version = store.set_setting(key, _empty(str(proposal["setting"])), by=by, note=f"revert of version {version}")
    store.decide_proposal(pid, "reverted", by=by, note=note, expect="applied",
                          value={**value, "reverted": {"version": new_version, "at": util.iso()}})
    return {"proposal": pid, "state": "reverted", "key": key, "version": new_version}


# -- the day --------------------------------------------------------------------------------------------

def settle_shipped(store: Any, config: Any, env: Mapping[str, str]) -> dict[str, list[int]]:
    if not config.engineer.repo or not env.get("GITHUB_TOKEN"):
        return {"merged": [], "closed": []}
    from tailwatch.desk import engineer

    return engineer.settle(store, config, env)


def review_day(store: Any, config: Any, env: Mapping[str, str], packet: Mapping[str, Any], *,
               dry_run: bool = False) -> dict[str, Any]:
    """The model's answer on the packet and what the limits let through. Spends, within the cap."""
    cap = float(config.budgets.maintainer_daily)
    spent = store.spend_today((ROUTINE,), config.schedule.timezone)
    out: dict[str, Any] = {"answer": None, "written": [], "refused": [], "issues": []}
    if quiet(packet):
        out["quiet"] = True
        return out
    if spent >= cap - 0.001:
        out["skipped"] = f"the maintainer's daily budget (${cap:.2f}) is spent"
        return out
    try:
        answer = ask(store, config, env, packet, budget=cap - spent)
    except loop.LoopError as error:
        out["error"] = util.clip(error, 300)
        return out
    out["answer"] = answer
    if answer:
        out["written"], out["refused"] = propose(store, config, answer, packet, dry_run=dry_run)
        out["issues"] = open_issues(store, config, answer, dry_run=dry_run)
    return out


def run_daily(store: Any, config: Any, env: Mapping[str, str]) -> dict[str, Any]:
    """The scheduler's maintenance job."""
    expired = store.expire_proposals()
    settled = settle_shipped(store, config, env)
    packet = gather(store, config, env)
    reverts = suggest_reverts(store, config, packet)
    reviewed = review_day(store, config, env, packet)
    outcome = {k: v for k, v in reviewed.items() if k != "answer"} | {"expired": expired, "settled": settled, "reverts": reverts}
    posted = note.post_note(store, config, env, packet=packet, answer=reviewed.get("answer"), outcome=outcome)
    answer = reviewed.get("answer") or {}
    return {"day": packet["day"], "expired": expired, "settled": settled, "reverts": [r["id"] for r in reverts],
            "proposed": [w["id"] for w in reviewed["written"]], "refused": reviewed["refused"],
            "issues": [i.get("id") for i in reviewed["issues"] if i.get("created")],
            "summary": answer.get("summary") or "", "needs_owner": answer.get("needs_owner") or [],
            **{k: reviewed[k] for k in ("quiet", "skipped", "error") if k in reviewed},
            "note": posted.get("posted") or "", "note_already": posted.get("already") or ""}


def preview(store: Any, config: Any, env: Mapping[str, str]) -> dict[str, Any]:
    """What the maintainer would say and propose today. Nothing is written or posted; the model
    call is paid for and logged."""
    packet = gather(store, config, env)
    reviewed = review_day(store, config, env, packet, dry_run=True)
    outcome = {k: v for k, v in reviewed.items() if k != "answer"}
    return {"summary": (reviewed.get("answer") or {}).get("summary") or "", "would_propose": reviewed["written"],
            "refused": reviewed["refused"], "would_open": reviewed["issues"],
            **{k: reviewed[k] for k in ("quiet", "skipped", "error") if k in reviewed},
            "note": note.render({**packet, "desk": note.desk_state(store, config)}, reviewed.get("answer"), outcome=outcome)}
