"""The demo is the quickest proof that the whole pipeline works: keep it green."""

from __future__ import annotations

import io

from rich.console import Console

from tailwatch.demo import run_demo


def test_demo_runs_end_to_end_with_no_keys() -> None:
    out = io.StringIO()
    result = run_demo(console=Console(file=out, width=140))
    text = out.getvalue()
    assert result["people"] == 3
    assert result["counts"]["gated"] == 1 and result["counts"]["signals"] == 4
    assert "Ada Quill" in text and "Ben Okafor" in text and "Felix Moreau" in text
    assert "Posting again: already sent" in text
    assert 0 < result["spend"] < 1
