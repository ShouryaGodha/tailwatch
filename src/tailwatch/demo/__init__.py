"""``tailwatch demo``: the whole pipeline on a simulated world, with no keys, no network and no
cost.

The world is two days of public pages for six fictional people at fictional organizations.
The real connectors (GitHub, homepage, arXiv, Hacker News) read it through recorded HTTP
answers, and a scripted model stands in for Claude. Everything else is the production code:
the store, the runner, the judge's gates and guards, the feed matching, the ranker's fallback,
the durable digest, the run log. The model's token counts and costs are simulated at Claude
Opus 5.5 list prices, so the run log shows what a real day would cost.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any

from tailwatch import config as config_module
from tailwatch import util
from tailwatch.agent import model as model_client
from tailwatch.agent.providers.scripted import ScriptedAdapter
from tailwatch.agent.types import Reply, Request
from tailwatch.connectors.http import Call, Response

DAY_ONE = "2026-11-20T02:00:00Z"
GITHUB = "https://api.github.com"
DEMO_MODEL = "scripted:demo-claude"
PRICE = (4.0, 0.2, 20.0)  # Claude Opus 5.5 list prices, USD per million tokens

PEOPLE = [
    {"name": "Ada Quill", "organization": "Helios Labs", "title": "Research Scientist",
     "links": {"github": "https://github.com/adaquill", "homepage": "https://adaquill.example"}, "tags": ["interpretability"]},
    {"name": "Ben Okafor", "organization": "Northwind AI", "title": "ML Engineer",
     "links": {"github": "https://github.com/bokafor", "homepage": "https://benokafor.example"}},
    {"name": "Chen Wei", "organization": "Quanta Research", "title": "Researcher",
     "links": {"arxiv": "Wei, Chen", "homepage": "https://chenwei.example"}},
    {"name": "Dara Singh", "organization": "Helios Labs", "title": "Member of Technical Staff",
     "links": {"github": "https://github.com/darasingh"}},
    {"name": "Elena Petrova", "organization": "Orbit Robotics", "title": "Staff Engineer",
     "links": {"homepage": "https://elenap.example", "github": "https://github.com/elenap"}},
    {"name": "Felix Moreau", "organization": "Northwind AI", "title": "Research Engineer",
     "links": {"homepage": "https://felixmoreau.example"}},
]

GITHUB_USERS = {
    "adaquill": [
        {"login": "adaquill", "name": "Ada Quill", "company": "@helios-labs", "bio": "Interpretability research at Helios Labs.",
         "location": "London", "blog": "https://adaquill.example", "hireable": False, "public_repos": 12},
        {"login": "adaquill", "name": "Ada Quill", "company": "", "bio": "Building something new. Previously interpretability at Helios Labs.",
         "location": "London", "blog": "https://adaquill.example", "hireable": False, "public_repos": 13},
    ],
    "bokafor": [{"login": "bokafor", "name": "Ben Okafor", "company": "Northwind AI", "bio": "ML infra.", "hireable": False, "public_repos": 30}] * 2,
    "darasingh": [
        {"login": "darasingh", "name": "Dara Singh", "company": "Helios Labs", "bio": "Training systems.", "hireable": False, "public_repos": 8},
        {"login": "darasingh", "name": "Dara Singh", "company": "Helios Labs", "bio": "Training systems.", "hireable": True, "public_repos": 8},
    ],
    "elenap": [{"login": "elenap", "name": "Elena Petrova", "company": "Orbit Robotics", "bio": "Robots.", "public_repos": 5}] * 2,
}
NEW_REPO = {"name": "sparse-probe", "description": "Sparse probing for transformer circuits", "created_at": "2026-11-20T18:00:00Z",
            "html_url": "https://github.com/adaquill/sparse-probe", "stargazers_count": 41, "fork": False}

PAGES = {
    "https://adaquill.example": [
        "<html><head><title>Ada Quill</title></head><body><h1>Ada Quill</h1><p>I am a research scientist at Helios Labs, "
        "where I work on interpretability of large language models.</p><p>Before Helios I did my PhD at the University of Edinburgh.</p></body></html>",
        "<html><head><title>Ada Quill</title></head><body><h1>Ada Quill</h1><p>In November 2026 I left Helios Labs after four years "
        "to start a new company focused on interpretability tooling.</p><p>Before Helios I did my PhD at the University of Edinburgh.</p></body></html>",
    ],
    "https://benokafor.example": [
        "<html><head><title>Ben Okafor</title></head><body><p>I build ML infrastructure at Northwind AI: training clusters and evaluation pipelines.</p></body></html>",
        "<html><head><title>Ben Okafor</title></head><body><p>I build ML infrastructure at Northwind AI: training clusters and evaluation pipelines.</p>"
        "<p>After three great years I am looking for my next role, starting in January. If your team trains large models, I would love to talk.</p></body></html>",
    ],
    "https://chenwei.example": ["<html><head><title>Chen Wei</title></head><body><p>Researcher at Quanta Research working on sparse attention.</p></body></html>"] * 2,
    "https://elenap.example": ["<html><head><title>Elena Petrova</title></head><body><p>Staff engineer at Orbit Robotics, working on manipulation.</p></body></html>"] * 2,
    "https://felixmoreau.example": ["<html><head><title>Felix Moreau</title></head><body><p>Research engineer at Northwind AI.</p></body></html>"] * 2,
}


def _atom(papers: list[tuple[str, str, str]]) -> str:
    entries = "".join(
        f"<entry><id>http://arxiv.org/abs/{pid}v1</id><published>{date}T00:00:00Z</published><updated>{date}T00:00:00Z</updated>"
        f"<title>{title}</title><summary>An abstract.</summary><author><name>Chen Wei</name></author>"
        f"<author><name>Mara Lind</name></author><link href=\"http://arxiv.org/abs/{pid}v1\" rel=\"alternate\" type=\"text/html\"/></entry>"
        for pid, title, date in papers)
    return f'<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom">{entries}</feed>'


ARXIV = [
    _atom([("2609.01234", "Sparse Attention for Long Video", "2026-09-02")]),
    _atom([("2611.04567", "Routing Tokens Across Experts at Scale", "2026-11-20"),
           ("2609.01234", "Sparse Attention for Long Video", "2026-09-02")]),
]

HN_MARKER = 1763600000
HN_HITS = [
    {"objectID": "48100001", "created_at_i": HN_MARKER + 3600, "created_at": "2026-11-20T10:00:00Z", "author": "newsbot",
     "title": "Felix Moreau leaves Northwind AI to join Orbit Robotics as head of research", "url": "https://news.example/felix-moreau"},
    {"objectID": "48100002", "created_at_i": HN_MARKER + 7200, "created_at": "2026-11-20T12:00:00Z", "author": "newsbot",
     "title": "Priya Raman, formerly of Helios Labs, joins Quanta Research", "url": "https://news.example/priya-raman"},
]


class World:
    """The simulated web: answers every request the connectors make, day by day."""

    def __init__(self) -> None:
        self.day = 0
        self.requests = 0

    def __call__(self, call: Call, _timeout: float) -> Response:
        self.requests += 1
        url = call.url
        if url.startswith(f"{GITHUB}/users/"):
            parts = url.removeprefix(f"{GITHUB}/users/").split("/")
            login = parts[0]
            versions = GITHUB_USERS.get(login)
            if versions is None:
                return Response(404, json.dumps({"message": "Not Found"}), url=url)
            if len(parts) > 1:
                repos = [NEW_REPO] if login == "adaquill" and self.day == 1 else []
                return Response(200, json.dumps(repos), url=url)
            return Response(200, json.dumps(versions[min(self.day, len(versions) - 1)]), url=url)
        if url.startswith("http://export.arxiv.org/api/query"):
            return Response(200, ARXIV[self.day], url=url)
        if url.startswith("https://hn.algolia.com/"):
            hits = HN_HITS if self.day == 1 else []
            return Response(200, json.dumps({"hits": hits}), url=url)
        base = url.rstrip("/")
        if base in PAGES:
            return Response(200, PAGES[base][self.day], headers={"content-type": "text/html"}, url=url)
        return Response(404, "not found", url=url)


# -- the scripted model ------------------------------------------------------------------------

def _tokens(request: Request, out: int) -> tuple[int, int]:
    size = len(request.system) + sum(len(str(m.get("content") or "")) for m in request.messages)
    return max(400, size // 4), out


def _judge(seed: dict[str, Any]) -> dict[str, Any]:
    person = seed["person"]
    text = " ".join(f"{c['label']} {c['after']}" for c in seed["change"]["changes"]).lower()
    base = {"confidence": 0.9, "wrong_person": False, "category": "researcher", "eligible": True, "priority": 70,
            "priority_reason": "", "signal_date": "", "same_as": 0, "supersedes": [], "flags": [],
            "now": {"organization": person["organization"], "title": person["title"], "since": ""}}
    earlier = [a["id"] for a in seed.get("prior_alerts") or [] if a["signal"] == "left" and not a["delivered"]]
    if "left helios labs" in text or "start a new company" in text:
        return {**base, "signal": "left", "confidence": 0.95, "signal_date": "2026-11", "supersedes": earlier,
                "summary": "Homepage says she left Helios Labs to start a company",
                "context": "Research Scientist at Helios Labs, 4 yrs, before a PhD at Edinburgh",
                "reason": "Her own homepage now says she left Helios Labs in November 2026 after four years to start a company. "
                          "GitHub agrees: the company field was cleared and the bio says she is building something new.",
                "now": {"organization": "Stealth startup", "title": "Founder", "since": "2026-11"}, "priority": 92}
    if "building something new" in text:
        return {**base, "signal": "left", "confidence": 0.7, "summary": "GitHub bio says she is building something new",
                "context": "Research Scientist at Helios Labs", "priority": 80,
                "reason": "The company field was cleared and the bio now says she is building something new, previously at "
                          "Helios Labs. Her own words, but no date and no destination yet.",
                "now": {"organization": "", "title": "", "since": ""}}
    if "looking for my next role" in text:
        return {**base, "signal": "looking", "category": "engineer", "signal_date": "2026-11",
                "summary": "Homepage says he is looking for his next role from January",
                "context": "ML Engineer at Northwind AI, 3 yrs, training infrastructure",
                "reason": "A new paragraph in his own words: after three years he is looking for his next role, starting in "
                          "January, and invites teams that train large models to talk.", "priority": 78}
    if "joins orbit robotics" in text or "leaves northwind" in text:
        return {**base, "signal": "left", "category": "engineer", "signal_date": "2026-11-20",
                "summary": "Hacker News report: leaving Northwind AI for Orbit Robotics",
                "context": "Research Engineer at Northwind AI",
                "reason": "A news story names him and says he leaves Northwind AI to lead research at Orbit Robotics. "
                          "It is a report, not his own words; his homepage still lists Northwind, which is usual right after a move.",
                "now": {"organization": "Orbit Robotics", "title": "Head of Research", "since": "2026-11"}, "priority": 75,
                "confidence": 0.8}
    if "new paper" in text:
        return {**base, "signal": "none", "summary": "A new arXiv paper, ordinary research activity",
                "context": "Researcher at Quanta Research", "reason": "A new preprint is work, not a move or a search."}
    return {**base, "signal": "none", "summary": "No move in this change", "context": "", "reason": "Nothing points to a move."}


def _desk(request: Request) -> Reply:
    """The desk agent asked "why is Ada on the digest?": look the evidence up, then answer from it."""
    from tailwatch.agent.types import ToolCall

    results = [m for m in request.messages if m.get("role") == "tool"]
    if not results:
        return Reply(tool_calls=[ToolCall(id="call_why", name="why", args={"person": "Ada Quill"})],
                     tokens_in=_tokens(request, 60)[0], tokens_out=60)
    text = str(results[-1].get("content") or "")
    confidence = re.search(r'"confidence": ([0-9.]+)', text)
    answer = ("Ada Quill is on today's digest as *left*. Her own homepage changed overnight to say: \"In November 2026 I "
              "left Helios Labs after four years to start a new company.\" GitHub agrees (company cleared, bio \"building "
              "something new\"). That weaker GitHub signal was superseded by the homepage one, so she gets one card"
              + (f"; the judge's confidence is {confidence.group(1)}." if confidence else "."))
    return Reply(text=answer, tokens_in=_tokens(request, 180)[0], tokens_out=180)


def _maintainer(packet: dict[str, Any], request: Request) -> Reply:
    """The maintainer reads the day once: grades the judge, proposes one ranking rule with two of
    the day's ids as evidence, applies nothing."""
    samples = packet.get("judge_samples") or []
    by_signal = {s.get("signal"): s for s in samples}
    people = [f"person:{r['person_id']}" for r in packet.get("ranker") or [] if r.get("person_id")][:2]
    grades = [{"agent": "judge", "subject": str(s["id"]), "verdict": "right", "kind": "pattern",
               "why": "the label follows the person's own words" if s.get("signal") != "none" else "ordinary activity is none"}
              for s in samples[:4]]
    proposals = []
    if len(people) == 2:
        proposals.append({"skill": "ranking", "setting": "RULES", "op": "add", "urgent": False, "evidence": people,
                          "value": "A departure to found a company ranks above a reported move to an announced employer.",
                          "rationale": "Both ranked people left this week; the founder is reachable, the other has started a new job."})
    answer = {"summary": f"A quiet first day: {len(samples)} checks judged, "
                         f"{'a departure and a search' if 'left' in by_signal and 'looking' in by_signal else 'a few signals'} "
                         "found, every source read, spend well under budget.",
              "lessons": [{"lesson": "A homepage sentence in the person's own words is the strongest evidence of a move.",
                           "count": 1, "example_ids": [str(by_signal['left']['id'])] if "left" in by_signal else []}],
              "grades": grades, "proposals": proposals, "issues": [], "needs_owner": []}
    return Reply(text=json.dumps(answer), tokens_in=_tokens(request, 900)[0], tokens_out=900)


def brain(request: Request) -> Reply:
    if request.schema_name in ("desk", "channel"):
        return _desk(request)
    if request.schema_name == "daily_review":
        try:
            return _maintainer(json.loads(str(request.messages[0].get("content") or "{}")), request)
        except ValueError:
            pass
    seed: Any = None
    try:
        seed = json.loads(str(request.messages[0].get("content") or ""))
    except (ValueError, IndexError):
        pass
    if request.schema_name == "judge" and isinstance(seed, dict):
        answer: Any = _judge(seed)
        tokens = _tokens(request, 420)
    elif request.schema_name == "rank_cards" and isinstance(seed, list):
        order = sorted(seed, key=lambda p: -int(p["signal"]["priority"]))
        why = {"left": "Left a top lab to found a company; reachable now as a possible co-founder or early hire.",
               "looking": "Says in his own words he is looking from January; three years of ML infra at Northwind AI."}
        answer = {"ranked": [{"person_id": p["person_id"], "score": 95 - 10 * i, "worth": "top" if i == 0 else "later",
                              "strength": "strong", "why": why.get(p["signal"]["label"], "") if "report" not in p["signal"]["summary"].lower()
                              else "Reported move from Northwind AI to lead research at Orbit Robotics; a destination already taken."}
                             for i, p in enumerate(order)]}
        tokens = _tokens(request, 380)
    else:
        answer = {} if request.schema is not None else "ok"
        tokens = _tokens(request, 50)
    text = json.dumps(answer) if not isinstance(answer, str) else answer
    return Reply(text=text, tokens_in=tokens[0], tokens_out=tokens[1])


# -- the run -------------------------------------------------------------------------------------

def demo_config(folder: Path) -> config_module.Config:
    return config_module.load(None, overrides={
        "name": "AI researchers (demo)", "base_dir": str(folder), "database": f"sqlite:///{folder / 'tailwatch.db'}",
        "signals": "career-moves", "models": {"default": DEMO_MODEL},
        "priority": {"tiers": [{"name": "frontier labs", "orgs": ["Helios Labs", "Quanta Research"]},
                               {"name": "notable", "orgs": ["Northwind AI", "Orbit Robotics"]}]},
        "watchlist": {"people": PEOPLE},
        "connectors": {"bluesky": {"enabled": False}, "semantic_scholar": {"enabled": False}, "rss": {"enabled": False}},
        "feeds": [{"kind": "hackernews", "name": "hn-moves", "options": {"query": "joins OR leaves"}}],
        "delivery": {"console": False, "markdown_dir": "digests"},
        "switches": {"verify": "off", "rank": True, "engineer": False, "candidates": True},
        "pricing": {DEMO_MODEL: PRICE},
        "schedule": {"timezone": "UTC"},
    })


def run_demo(*, console: Any = None, keep: bool = False, steps: set[int] | None = None) -> dict[str, Any]:
    """Run every step; print only ``steps`` (all when None)."""
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    from tailwatch.connectors.http import Http
    from tailwatch.delivery.local import ConsoleSink, MarkdownSink
    from tailwatch.pipeline import coverage, digest, ingest, judge, runner, watch
    from tailwatch.store import open_store

    real = console or Console()
    hidden = Console(file=io.StringIO())
    console = real
    folder = Path("tailwatch-demo").resolve() if keep else Path(tempfile.mkdtemp(prefix="tailwatch-demo-"))
    if keep and folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True, exist_ok=True)
    config = demo_config(folder)
    store = open_store(config.database, config)
    world = World()
    http = Http(transport=world)
    adapter = ScriptedAdapter(brain)
    previous_adapter = model_client._adapters.get("scripted")
    model_client.register_adapter("scripted", adapter)
    env: dict[str, str] = {}
    def log(_m):
        return (None)

    def step(title: str, note: str = "") -> None:
        nonlocal console
        number = int(title.split(" ", 1)[0]) if title[:1].isdigit() else 0
        console = real if steps is None or number in steps else hidden
        console.rule(f"[bold cyan]{title}")
        if note:
            console.print(f"[dim]{note}[/dim]")

    try:
        util.set_now(util.parse(DAY_ONE))
        step("1 · The watchlist", "Six fictional people at fictional organizations; links in one spelling each.")
        ingest.sync_watchlist(store, config)
        table = Table("id", "name", "organization", "title", "sources")
        for person in store.people():
            table.add_row(str(person["id"]), person["name"], person["organization"], person["title"], ", ".join(person["links"]))
        console.print(table)

        step("2 · Day one: the first pass", "The first read of a source records what is there. Changes start from the second read.")
        first = runner.run_pass(store, config, env, http=http, judge=False, log=log)
        store.set_setting(watch.marker_key("hn-moves"), str(HN_MARKER), history=False)
        checked = sum(k["checked"] for k in first["kinds"])
        console.print(f"{checked} sources read across {len(first['kinds'])} connectors "
                      f"({', '.join(k['kind'] for k in first['kinds'])}); 0 changes, as expected on a first read.")

        world.day = 1
        util.set_now(util.parse(DAY_ONE) + timedelta(days=1))
        step("3 · Day two: what changed", "Each difference between two good reads is one mechanical event. Code never decides what it means.")
        runner.run_pass(store, config, env, http=http, judge=False, log=log)
        table = Table("person", "source", "event", "after")
        for check in store.pending_checks(50):
            person = store.person(check["person_id"])
            for event in check["events"]:
                table.add_row(person["name"], check["kind"], event["label"], util.clip(event["after_text"].replace("\n", " "), 70))
        console.print(table)

        step("4 · Feeds", "A Hacker News search: a story about a tracked person becomes their check; anyone else becomes a candidate.")
        fed = watch.watch_feeds(store, config, env, http=http, judge=False, log=log)
        console.print(f"{fed['reports']} report about a tracked person (judged with the rest), "
                      f"{fed['candidates']} candidate for the enrichment pipeline.")

        step("5 · The judge", "One strict model call per check, with everything held on the person. A flag alone never reaches the model.")
        counts = judge.judge_pending(store, config, env, log=log)
        table = Table("person", "source", "signal", "summary", "judged by")
        for row in store.db.query("SELECT v.*, p.name, s.kind FROM verdicts v JOIN people p ON p.id = v.person_id "
                                  "JOIN observations o ON o.id = v.observation_id JOIN sources s ON s.id = o.source_id ORDER BY v.id"):
            color = {"left": "red", "looking": "yellow"}.get(row["signal"], "dim")
            table.add_row(row["name"], row["kind"], f"[{color}]{row['signal']}[/{color}]", util.clip(row["summary"], 60), row["judged_by"])
        console.print(table)
        console.print(f"{counts['judged']} judged by the model, {counts['gated']} by a gate with no model call, "
                      f"{counts['signals']} signals.")

        step("6 · The digest", "Ranked by one model call, one card per move, delivered once (prepared → sending → sent) with receipts.")
        result = digest.post(store, config, env, sinks=[ConsoleSink(console), MarkdownSink(config.path("digests"))])
        again = digest.post(store, config, env, sinks=[ConsoleSink(console)])
        console.print(f"Posted {result['people']} people ({result['ranked_by']} ranking). Posting again: {again['state']}.")

        step("7 · Ask the desk", "The same agent answers in Slack, in the terminal (tailwatch ask) and over MCP. It loads skills on demand.")
        console.print('[bold]> tailwatch ask "why is Ada Quill on the digest?"[/bold]')
        try:
            from tailwatch.desk.chat import respond

            reply = respond(store, config, env, text="why is Ada Quill on the digest?", user="local", channel="cli",
                            thread="demo", message_key="demo:why")
            console.print(reply.get("answer") or "(no answer)")
        except ImportError:
            console.print("[dim](the desk is not installed)[/dim]")

        step("8 · The maintainer and the desk note",
             "Once a day it reads everything, grades the agents and proposes. It applies nothing: a person replies \"apply P1\".")
        try:
            from tailwatch.desk import notices
            from tailwatch.desk.maintainer import run_daily

            notes: list[str] = []
            notices.set_poster(lambda text, thread: notes.append(text) or f"demo-note-{len(notes)}")
            try:
                daily = run_daily(store, config, env)
            finally:
                notices.set_poster(None)
            for text in notes:
                console.print(Panel(text, border_style="magenta", title="desk note", title_align="left"))
            proposal = next(iter(store.proposals(state="proposed")), None)
            if proposal:
                from tailwatch.desk.maintainer import decide

                console.print(f'[bold]> reply in the thread: "apply P{proposal["id"]}"[/bold]')
                applied = decide(store, config, env, int(proposal["id"]), "apply", by="local")
                console.print(f"Applied as a versioned setting: {applied.get('key') or applied}")
            _ = daily
        except ImportError:
            console.print("[dim](the maintainer is not installed)[/dim]")

        step("9 · Coverage and the run log", "Every model run is logged with tokens and real cost; failed runs keep what they spent.")
        console.print(coverage.render(coverage.report(store)))
        table = Table("routine", "runs", "tokens in", "tokens out", "cost")
        for row in store.db.query("SELECT routine, COUNT(*) AS n, SUM(tokens_in) AS i, SUM(tokens_out) AS o, SUM(cost_usd) AS c "
                                  "FROM runs GROUP BY routine ORDER BY routine"):
            table.add_row(row["routine"], str(row["n"]), str(row["i"]), str(row["o"]), f"${row['c']:.4f}")
        console.print(table)
        total = store.spend_since("0000")
        console.print(f"Simulated spend for the day at Claude Opus 5.5 list prices: [bold]${total:.3f}[/bold] "
                      f"· {world.requests} HTTP requests answered by the simulated web.")
        console = real
        console.rule("[bold green]Done")
        console.print("Next: [bold]tailwatch init[/bold] to start your own watchlist · docs: docs/architecture.md"
                      + (f"\nThe demo's database and digest are in {folder}" if keep else ""))
        return {"folder": str(folder), "people": result.get("people"), "spend": total, "counts": counts}
    finally:
        util.set_now(None)
        if previous_adapter is not None:
            model_client.register_adapter("scripted", previous_adapter)
        else:
            model_client._adapters.pop("scripted", None)
        if not keep:
            store.db.close()
            shutil.rmtree(folder, ignore_errors=True)


_ = re  # kept for fixtures that grow regex rules
