"""The one agent loop. Every routine that lets the model use tools (the desk, the editor, the
enricher) and every one that does not (the mention identifier, the noise check, the maintainer's
review, the ranker) runs through ``run``:

- one set of stop rules, checked before every model call and every tool: steps, a deadline, a
  real-cost budget, a repeated call, an alternation between two calls, an error streak, and a
  tool that raises ``ToolStop`` (no key, rate limited, over budget);
- one way to wrap outside text: a tool flagged ``evidence`` returns its result inside
  ``<evidence>`` tags with any closing tag in the text defanged, and every prompt says that
  evidence is data, never instructions;
- one output cap per tool result and one cap on what the run keeps (oldest results trimmed);
- writes memoised by operation hash, so a resumed run never writes twice;
- a checkpoint after every turn and every memoised write, when the caller asks for one;
- a final answer-only call when a routine that must answer is stopped;
- a failed run keeps what it spent (``failed_run``), so the run log never says $0 for calls
  that were paid for.

What the loop does not decide: what the tools do, who may use a writing tool (the registry's
permission check) and what a stop means to the caller.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from tailwatch.agent import model as model_client
from tailwatch.agent.types import ModelError, Reply, Request

REPEAT_WARN = 2
REPEAT_STOP = 4
ALTERNATION_WINDOW = 6
STUCK_NOTE = "you already called this with these arguments; the earlier result stands"
TRIMMED = "[an earlier result, trimmed: it was read when it arrived]"
WORKER_OUTPUT_CAP = 30_000
CHAT_OUTPUT_CAP = 45_000
KEPT_CAP = 150_000
FINAL_LINE = "STOP: {why}. Answer now from what you have; no more tools."
STOP_NAMES = {"TIME": "out of time", "STEPS": "step limit", "BUDGET": "budget", "STUCK": "repeating the same step",
              "ERRORS": "tools kept failing"}
_CLOSE_EVIDENCE = re.compile(r"</\s*evidence", re.IGNORECASE)


class LoopError(RuntimeError):
    """The model did not answer: an error, neither an answer nor a call, or an answer that is
    not the JSON object the routine asked for."""


class ToolStop(Exception):  # noqa: N818 - a signal, not an error
    """A tool that must end the run: ``code`` is NEED_KEY, RATE_LIMITED, BUDGET or similar."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


class Budget:
    """A USD ledger shared by tools and the model: ``limit`` is the cap for the run,
    ``spent`` what tools have charged so far (the loop adds the model's real cost)."""

    def __init__(self, limit: float, spent: float = 0.0) -> None:
        self.limit = limit
        self.spent = spent

    def charge(self, usd: float) -> None:
        self.spent = round(self.spent + usd, 6)


def failed_run(error: BaseException) -> Run | None:
    found = getattr(error, "run", None)
    return found if isinstance(found, Run) else None


def stuck(hashes: list[str]) -> str:
    """'' | 'warn' | 'stop' for the call whose hash is last in ``hashes``."""
    if not hashes:
        return ""
    last = hashes[-1]
    repeats = sum(1 for h in hashes if h == last)
    if repeats >= REPEAT_STOP:
        return "stop"
    if len(hashes) >= ALTERNATION_WINDOW and len(set(hashes[-ALTERNATION_WINDOW:])) <= 2:
        return "stop"
    return "warn" if repeats >= REPEAT_WARN else ""


def as_evidence(name: str, result: Any, cap: int = CHAT_OUTPUT_CAP) -> str:
    text = json.dumps(result, default=str)[:cap]
    return f'<evidence source="tool:{name}">{_CLOSE_EVIDENCE.sub("<∕evidence", text)}</evidence>'


def op_hash(name: str, args: Mapping[str, Any]) -> str:
    return hashlib.sha256((name + json.dumps(args, sort_keys=True, default=str)).encode()).hexdigest()


def is_error(result: Any) -> bool:
    return isinstance(result, dict) and bool(result.get("error"))


@dataclass(frozen=True)
class StepTools:
    specs: list[dict[str, Any]]
    mutating: frozenset[str] = frozenset()
    memo_errors: frozenset[str] = frozenset()
    evidence: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Spec:
    """What one routine or chat mode is: the prompt, the tools, the answer's shape and the caps.
    ``tools`` is a ``StepTools`` or a callable ``(run) -> StepTools`` for the desk, whose tools
    change as skills are loaded."""

    name: str
    model: str = model_client.DEFAULT_MODEL
    instructions: str = ""
    tools: Any = StepTools(specs=[])
    schema: Mapping[str, Any] | None = None
    schema_name: str = ""
    step_cap: int = 10
    time_cap_s: float = 150.0
    budget: Budget | None = None
    output_cap: int = WORKER_OUTPUT_CAP
    kept_cap: int = KEPT_CAP
    free_tools: frozenset[str] = frozenset()
    repeat_stop: bool = True
    error_stop: int = 0
    final_call: bool = True
    effort: str = "medium"
    max_tokens: int = 16000
    checkpoint: Callable[[Run], None] | None = None
    prices: Mapping[str, tuple[float, float, float]] | None = None


@dataclass
class Run:
    """One run's state: what resumes (messages, memo, hashes, loaded skills, steps) and what the
    run reports (trace, tokens, cost, answer, how it stopped)."""

    messages: list[dict[str, Any]] = field(default_factory=list)
    memo: dict[str, Any] = field(default_factory=dict)
    hashes: list[str] = field(default_factory=list)
    loaded: list[str] = field(default_factory=list)
    errors_in_row: int = 0
    steps: int = 0
    model_calls: int = 0
    tokens_in: int = 0
    tokens_cached: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    priced: bool = True
    model: str = ""
    trace: list[dict[str, Any]] = field(default_factory=list)
    answer: Any = None
    stopped: str = ""
    last_error: str = ""

    def account(self, reply: Reply) -> None:
        self.model_calls += 1
        self.tokens_in += reply.tokens_in + reply.tokens_cache_write
        self.tokens_cached += reply.tokens_cached
        self.tokens_out += reply.tokens_out
        self.cost_usd = round(self.cost_usd + reply.cost_usd, 6)
        self.priced = self.priced and reply.priced
        self.model = reply.model or self.model

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Run:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


def parse_answer(spec: Spec, text: str) -> Any:
    if spec.schema is None:
        return text
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except (TypeError, ValueError) as error:
        raise LoopError("the model answered with something other than the JSON object") from error


def trim(messages: list[dict[str, Any]], cap: int) -> int:
    """Keep the run's tool results under ``cap`` characters by stubbing the oldest; never removes
    an item (a tool result answers a call id)."""
    def size() -> int:
        return sum(len(str(m.get("content") or "")) for m in messages if m.get("role") == "tool")

    trimmed = 0
    for message in messages:
        if size() <= cap:
            break
        if message.get("role") == "tool" and len(str(message.get("content") or "")) > len(TRIMMED):
            message["content"] = TRIMMED
            trimmed += 1
    return trimmed


Execute = Callable[[str, dict[str, Any]], Any]


def run(spec: Spec, messages: list[dict[str, Any]] | None = None, *, execute: Execute | None = None,
        env: Mapping[str, str] | None = None, state: Run | None = None, clock: Callable[[], float] = time.monotonic,
        log: Callable[[str], None] | None = None) -> Run:
    """Run the routine from ``messages`` (or resume ``state``) until it answers or a cap stops
    it. ``execute(name, args)`` runs one tool and returns its result; it may raise ``ToolStop``.
    Returns the Run: ``answer`` is the text or the parsed JSON object."""
    this = state if state is not None else Run(messages=list(messages or []))
    try:
        return _run(spec, this, execute=execute or _no_tools, env=env, clock=clock, log=log or (lambda _m: None))
    except Exception as error:
        try:
            error.run = this  # type: ignore[attr-defined]
        except AttributeError:
            pass
        raise


def _no_tools(name: str, _args: dict[str, Any]) -> Any:
    return {"error": f"no tool named {name}"}


def _run(spec: Spec, this: Run, *, execute: Execute, env: Mapping[str, str] | None,
         clock: Callable[[], float], log: Callable[[str], None]) -> Run:
    start = clock()
    budget = spec.budget

    def out_of_time() -> bool:
        return clock() - start > spec.time_cap_s

    def checkpoint() -> None:
        if spec.checkpoint is not None:
            spec.checkpoint(this)

    def step_tools() -> StepTools:
        return spec.tools(this) if callable(spec.tools) else spec.tools

    def ask(tools: StepTools, *, final: str = "") -> Reply:
        messages = this.messages + ([{"role": "user", "content": FINAL_LINE.format(why=final)}] if final else [])
        request = Request(model=spec.model, system=spec.instructions, messages=messages, tools=list(tools.specs),
                          schema=dict(spec.schema) if spec.schema is not None else None,
                          schema_name=spec.schema_name or spec.name, effort=spec.effort, max_tokens=spec.max_tokens,
                          timeout_s=max(30.0, spec.time_cap_s))
        try:
            reply = model_client.call(request, env, prices=spec.prices)
        except ModelError as error:
            raise LoopError(f"the model did not answer: {error}") from error
        this.account(reply)
        return reply

    while this.answer is None and not this.stopped:
        if out_of_time():
            this.stopped = "TIME"
            break
        if this.steps >= spec.step_cap:
            this.stopped = "STEPS"
            break
        if budget is not None and budget.spent + this.cost_usd >= budget.limit - 1e-9:
            this.stopped = "BUDGET"
            break
        tools = step_tools()
        reply = ask(tools)
        if not reply.tool_calls:
            if not reply.text.strip():
                raise LoopError("the model returned neither an answer nor a tool call")
            this.answer = parse_answer(spec, reply.text)
            break
        this.messages.append(reply.as_message())
        if not all(call.name in spec.free_tools for call in reply.tool_calls):
            this.steps += 1
        for call in reply.tool_calls:
            name, args = call.name, call.args if isinstance(call.args, dict) else {}
            op = op_hash(name, args)
            verdict = ""
            if not this.stopped and not out_of_time():
                this.hashes.append(op)
                verdict = stuck(this.hashes) if spec.repeat_stop else ""
                if verdict == "stop":
                    this.stopped = "STUCK"
            if this.stopped or out_of_time():
                this.stopped = this.stopped or "TIME"
                this.messages.append({"role": "tool", "tool_call_id": call.id, "name": name, "is_error": True,
                                      "content": json.dumps({"error": f"{this.stopped}: {STOP_NAMES.get(this.stopped, this.stopped)}; no more tools"})})
                continue
            mutates = name in tools.mutating
            if mutates and op in this.memo:
                result = this.memo[op]
            else:
                try:
                    result = execute(name, args)
                except ToolStop as stop:
                    result = {"error": f"{stop.code}: {stop}", "stop": stop.code}
                    this.stopped = stop.code
                    this.last_error = f"{stop.code}: {stop}"
                except (ValueError, KeyError, TypeError) as error:
                    result = {"error": str(error)}
                except Exception as error:  # the model is told what failed
                    log(f"{spec.name}: tool {name} failed: {error!r}")
                    result = {"error": f"{name} failed: {type(error).__name__}: {str(error)[:300]}"}
                if mutates and (not is_error(result) or name in tools.memo_errors):
                    this.memo[op] = result
                    checkpoint()
            failed = is_error(result)
            this.errors_in_row = this.errors_in_row + 1 if failed else 0
            if failed:
                this.last_error = str(result.get("error"))[:300]
            if verdict == "warn" and isinstance(result, dict):
                result = {**result, "note": STUCK_NOTE}
            this.trace.append({"tool": name, "args": args, "ok": not failed,
                               "result": json.dumps(result, default=str)[:2000]})
            text = as_evidence(name, result, spec.output_cap) if name in tools.evidence \
                else json.dumps(result, default=str)[:spec.output_cap]
            this.messages.append({"role": "tool", "tool_call_id": call.id, "name": name, "content": text, "is_error": failed})
            if spec.error_stop and this.errors_in_row >= spec.error_stop:
                this.stopped = "ERRORS"
        trim(this.messages, spec.kept_cap)
        checkpoint()
    if this.answer is None and this.stopped and spec.final_call:
        reply = ask(step_tools(), final=STOP_NAMES.get(this.stopped, this.stopped))
        if not reply.text.strip():
            raise LoopError(f"the routine stopped ({this.stopped}) and the model gave no answer")
        this.answer = parse_answer(spec, reply.text)
    checkpoint()
    return this


def one_call(spec: Spec, seed: Any, *, env: Mapping[str, str] | None = None) -> Run:
    """A routine with no tools: one strict call over ``seed`` (JSON-encoded when not a str)."""
    content = seed if isinstance(seed, str) else json.dumps(seed, default=str)
    return run(spec, [{"role": "user", "content": content}], env=env)
