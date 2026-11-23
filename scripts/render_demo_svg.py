"""Render `tailwatch demo` to docs/assets/demo.svg for the README: python scripts/render_demo_svg.py"""

from __future__ import annotations

import io
from pathlib import Path

from rich.console import Console
from rich.terminal_theme import MONOKAI

from tailwatch.demo import run_demo

console = Console(record=True, width=118, file=io.StringIO())
run_demo(console=console)
target = Path(__file__).resolve().parent.parent / "docs" / "assets" / "demo.svg"
target.write_text(console.export_svg(title="tailwatch demo", theme=MONOKAI), encoding="utf-8")
print(f"wrote {target}")
