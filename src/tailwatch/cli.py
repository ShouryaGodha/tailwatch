"""The ``tailwatch`` command."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from tailwatch import __version__

app = typer.Typer(add_completion=False, no_args_is_help=True, rich_markup_mode="rich",
                  help="tailwatch: track any set of people across the web, judge what changed, get one digest a day.")
db_app = typer.Typer(no_args_is_help=True, help="The database.")
app.add_typer(db_app, name="db")
console = Console()


class Session:
    def __init__(self, config_path: str | None = None) -> None:
        from tailwatch import config as config_module
        from tailwatch.store import open_store

        self.config = config_module.load(config_path)
        self.env = config_module.load_env(Path(self.config.base_dir) / ".env")
        self.store = open_store(self.config.database, self.config)


_config_option = typer.Option(None, "--config", "-c", help="Path to tailwatch.yaml (default: search upwards).")


def session(config_path: str | None) -> Session:
    try:
        return Session(config_path)
    except Exception as error:
        console.print(f"[red]Could not load the configuration:[/red] {error}")
        raise typer.Exit(2) from error


def call(target: str, *args: Any, **kwargs: Any) -> Any:
    module, _, name = target.partition(":")
    try:
        fn = getattr(importlib.import_module(module), name)
    except (ImportError, AttributeError) as error:
        console.print(f"[red]{target} is not available:[/red] {error}")
        raise typer.Exit(1) from error
    return fn(*args, **kwargs)


def show(value: Any) -> None:
    if isinstance(value, str):
        console.print(value)
    else:
        console.print_json(json.dumps(value, default=str))


@app.command()
def version() -> None:
    """Print the version."""
    console.print(f"tailwatch {__version__}")


@app.command()
def init(preset: str = typer.Option("ai-researchers", help="ai-researchers, oss-maintainers or founders."),
         folder: Path = typer.Argument(Path("."), help="Where to write tailwatch.yaml.")) -> None:
    """Write tailwatch.yaml, .env and a starter watchlist from a preset."""
    from tailwatch.agent import prompts

    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "tailwatch.yaml"
    if target.exists():
        console.print(f"[yellow]{target} exists; leaving it as it is.[/yellow]")
    else:
        source = prompts.path_of("presets", f"{preset}.yaml")
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        console.print(f"[green]Wrote {target}[/green] from the {preset} preset.")
    csv_source = prompts.path_of("presets", f"{preset}.csv")
    if csv_source.exists() and not (folder / "watchlist.csv").exists():
        (folder / "watchlist.csv").write_text(csv_source.read_text(encoding="utf-8"), encoding="utf-8")
        console.print(f"[green]Wrote {folder / 'watchlist.csv'}[/green]: edit it to list the people you want to follow.")
    env = folder / ".env"
    if not env.exists():
        env.write_text(prompts.path_of("presets", "env.example").read_text(encoding="utf-8"), encoding="utf-8")
        console.print(f"[green]Wrote {env}[/green]: add ANTHROPIC_API_KEY (or another provider's key).")
    console.print("\nNext: [bold]tailwatch doctor[/bold], then [bold]tailwatch check[/bold] and [bold]tailwatch digest[/bold].")


@app.command()
def demo(keep: bool = typer.Option(False, help="Keep the demo database and digests in ./tailwatch-demo.")) -> None:
    """Run the whole pipeline on bundled fixtures: no keys, no network, no cost."""
    from tailwatch.demo import run_demo

    run_demo(console=console, keep=keep)


@app.command()
def doctor(config: str = _config_option) -> None:
    """Check the configuration, keys, connectors and database."""
    from tailwatch.connectors.registry import status
    from tailwatch.pipeline import packs

    s = session(config)
    console.print(f"[bold]{s.config.name}[/bold] · database {s.config.database} · signals {s.config.signals}")
    pack = packs.load(s.config.signals)
    console.print(f"Signal pack [cyan]{pack.name}[/cyan]: {', '.join(pack.names)} · prompt {pack.prompt_version}")
    provider = s.config.models.default.split(":")[0]
    key_name = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "azure": "AZURE_OPENAI_API_KEY",
                "openrouter": "OPENROUTER_API_KEY"}.get(provider)
    if key_name and not s.env.get(key_name):
        console.print(f"[red]✗ {key_name} is not set[/red]: the judge cannot run (model {s.config.models.default}).")
    else:
        console.print(f"[green]✓[/green] model {s.config.models.default}")
    table = Table("connector", "type", "cost", "state")
    for row in status(s.config, s.env):
        state = "[green]on[/green]" if row["enabled"] and not row["missing"] else \
            f"[yellow]needs {', '.join(row['missing'])}[/yellow]" if row["enabled"] else "[dim]off[/dim]"
        table.add_row(row["kind"], row["type"], "paid" if row["paid"] else "free", state)
    console.print(table)
    slack = s.config.delivery.slack
    console.print(f"Slack: {'on' if s.env.get('SLACK_BOT_TOKEN') and slack.digest_channel else 'off'} · "
                  f"console {'on' if s.config.delivery.console else 'off'} · markdown {s.config.delivery.markdown_dir or 'off'}")
    console.print(f"People: {s.store.count_people() or 'none yet'}")


@db_app.command("migrate")
def db_migrate(config: str = _config_option) -> None:
    """Apply database migrations."""
    from tailwatch.store import Database

    s = session(config)
    applied = Database(s.config.database).migrate()
    console.print(f"Applied: {', '.join(applied) or 'nothing (up to date)'}")


@app.command()
def sync(config: str = _config_option) -> None:
    """Add everyone tailwatch.yaml lists (inline and imported files) and create their sources."""
    from tailwatch.connectors.registry import enabled_checkers
    from tailwatch.pipeline.ingest import sync_watchlist

    s = session(config)
    show(sync_watchlist(s.store, s.config))
    show(s.store.sync_sources(enabled_checkers(s.config, s.env)))


@app.command()
def add(links: list[str] = typer.Argument(..., help="One or more profile URLs or @handles of one person."),
        name: str = typer.Option(..., "--name", "-n"), organization: str = typer.Option("", "--org"),
        title: str = typer.Option(""), tags: str = typer.Option("", help="Comma-separated."),
        config: str = _config_option) -> None:
    """Add one person."""
    from tailwatch import links as link_rules

    s = session(config)
    by_kind: dict[str, str] = {}
    for value in links:
        by_kind.setdefault(link_rules.guess_kind(value), value)
    person_id, created = s.store.add_person(name, organization=organization, title=title, links=by_kind,
                                            tags=[t.strip() for t in tags.split(",") if t.strip()], added_because="cli")
    console.print(f"{'Added' if created else 'Already listed as'} #{person_id} {name}: {', '.join(by_kind)}")


@app.command("import")
def import_(path: Path, config: str = _config_option) -> None:
    """Import people from a CSV or YAML file."""
    from tailwatch.pipeline.ingest import add_entries, entries_from_file

    s = session(config)
    show(add_entries(s.store, entries_from_file(path), because=f"import: {path.name}"))


@app.command()
def people(status: str = typer.Option("tracked"), config: str = _config_option) -> None:
    """List people."""
    s = session(config)
    table = Table("id", "name", "organization", "title", "links", "status")
    for p in s.store.people(status=None if status == "all" else status):
        table.add_row(str(p["id"]), p["name"], p["organization"], p["title"], ", ".join(p["links"]), p["status"])
    console.print(table)


@app.command()
def person(query: str, config: str = _config_option) -> None:
    """Everything about one person: sources, signals, changes."""
    s = session(config)
    found = s.store.find_people(query, limit=1)
    if not found:
        console.print("Nobody found.")
        raise typer.Exit(1)
    p = found[0]
    console.print(f"[bold]#{p['id']} {p['name']}[/bold] · {p['title']} at {p['organization']} · {p['status']}")
    for source in s.store.sources_of(p["id"]):
        console.print(f"  {source['kind']:<18} {source['status']:<12} last {str(source['last_checked_at'] or 'never')[:16]}  {source['url']}")
    for v in s.store.verdicts_of(p["id"], limit=15):
        console.print(f"  {v['created_at'][:10]} {v['kind']:<12} [bold]{v['signal']}[/bold] {v['summary']} [dim]({v['judged_by']})[/dim]")


@app.command()
def check(kind: list[str] = typer.Option(None, "--kind", "-k", help="Only these connectors."),
          limit: int = typer.Option(0, help="At most this many sources per connector."),
          no_judge: bool = typer.Option(False, help="Record changes without judging them."),
          config: str = _config_option) -> None:
    """Run a source pass now: check every due source, then judge what changed."""
    from tailwatch.pipeline.ingest import sync_watchlist
    from tailwatch.pipeline.runner import run_pass

    s = session(config)
    sync_watchlist(s.store, s.config)
    result = run_pass(s.store, s.config, s.env, kinds=list(kind or []) or None, limit=limit or None, judge=not no_judge,
                      log=console.print)
    show({"usd": result["usd"], "outages": result["outages"], "judge": result.get("judge")})


@app.command()
def watch(config: str = _config_option) -> None:
    """Read the configured feeds once."""
    from tailwatch.pipeline.watch import watch_feeds

    s = session(config)
    show(watch_feeds(s.store, s.config, s.env, log=console.print))


@app.command()
def judge(config: str = _config_option) -> None:
    """Judge every check waiting for the judge."""
    from tailwatch.pipeline.judge import judge_pending

    s = session(config)
    show(judge_pending(s.store, s.config, s.env, log=console.print))


@app.command()
def digest(preview: bool = typer.Option(True, "--preview/--post", help="Preview (default) or post and record receipts."),
           config: str = _config_option) -> None:
    """Build the digest; --post delivers it (once a day) and records what it carried."""
    from tailwatch.delivery.local import ConsoleSink
    from tailwatch.pipeline import digest as digest_module

    s = session(config)
    if preview:
        ConsoleSink(console).deliver(digest_module.build(s.store, s.config, s.env), "preview")
    else:
        show(digest_module.post_morning(s.store, s.config, s.env, console=console))


@app.command()
def coverage(config: str = _config_option) -> None:
    """Who is actually being watched."""
    from tailwatch.pipeline import coverage as coverage_module

    s = session(config)
    console.print(coverage_module.render(coverage_module.report(s.store)))


@app.command()
def runs(routine: str = typer.Option(""), limit: int = 20, config: str = _config_option) -> None:
    """The run log: every model run with its tokens and real cost."""
    s = session(config)
    table = Table("id", "routine", "subject", "state", "model", "calls", "tokens in/out", "cost", "started")
    for r in s.store.runs(routine=routine, limit=limit):
        table.add_row(str(r["id"]), r["routine"], (r["subject"] or "")[:28], r["state"] + (f" ({r['stopped']})" if r["stopped"] else ""),
                      r["model"] or "", str(r["model_calls"]), f"{r['tokens_in']}/{r['tokens_out']}",
                      f"${r['cost_usd'] + r['tool_usd']:.4f}" + ("" if r["priced"] else "*"), str(r["started_at"])[:16])
    console.print(table)
    console.print(f"Spent today: ${s.store.spend_today(zone=s.config.schedule.timezone):.4f} (* = model not in the price table)")


@app.command()
def run(run_id: int, config: str = _config_option) -> None:
    """One run in full: its trace, answer and cost."""
    show(session(config).store.run(run_id))


@app.command()
def tick(only: list[str] = typer.Option(None, "--only", help="Run only these jobs."), config: str = _config_option) -> None:
    """Run one scheduler tick (for cron or a CI schedule)."""
    from tailwatch.scheduler import tick as tick_once

    s = session(config)
    show(tick_once(s.store, s.config, s.env, log=console.print, only=tuple(only or ())))


@app.command()
def up(config: str = _config_option, slack: bool = typer.Option(True, help="Also answer in Slack (Socket Mode).")) -> None:
    """Run forever: the scheduler, and the Slack bot when SLACK_APP_TOKEN is set."""
    import threading

    from tailwatch.scheduler import run_forever

    s = session(config)
    if slack and s.env.get("SLACK_APP_TOKEN") and s.env.get("SLACK_BOT_TOKEN"):
        from tailwatch.slack_app import serve

        threading.Thread(target=serve, args=(config,), daemon=True).start()
        console.print("Slack bot connected (Socket Mode).")
    console.print(f"tailwatch is running: a tick every {s.config.schedule.tick_minutes} minutes, "
                  f"digest at {s.config.schedule.digest_time} {s.config.schedule.timezone}. Ctrl-C to stop.")
    run_forever(s.store, s.config, s.env, log=console.print)


@app.command()
def ask(message: str = typer.Argument(..., help="A question or an instruction for the desk."),
        thread: str = typer.Option("cli", help="Keep a conversation going by reusing a thread name."),
        config: str = _config_option) -> None:
    """Talk to the desk agent from the terminal (the same agent as the Slack desk)."""
    import uuid

    s = session(config)
    result = call("tailwatch.desk.chat:respond", s.store, s.config, s.env, text=message, user="local", channel="cli",
                  thread=thread, message_key=f"cli:{uuid.uuid4().hex}")
    console.print(result.get("answer") or "[dim](no reply)[/dim]")


@app.command()
def slack(config: str = _config_option) -> None:
    """Run only the Slack bot (Socket Mode)."""
    from tailwatch.slack_app import serve

    serve(config)


@app.command()
def mcp(config: str = _config_option) -> None:
    """Serve tailwatch's tools over MCP (stdio) for Claude Code, Claude Desktop or Cursor."""
    from tailwatch.mcp_server import serve

    serve(config)


@app.command()
def verify(config: str = _config_option) -> None:
    """Run the editor over the cards the next digest could list."""
    s = session(config)
    show(call("tailwatch.desk.verify:verify_cards", s.store, s.config, s.env))


@app.command()
def maintain(config: str = _config_option) -> None:
    """Run the maintainer's daily review now and post the desk note."""
    s = session(config)
    show(call("tailwatch.desk.maintainer:run_daily", s.store, s.config, s.env))


@app.command()
def engineer(task: str, config: str = _config_option) -> None:
    """Ask the engineer for a code change (a draft pull request). Needs switches.engineer: true."""
    s = session(config)
    show(call("tailwatch.desk.engineer:start_task", s.store, s.config, s.env, task, by="local"))


@app.command()
def score(config: str = _config_option) -> None:
    """The editor's score on the labelled cards (recall, false pass, hold rate)."""
    s = session(config)
    show(call("tailwatch.desk.verify:score", s.store))


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
