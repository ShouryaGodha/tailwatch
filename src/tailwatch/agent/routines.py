"""Routines: the repeated jobs that run through the one loop. A routine is a prompt, a set of
tools (or none), an answer schema and the caps. ``run_routine`` opens a row in the run log,
runs the loop, and finishes the row with what it cost, also when it fails.

The code around a routine (what to feed in, what to do with the answer) lives with its owner:
the judge in ``pipeline.judge``, the editor in ``desk.editor``, the enricher in ``enrich.agent``,
the maintainer in ``desk.maintainer``, the ranker in ``pipeline.ranker``. What they share is
here, so every model call in tailwatch is logged and priced the same way.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from tailwatch.agent import loop

ROUTINES = {
    # name: (model role in tailwatch.yaml, effort, step cap, time cap seconds)
    "judge": ("judge", "medium", 1, 180.0),
    "verify_card": ("editor", "medium", 10, 180.0),
    "enrich_person": ("enricher", "medium", 12, 240.0),
    "identify_mention": ("identifier", "low", 1, 120.0),
    "noise_check": ("identifier", "low", 1, 120.0),
    "rank_cards": ("ranker", "medium", 1, 180.0),
    "daily_review": ("maintainer", "high", 1, 600.0),
    "desk": ("desk", "medium", 24, 300.0),
    "channel": ("desk", "medium", 16, 300.0),
    "engineer": ("engineer", "high", 60, 1800.0),
}


def spec_for(name: str, config: Any, *, instructions: str, tools: Any = None, schema: Mapping[str, Any] | None = None,
             budget: loop.Budget | None = None, **overrides: Any) -> loop.Spec:
    role, effort, steps, seconds = ROUTINES[name]
    model = config.models.for_(role) if config is not None else loop.model_client.DEFAULT_MODEL
    values: dict[str, Any] = {"name": name, "model": model, "instructions": instructions, "schema": schema,
                              "schema_name": name, "step_cap": steps, "time_cap_s": seconds, "effort": effort, "budget": budget,
                              "prices": dict(getattr(config, "pricing", {}) or {})}
    if tools is not None:
        values["tools"] = tools
    values.update(overrides)
    return loop.Spec(**values)


def run_routine(store: Any, spec: loop.Spec, seed: Any, *, subject: str = "", env: Mapping[str, str] | None = None,
                execute: Callable[[str, dict[str, Any]], Any] | None = None, tool_usd: Callable[[], float] | None = None) -> loop.Run:
    """Run one routine and log it. ``seed`` is the first user message (JSON-encoded when not a
    str). The answer, the trace and the spend are on the run row whatever happens."""
    import json

    run_id = store.open_run(spec.name, subject=subject, model=spec.model) if store is not None else None
    content = seed if isinstance(seed, str) else json.dumps(seed, default=str, ensure_ascii=False)
    try:
        result = loop.run(spec, [{"role": "user", "content": content}], execute=execute, env=env)
    except Exception as error:
        if run_id is not None:
            store.finish_run(run_id, loop.failed_run(error), state="failed", error=str(error),
                             tool_usd=tool_usd() if tool_usd else 0.0)
        raise
    if run_id is not None:
        state = "capped" if result.stopped and result.answer is None else "done"
        store.finish_run(run_id, result, state=state, answer=result.answer, tool_usd=tool_usd() if tool_usd else 0.0)
    return result
