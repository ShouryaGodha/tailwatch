"""Sinks that need no account: the terminal and a Markdown file per digest."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tailwatch.delivery.base import render


class ConsoleSink:
    name = "console"

    def __init__(self, console: Any = None) -> None:
        from rich.console import Console

        self.console = console or Console()

    def deliver(self, payload: Mapping[str, Any], message_key: str) -> str:
        from rich.markdown import Markdown
        from rich.panel import Panel

        self.console.print(Panel(Markdown(render(payload, "markdown").replace("\n", "  \n")), border_style="cyan",
                                 title="tailwatch", title_align="left"))
        return f"console:{message_key}"

    def find(self, message_key: str) -> str | None:
        return None

    def notice(self, text: str) -> None:
        self.console.print(f"[yellow]⚠ {text}[/yellow]")


class MarkdownSink:
    name = "markdown"

    def __init__(self, folder: str | Path) -> None:
        self.folder = Path(folder)

    def path_for(self, payload: Mapping[str, Any]) -> Path:
        return self.folder / f"{payload.get('day') or 'digest'}.md"

    def deliver(self, payload: Mapping[str, Any], message_key: str) -> str:
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.path_for(payload)
        path.write_text(render(payload, "markdown") + f"\n\n<!-- tailwatch:{message_key} -->\n", encoding="utf-8")
        return str(path)

    def find(self, message_key: str) -> str | None:
        if not self.folder.exists():
            return None
        for path in self.folder.glob("*.md"):
            if f"tailwatch:{message_key}" in path.read_text(encoding="utf-8"):
                return str(path)
        return None

    def notice(self, text: str) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        with (self.folder / "notices.log").open("a", encoding="utf-8") as handle:
            handle.write(text + "\n")
