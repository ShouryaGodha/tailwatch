"""Posting to the desk: the desk channel in Slack when one is set, else the terminal and a
Markdown log next to the digests. Every desk post (the desk note, an enrichment notice, an
engineer's pull request line) goes through ``post_desk``; ``say_once`` adds the once-a-day rule."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tailwatch import util

_poster: Any = None  # tests install a recorder with set_poster


def set_poster(poster: Any) -> None:
    """Install ``poster(text, thread) -> id`` in place of Slack and the terminal (tests, the demo)."""
    global _poster
    _poster = poster


def post_desk(store: Any, config: Any, env: Mapping[str, str], text: str, *, thread: str = "", tag: str = "") -> str:
    body = f"{text}\n_{tag}_" if tag else text
    if _poster is not None:
        return str(_poster(body, thread) or "")
    channel = config.delivery.slack.desk_channel
    if env.get("SLACK_BOT_TOKEN") and channel:
        from tailwatch.delivery.slack import SlackSink

        try:
            return SlackSink(env["SLACK_BOT_TOKEN"], channel).post(body, thread_ts=thread)
        except Exception as error:  # the desk must never break a job; the log keeps the text
            body = f"(Slack refused: {error})\n{body}"
    folder = config.path(config.delivery.markdown_dir or "digests")
    folder.mkdir(parents=True, exist_ok=True)
    path = Path(folder) / f"desk-{util.local_day(config.schedule.timezone)}.md"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n---\n{util.iso()[:16]}Z{f' (thread {thread})' if thread else ''}\n\n{body}\n")
    if config.delivery.console:
        from rich.console import Console

        Console().print(f"[magenta]desk[/magenta] {body}")
    return f"file:{path}"


def say_once(store: Any, config: Any, env: Mapping[str, str], topic: str, text: str, *, tag: str = "") -> bool:
    if not store.notice_once(f"desk:{topic}", util.local_day(config.schedule.timezone)):
        return False
    post_desk(store, config, env, text, tag=tag)
    return True
