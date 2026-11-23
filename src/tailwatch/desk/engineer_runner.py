"""The engineer's sessions: tailwatch's own loop (``native``) with tools that work inside one
checkout, and Claude through the Claude Agent SDK (``claude``) when a Claude credential is set.

What the checkout tools guarantee, and what they do not:

- every path stays inside the checkout and out of ``.git``; writes stay under the allowed paths
  and off the protected ones (the same lists ``engineer.validate`` holds the final diff to);
- ``run`` runs only the test suite and the linter (``pytest``, ``ruff``, ``python -m pytest``),
  as an argument list with no shell, refuses network and install tools by name, and runs with a
  scrubbed environment: no keys, no tokens, a proxy that goes nowhere;
- a test the engineer writes is still code that runs: the scrubbed environment, the temporary
  checkout and the diff floors are the protection, not the command filter.

Every session returns the same shape: ``answer`` (the parsed JSON report or verdict, or None),
``cost_usd``, ``session_id`` (to continue the conversation with the test output), ``error``,
and the token counts for the run log.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tailwatch.agent import loop, routines

READ_CAP = 60_000
RUN_CAP = 12_000
RUN_TIMEOUT_S = 600
LIST_CAP = 400
SEARCH_CAP = 200
REPAIR_STEPS = 60
REVIEW_STEPS = 20
PACKET_DIR = ".engineer"
IGNORED_PARTS = (".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".venv", "node_modules")
SHELL_CHARACTERS = set(";|&`$<>\n\\")
ALLOWED_COMMANDS = (("pytest",), ("ruff",), ("python", "-m", "pytest"), ("python3", "-m", "pytest"),
                    ("python", "-m", "ruff"), ("python3", "-m", "ruff"))
DENIED_WORDS = ("curl", "wget", "ssh", "scp", "sftp", "nc", "ncat", "telnet", "ftp", "pip", "pip3", "uv", "git", "gh",
                "sudo", "rm", "chmod", "chown", "http.server", "urllib", "requests", "httpx", "socket")
SECRET_ENV_WORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "DSN", "DATABASE_URL", "CREDENTIAL", "WEBHOOK", "COOKIE")
NOWHERE_PROXY = "http://127.0.0.1:9"
CLAUDE_TOOLS = ["Read", "Edit", "Write", "Bash", "Grep", "Glob"]
CLAUDE_REVIEW_TOOLS = ["Read", "Grep", "Glob"]
CLAUDE_NEVER = ["WebFetch", "WebSearch", "Task", "Agent", "NotebookEdit"]
CLAUDE_CREDENTIALS = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")

SECRET_PATTERN = re.compile(
    r"(sk-ant-[A-Za-z0-9_-]{16,}|sk-[A-Za-z0-9]{24,}|xox[abprs]-[A-Za-z0-9-]{10,}|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|hooks\.slack\.com/services/[A-Za-z0-9/]+|postgres(?:ql)?://[^\s'\"/:@]+:[^\s'\"@]+@)")


def looks_secret(text: str) -> bool:
    """A key, a token, a private key or a connection string with a password in ``text``."""
    return bool(SECRET_PATTERN.search(str(text or "")))


REPORT_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["outcome", "title", "summary", "user_message", "question", "learned"],
    "properties": {"outcome": {"type": "string", "enum": ["fixed", "no_change", "needs_decision", "failed"]},
                   "title": {"type": "string"}, "summary": {"type": "string"}, "user_message": {"type": "string"},
                   "question": {"type": "string"}, "learned": {"type": "string"}},
}
VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["verdict", "reasons"],
    "properties": {"verdict": {"type": "string", "enum": ["ship", "fix", "stop"]},
                   "reasons": {"type": "array", "items": {"type": "string"}}},
}


def sandbox_env(root: Path, env: Mapping[str, str] | None = None) -> dict[str, str]:
    """What a command in the checkout sees: a path, the checkout as home and import root, no
    secret of any kind, and a proxy that goes nowhere so a stray HTTP call fails fast."""
    env = env if env is not None else os.environ
    return {"PATH": str(env.get("PATH") or "/usr/local/bin:/usr/bin:/bin"), "HOME": str(root),
            "LANG": str(env.get("LANG") or "C.UTF-8"), "PYTHONPATH": str(root / "src"), "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "TAILWATCH_SANDBOX": "1",
            "HTTP_PROXY": NOWHERE_PROXY, "HTTPS_PROXY": NOWHERE_PROXY, "ALL_PROXY": NOWHERE_PROXY,
            "http_proxy": NOWHERE_PROXY, "https_proxy": NOWHERE_PROXY, "NO_PROXY": "", "no_proxy": ""}


def command_argv(command: str) -> list[str]:
    """The argument list for an allowed command, or ValueError saying why it is not run."""
    text = str(command or "").strip()
    if not text:
        raise ValueError("no command")
    if any(c in SHELL_CHARACTERS for c in text):
        raise ValueError("no shell here: one command, no pipes, redirects, ; or $")
    try:
        argv = shlex.split(text)
    except ValueError as error:
        raise ValueError(f"cannot read the command: {error}") from error
    words = {w.lower().strip("'\"") for w in argv}
    denied = sorted(words & set(DENIED_WORDS))
    if denied:
        raise ValueError(f"{', '.join(denied)} is not run here: nothing leaves the checkout and nothing is installed")
    for allowed in ALLOWED_COMMANDS:
        if tuple(argv[:len(allowed)]) == allowed:
            rest = argv[len(allowed):]
            tool = allowed[-1]
            return [sys.executable, "-m", tool, *rest]
    raise ValueError("only the tests and the linter run here: pytest, ruff, python -m pytest, python -m ruff")


def _under(path: str, prefixes: list[str] | tuple[str, ...]) -> bool:
    """A prefix ending in / is a folder; any other is one file (or a folder named so)."""
    return any(path.startswith(p) if p.endswith("/") else (path == p or path.startswith(p + "/")) for p in prefixes)


def protected(path: str, protected_paths: list[str] | tuple[str, ...]) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name == ".env" or name.startswith(".env.") or _under(path, protected_paths)


def allowed(path: str, allowed_paths: list[str] | tuple[str, ...]) -> bool:
    return _under(path, allowed_paths)


class Checkout:
    """The tools of one session over one checkout."""

    READ_TOOLS = ("read_file", "list_files", "search")
    WRITE_TOOLS = ("edit_file", "write_file", "run")

    def __init__(self, root: Path, *, allowed_paths: list[str] | tuple[str, ...], protected_paths: list[str] | tuple[str, ...],
                 review: bool = False, env: Mapping[str, str] | None = None) -> None:
        self.root = Path(root).resolve()
        self.allowed_paths = tuple(allowed_paths)
        self.protected_paths = tuple(protected_paths)
        self.review = review
        self.env = sandbox_env(self.root, env)
        self.calls: list[dict[str, Any]] = []

    @property
    def names(self) -> tuple[str, ...]:
        return self.READ_TOOLS if self.review else self.READ_TOOLS + self.WRITE_TOOLS

    # -- paths --------------------------------------------------------------------------------
    def inside(self, path: str) -> Path:
        raw = str(path or "").strip()
        target = (self.root / raw).resolve() if raw else self.root
        if target != self.root and self.root not in target.parents:
            raise ValueError(f"{raw!r} is outside the checkout")
        if ".git" in target.relative_to(self.root).parts:
            raise ValueError("the .git folder is not for reading or writing")
        return target

    def writable(self, path: str) -> Path:
        target = self.inside(path)
        rel = target.relative_to(self.root).as_posix()
        if rel.startswith(PACKET_DIR + "/") or rel == PACKET_DIR:
            raise ValueError(f"{PACKET_DIR}/ is the task's packet; it is read, never written")
        if protected(rel, self.protected_paths):
            raise ValueError(f"{rel} is protected; a task cannot change it")
        if not allowed(rel, self.allowed_paths):
            raise ValueError(f"{rel} is outside the paths a task may change ({', '.join(self.allowed_paths)})")
        return target

    # -- the tools ----------------------------------------------------------------------------
    def read_file(self, path: str, start: int = 0, end: int = 0) -> dict[str, Any]:
        target = self.inside(path)
        if not target.is_file():
            return {"error": f"no file at {path!r}"}
        try:
            lines = target.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            return {"error": f"{path!r} is not a text file"}
        first = max(int(start or 1), 1)
        last = int(end or len(lines))
        text = "\n".join(f"{n}\t{line}" for n, line in enumerate(lines[first - 1:last], start=first))
        out: dict[str, Any] = {"path": target.relative_to(self.root).as_posix(), "lines": len(lines), "text": text[:READ_CAP]}
        if len(text) > READ_CAP:
            out["note"] = f"cut at {READ_CAP} characters; read a line range"
        return out

    def _files(self, base: Path, pattern: str = "*") -> list[Path]:
        found = []
        for item in sorted(base.rglob(pattern or "*")):
            rel = item.relative_to(self.root)
            if item.is_file() and not any(part in IGNORED_PARTS for part in rel.parts):
                found.append(item)
        return found

    def list_files(self, path: str = "", pattern: str = "") -> dict[str, Any]:
        target = self.inside(path)
        if not target.is_dir():
            return {"error": f"no folder at {path!r}"}
        files = [p.relative_to(self.root).as_posix() for p in self._files(target, pattern or "*")]
        return {"files": files[:LIST_CAP], "count": len(files), **({"note": f"cut at {LIST_CAP}"} if len(files) > LIST_CAP else {})}

    def search(self, pattern: str, path: str = "") -> dict[str, Any]:
        target = self.inside(path)
        try:
            regex = re.compile(pattern)
        except re.error as error:
            return {"error": f"bad pattern: {error}"}
        matches: list[str] = []
        for item in self._files(target) if target.is_dir() else [target]:
            try:
                text = item.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    matches.append(f"{item.relative_to(self.root).as_posix()}:{number}:{line[:300]}")
                    if len(matches) > SEARCH_CAP:
                        return {"matches": matches[:SEARCH_CAP], "note": f"cut at {SEARCH_CAP} lines"}
        return {"matches": matches, "count": len(matches)}

    def edit_file(self, path: str, old: str, new: str) -> dict[str, Any]:
        target = self.writable(path)
        if not target.is_file():
            return {"error": f"no file at {path!r}; write_file creates one"}
        text = target.read_text(encoding="utf-8")
        if old == "":
            updated = text + ("" if not text or text.endswith("\n") else "\n") + new
        elif text.count(old) != 1:
            return {"error": f"old occurs {text.count(old)} times in {path}; it must occur exactly once"}
        else:
            updated = text.replace(old, new, 1)
        target.write_text(updated, encoding="utf-8")
        return {"ok": True, "path": target.relative_to(self.root).as_posix()}

    def write_file(self, path: str, content: str) -> dict[str, Any]:
        target = self.writable(path)
        existed = target.exists()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"ok": True, "path": target.relative_to(self.root).as_posix(), "replaced": existed}

    def run(self, command: str, timeout_s: int = 0) -> dict[str, Any]:
        argv = command_argv(command)
        timeout = min(int(timeout_s or RUN_TIMEOUT_S), RUN_TIMEOUT_S)
        try:
            result = subprocess.run(argv, cwd=self.root, env=self.env, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            return {"error": f"the command ran past {timeout} seconds and was stopped"}
        output = result.stdout + result.stderr
        return {"exit_code": result.returncode, "output": output[-RUN_CAP:],
                **({"note": f"output cut to its last {RUN_CAP} characters"} if len(output) > RUN_CAP else {})}

    def call(self, name: str, args: Mapping[str, Any]) -> Any:
        if name not in self.names:
            return {"error": f"no tool {name!r} in this session"}
        self.calls.append({"tool": name, "args": {k: str(v)[:120] for k, v in args.items()}})
        try:
            return getattr(self, name)(**dict(args))
        except (ValueError, TypeError) as error:
            return {"error": str(error)[:300]}
        except OSError as error:
            return {"error": f"{type(error).__name__}: {str(error)[:200]}"}

    def step_tools(self) -> loop.StepTools:
        return loop.StepTools(specs=[s for s in TOOL_SPECS if s["name"] in self.names])


def _spec(name: str, description: str, **properties: tuple[str, str, bool]) -> dict[str, Any]:
    return {"name": name, "description": description,
            "input_schema": {"type": "object", "additionalProperties": False,
                             "properties": {k: {"type": t, "description": d} for k, (t, d, _r) in properties.items()},
                             "required": [k for k, (_t, _d, r) in properties.items() if r]}}


TOOL_SPECS = [
    _spec("read_file", "Read a file of the checkout, or a line range of it (1-based, inclusive).",
          path=("string", "path from the checkout root", True), start=("integer", "first line, 0 for the start", False),
          end=("integer", "last line, 0 for the end", False)),
    _spec("list_files", "List the files under a folder of the checkout, optionally matching a glob such as '*.py'.",
          path=("string", "folder, '' for the root", False), pattern=("string", "glob", False)),
    _spec("search", "Search the checkout's text files for a regular expression; up to 200 matching lines.",
          pattern=("string", "a Python regular expression", True), path=("string", "folder or file, '' for all", False)),
    _spec("edit_file", "Replace one exact passage of a file; old must occur exactly once ('' appends new at the end).",
          path=("string", "file", True), old=("string", "the exact passage", True), new=("string", "its replacement", True)),
    _spec("write_file", "Create a file or replace a whole file; only under the paths the task allows.",
          path=("string", "file", True), content=("string", "the whole content", True)),
    _spec("run", "Run the tests or the linter in the checkout: pytest, ruff, python -m pytest or python -m ruff with "
          "their arguments. No shell, no network, no installs.",
          command=("string", "e.g. python -m pytest -q tests/test_x.py", True), timeout_s=("integer", "0 for the default", False)),
]


# -- the native session: tailwatch's own loop ------------------------------------------------------

_SESSIONS: dict[str, tuple[loop.Spec, loop.Run, Checkout]] = {}


def native_session(root: Path, prompt: str, *, system: str, review: bool, budget_usd: float, resume: str = "",
                   env: Mapping[str, str] | None = None, config: Any = None) -> dict[str, Any]:
    """One engineer session through ``agent.loop.run`` with the checkout's tools. ``resume``
    continues an earlier session's conversation with a new message (the failed tests)."""
    paths = config.engineer if config is not None else None
    if resume and resume in _SESSIONS:
        spec, state, checkout = _SESSIONS[resume]
        spec = dataclasses.replace(spec, budget=loop.Budget(limit=max(0.0, budget_usd)))
        before = state.cost_usd
        state.answer, state.stopped, state.steps = None, "", 0
        state.messages.append({"role": "user", "content": prompt})
        session_id = resume
        messages = None
    else:
        checkout = Checkout(root, allowed_paths=list(paths.allowed_paths) if paths else [],
                            protected_paths=list(paths.protected_paths) if paths else [], review=review, env=env)
        spec = routines.spec_for("engineer", config, instructions=system, tools=checkout.step_tools(),
                                 schema=VERDICT_SCHEMA if review else REPORT_SCHEMA, budget=loop.Budget(limit=max(0.0, budget_usd)),
                                 step_cap=REVIEW_STEPS if review else REPAIR_STEPS, time_cap_s=300.0 if review else 1800.0,
                                 repeat_stop=False, error_stop=8, output_cap=READ_CAP + RUN_CAP)
        state, before = None, 0.0
        session_id = f"native-{os.getpid()}-{len(_SESSIONS) + 1}"
        messages = [{"role": "user", "content": prompt}]
    calls_before = state.model_calls if state is not None else 0
    try:
        out = loop.run(spec, messages, execute=checkout.call, env=env, state=state)
    except Exception as error:  # the flow records it; a failed session still paid for its calls
        spent = loop.failed_run(error) or state
        return {"answer": None, "cost_usd": round((spent.cost_usd if spent else 0.0) - before, 6), "session_id": session_id,
                "error": f"{type(error).__name__}: {str(error)[:300]}",
                "model_calls": (spent.model_calls if spent else 0) - calls_before, "model": spec.model}
    _SESSIONS[session_id] = (spec, out, checkout)
    answer = out.answer if isinstance(out.answer, dict) else None
    return {"answer": answer, "cost_usd": round(out.cost_usd - before, 6), "session_id": session_id,
            "error": "" if answer is not None else f"stopped: {out.stopped or 'no answer'}",
            "model_calls": out.model_calls - calls_before, "tokens_in": out.tokens_in, "tokens_out": out.tokens_out,
            "model": out.model or spec.model, "tools": list(checkout.calls)}


# -- the Claude session: the Claude Agent SDK ------------------------------------------------------

def parse_fenced(text: str, required: str) -> dict[str, Any] | None:
    """The last JSON object in ``text`` (fenced or whole) that has the key ``required``."""
    candidates = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text or "", re.DOTALL) or [str(text or "").strip()]
    for block in reversed(candidates):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        if isinstance(data, dict) and required in data:
            return data
    return None


def claude_env(root: Path, env: Mapping[str, str]) -> dict[str, str]:
    """The sandbox environment plus the Claude credential. The SDK's CLI inherits the process
    environment, so every secret-looking variable of the process is blanked explicitly."""
    out = {k: "" for k in os.environ if any(w in k.upper() for w in SECRET_ENV_WORDS)}
    out.update(sandbox_env(root, env))
    for key in CLAUDE_CREDENTIALS:
        if env.get(key):
            out[key] = str(env[key])
    return out


def claude_model(config: Any) -> str | None:
    model = config.models.for_("engineer") if config is not None else ""
    provider, _, name = str(model).partition(":")
    if not name:
        return model or None
    return name if provider == "anthropic" else None


def claude_session(root: Path, prompt: str, *, system: str, review: bool, budget_usd: float, resume: str = "",
                   env: Mapping[str, str] | None = None, config: Any = None) -> dict[str, Any]:
    """One session of Claude through the Claude Agent SDK in the checkout: file and test tools for
    a repair, read-only tools for a review, no web and no subagents, turn and dollar caps, no
    settings files loaded, a scrubbed environment."""
    import asyncio
    import concurrent.futures

    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

    env = env if env is not None else os.environ
    options = ClaudeAgentOptions(
        cwd=str(root), allowed_tools=CLAUDE_REVIEW_TOOLS if review else CLAUDE_TOOLS,
        disallowed_tools=CLAUDE_NEVER + (["Edit", "Write", "Bash"] if review else []),
        permission_mode="default" if review else "acceptEdits", max_turns=REVIEW_STEPS if review else REPAIR_STEPS,
        max_budget_usd=max(0.01, budget_usd), system_prompt=system, setting_sources=[], env=claude_env(root, env),
        resume=resume or None, model=claude_model(config))

    async def session() -> dict[str, Any]:
        out: dict[str, Any] = {"text": "", "cost_usd": 0.0, "session_id": "", "error": "", "model_calls": 0}
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, ResultMessage):
                out.update(text=str(message.result or ""), cost_usd=float(message.total_cost_usd or 0),
                           session_id=str(message.session_id or ""), model_calls=int(message.num_turns or 0),
                           error=(message.subtype or "error") if message.is_error else "")
        return out

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:  # safe inside a running event loop too
            result = pool.submit(asyncio.run, session()).result()
    except Exception as error:  # the flow records it and opens nothing
        return {"answer": None, "cost_usd": 0.0, "session_id": resume, "error": f"{type(error).__name__}: {str(error)[:300]}"}
    answer = parse_fenced(result["text"], "verdict" if review else "outcome")
    if answer is None and not result["error"]:
        result["error"] = "the session ended without the JSON " + ("verdict" if review else "report")
    return {"answer": answer, "cost_usd": result["cost_usd"], "session_id": result["session_id"], "error": result["error"],
            "model_calls": result["model_calls"], "model": claude_model(config) or "claude"}
