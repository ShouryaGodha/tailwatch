"""The engineer: the desk's worker that changes tailwatch's own code, off unless
``switches.engineer`` is on. A task (a person's "fix: ..." through the ``engineer`` tool, or a
code proposal a person applied) is a proposal of setting CODE and skill engineer, shown as #T.

One task end to end (``run_task``): the daily and per-task caps; a fresh copy of the code (a
clone of ``engineer.repo`` when set, else of the local checkout) in a temporary folder; the
packet under ``.engineer/`` (the task, a code map, the learned rules); a session whose first read
is the handbook ``docs/engineer.md`` (Claude through the Agent SDK when a Claude credential is
set, else tailwatch's own loop with the sandboxed tools of ``engineer_runner``); the suite, with
its failures fed back for up to three rounds; a fresh read-only reviewer that answers ship, fix
or stop (one fix round); the floors in code (allowed paths, protected paths, size, secrets, a
test beside any change under src/); then a draft pull request on a ``desk/task-<id>`` branch
and one line in the thread that asked. It never merges and never deploys: a person merges on
GitHub. Every run is a row in the run log with what it cost."""

from __future__ import annotations

import ast
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tailwatch import util
from tailwatch.agent import prompts
from tailwatch.desk import engineer_runner
from tailwatch.desk.engineer_runner import PACKET_DIR, allowed, looks_secret, protected
from tailwatch.desk.notices import post_desk

SKILL = "engineer"
SETTING = "CODE"
ROUTINE = "engineer"
RULES_KEY = "ENGINEER:RULES"
HANDBOOK = "docs/engineer.md"
TAG = "engineer"
MIN_TEXT = 12
MAX_FILES = 30
MAX_BYTES = 400_000
TEST_ROUNDS = 3
TEST_TIMEOUT_S = 900.0
TITLE_PREFIX = "tailwatch desk: "
TEST_COMMAND = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
IGNORE_LINES = (f"{PACKET_DIR}/", "__pycache__/", "*.pyc", ".pytest_cache/", ".ruff_cache/", ".mypy_cache/")
IDS = re.compile(r"#?\b([RIPGCTV])(\d+)\b")
PARKED = ("#T{id} is recorded. The engineer is switched off (switches.engineer in tailwatch.yaml); "
          "it runs when the owner turns it on.")
QUEUED = "#T{id} is recorded; the engineer takes it on its next run and answers in this thread with a draft pull request."
STATES = {"open": ("proposed", "running", "open"), "waiting": ("proposed",), "merged": ("applied",),
          "closed": ("rejected", "expired"), "failed": ("failed",), "done": ("answered",), "all": ()}

Runner = Callable[[list[str], Path, Mapping[str, str], float], tuple[int, str]]
Session = Callable[..., dict[str, Any]]


class EngineerError(RuntimeError):
    pass


def run_command(argv: list[str], cwd: Path, env: Mapping[str, str], timeout: float = 600.0) -> tuple[int, str]:
    """Run one command (no shell); (exit code, output)."""
    try:
        result = subprocess.run(argv, cwd=cwd, env=dict(env), capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return 124, f"{argv[0]} ran past {timeout:.0f} seconds"
    except OSError as error:
        return 127, f"{argv[0]}: {error}"
    return result.returncode, result.stdout + result.stderr


def host_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment of the host's own git and gh commands (never the sandbox's)."""
    base = dict(os.environ)
    base.update({k: str(v) for k, v in (env or {}).items() if isinstance(v, str)})
    base["GIT_TERMINAL_PROMPT"] = "0"
    return base


def github_auth(env: Mapping[str, str]) -> dict[str, str]:
    """git's credentials for github.com from ``GITHUB_TOKEN``, passed in the environment so the
    token never appears on a command line."""
    token = str(env.get("GITHUB_TOKEN") or "")
    if not token:
        return {}
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}"}


def git(root: Path, *args: str, runner: Runner = run_command, env: Mapping[str, str] | None = None) -> str:
    code, out = runner(["git", *args], root, env if env is not None else host_env(), 600.0)
    if code != 0:
        raise EngineerError(f"git {args[0]} failed: {out.strip()[-300:]}")
    return out


# -- GitHub: gh when it is installed, else the REST API -------------------------------------------

def gh_available() -> bool:
    return shutil.which("gh") is not None


def _http(method: str, url: str, *, token: str, body: Mapping[str, Any] | None = None) -> Any:
    import httpx

    response = httpx.request(method, url, json=dict(body) if body is not None else None, timeout=30.0,
                             headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                      "X-GitHub-Api-Version": "2022-11-28"})
    if response.status_code >= 400:
        raise EngineerError(f"GitHub answered {response.status_code}: {response.text[:200]}")
    return response.json() if response.content else {}


def pr_number(url: str) -> int:
    tail = str(url or "").rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else 0


class GitHub:
    """The three things the engineer asks of GitHub: open a draft pull request, read one's state,
    close one with a reason. ``gh`` when it is on the path, else the REST API with a token."""

    def __init__(self, repo: str, token: str = "", *, runner: Runner = run_command, http: Callable[..., Any] = _http,
                 use_gh: bool | None = None, env: Mapping[str, str] | None = None) -> None:
        self.repo, self.token, self.runner, self.http = repo, token, runner, http
        self.use_gh = gh_available() if use_gh is None else use_gh
        self.env = host_env(env)
        if token:
            self.env["GH_TOKEN"] = token

    def _gh(self, args: list[str], cwd: Path | None = None) -> str:
        code, out = self.runner(["gh", *args], cwd or Path.cwd(), self.env, 120.0)
        if code != 0:
            raise EngineerError(f"gh {args[0]} {args[1] if len(args) > 1 else ''} failed: {out.strip()[-300:]}")
        return out

    def _api(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> Any:
        if not self.token:
            raise EngineerError("no GITHUB_TOKEN and no gh: the pull request cannot be opened")
        return self.http(method, f"https://api.github.com/repos/{self.repo}/{path}", token=self.token, body=body)

    def create_draft(self, *, root: Path, branch: str, base: str, title: str, body: str) -> str:
        if self.use_gh:
            out = self._gh(["pr", "create", "--draft", "--repo", self.repo, "--base", base, "--head", branch,
                            "--title", title, "--body", body], cwd=root)
            urls = re.findall(r"https://\S+/pull/\d+", out)
            return urls[-1] if urls else out.strip().splitlines()[-1]
        made = self._api("POST", "pulls", {"title": title, "head": branch, "base": base, "body": body, "draft": True})
        return str(made.get("html_url") or "")

    def state(self, url: str) -> str:
        """open, merged or closed."""
        if self.use_gh:
            data = json.loads(self._gh(["pr", "view", url, "--repo", self.repo, "--json", "state,mergedAt"]) or "{}")
            state = str(data.get("state") or "").lower()
            return "merged" if state == "merged" or data.get("mergedAt") else state
        pull = self._api("GET", f"pulls/{pr_number(url)}")
        return "merged" if pull.get("merged") or pull.get("merged_at") else str(pull.get("state") or "")

    def close(self, url: str, reason: str) -> None:
        if self.use_gh:
            self._gh(["pr", "close", url, "--repo", self.repo, "--comment", reason])
            return
        number = pr_number(url)
        self._api("POST", f"issues/{number}/comments", {"body": reason})
        self._api("PATCH", f"pulls/{number}", {"state": "closed"})


def github_for(config: Any, env: Mapping[str, str]) -> GitHub | None:
    """The repository's GitHub, or None when ``engineer.repo`` is not set."""
    repo = config.engineer.repo
    return GitHub(repo, str(env.get("GITHUB_TOKEN") or ""), env=env) if repo else None


# -- tasks ----------------------------------------------------------------------------------------

def evidence_ids(text: str) -> list[str]:
    return list(dict.fromkeys(f"{kind}{number}" for kind, number in IDS.findall(str(text or ""))))[:12]


def is_task(row: Mapping[str, Any] | None) -> bool:
    return bool(row) and row.get("setting") == SETTING and row.get("skill") == SKILL  # type: ignore[union-attr]


def task_row(store: Any, task_id: int) -> dict[str, Any] | None:
    row = store.proposal(int(task_id))
    return row if is_task(row) else None


def brief(row: Mapping[str, Any]) -> dict[str, Any]:
    value = row.get("value") or {}
    ran = value.get("run") or {}
    return {"id": f"#T{row['id']}", "task": row["id"], "text": util.clip(value.get("text"), 300), "asked_by": value.get("asked_by"),
            "state": row.get("state"), "pr": row.get("pr_url") or "", "outcome": ran.get("outcome"), "title": ran.get("title"),
            "tests_ok": ran.get("tests_ok"), "review": ran.get("review"), "cost_usd": ran.get("cost_usd"),
            "created": str(row.get("created_at") or "")[:16], "thread": row.get("thread") or ""}


def tasks(store: Any, state: str = "open", *, limit: int = 50) -> list[dict[str, Any]]:
    """The tasks in one state: open (waiting, running or with a pull request open), waiting,
    merged, closed, failed, done (answered without a change), or all."""
    states = STATES.get(state, (state,))
    return [brief(r) for r in store.code_tasks(states=states, limit=limit)]


def start_task(store: Any, config: Any, env: Mapping[str, str], text: str, *, by: str, thread: str = "",
               source: int = 0) -> dict[str, Any]:
    """Record a task. With the engineer switched off it is parked: recorded, nothing runs."""
    clean = " ".join(str(text or "").split())
    if len(clean) < MIN_TEXT:
        return {"error": "say what to change and why, in a sentence or two"}
    if looks_secret(clean):
        return {"error": "the task text looks like it holds a secret; leave it out and say where it lives instead"}
    value = {"text": clean[:4000], "asked_by": by, **({"from_proposal": int(source)} if source else {})}
    task_id = store.add_proposal(SETTING, skill=SKILL, op="task", value=value, rationale=f"asked by {by}",
                                 evidence=evidence_ids(clean) + ([f"P{source}"] if source else []), by=by, thread=thread)
    if not config.switches.engineer:
        return {"task": task_id, "id": f"#T{task_id}", "parked": True, "message": PARKED.format(id=task_id)}
    return {"task": task_id, "id": f"#T{task_id}", "parked": False, "message": QUEUED.format(id=task_id)}


# -- what the engineer is given -------------------------------------------------------------------

def local_root() -> Path | None:
    """The checkout tailwatch runs from (an editable install of a clone), or None."""
    repo = prompts.PACKAGE.parent.parent
    return repo if (repo / ".git").exists() else None


def remote_url(config: Any) -> str:
    return f"https://github.com/{config.engineer.repo}.git"


def code_map(root: Path) -> str:
    """Every module under src/ with the first paragraph of its docstring, and the prompt and
    skill folders: the map the engineer reads after the handbook."""
    lines = ["# tailwatch, module by module", ""]
    for path in sorted((root / "src").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) or ""
        except (SyntaxError, UnicodeDecodeError, OSError):
            doc = ""
        if path.name == "__init__.py" and not doc:
            continue
        first = " ".join(doc.split("\n\n")[0].split()) if doc else "(no docstring)"
        lines.append(f"- `{path.relative_to(root).as_posix()}`: {util.clip(first, 240)}")
    for folder, what in (("prompts", "the judge's and the ranker's prompts and the signal packs"),
                         ("skills", "the desk's skills: SKILL.md, and agent.md for a worker's instructions"),
                         ("tests", "the offline suite; tests/conftest.py has the scripted model and the in-memory store"),
                         ("docs", "the handbook (engineer.md) and the manuals")):
        if (root / folder).is_dir():
            lines.append(f"- `{folder}/`: {what}")
    return "\n".join(lines) + "\n"


def _evidence_rows(store: Any, ids: list[str]) -> list[dict[str, Any]]:
    rows = []
    for raw in ids[:12]:
        kind, number = raw[:1], raw[1:]
        if not number.isdigit():
            continue
        found: Any = None
        if kind == "R":
            found = store.review(int(number))
        elif kind == "I":
            found = store.issue(int(number))
        elif kind == "P":
            found = store.proposal(int(number))
        elif kind == "V":
            found = store.verdict(int(number))
        if found:
            rows.append({"id": raw, **{k: v for k, v in dict(found).items() if k not in ("checkpoint", "trace")}})
    return rows


def packet_for(store: Any, config: Any, task: Mapping[str, Any]) -> dict[str, Any]:
    value = task.get("value") or {}
    return {"task_id": task["id"], "text": value.get("text") or "", "asked_by": value.get("asked_by") or task.get("by_whom") or "",
            "evidence": _evidence_rows(store, list(task.get("evidence") or [])) if store is not None else [],
            "rules": list(store.get_setting(RULES_KEY, []) or []) if store is not None else [],
            "handbook": HANDBOOK, "allowed_paths": list(config.engineer.allowed_paths),
            "protected_paths": list(config.engineer.protected_paths), "asked_at": str(task.get("created_at") or "")[:19]}


def write_packet(root: Path, packet: Mapping[str, Any]) -> None:
    folder = root / PACKET_DIR
    folder.mkdir(exist_ok=True)
    (folder / "task.json").write_text(json.dumps({k: v for k, v in packet.items() if k != "rules"}, indent=1, default=str),
                                      encoding="utf-8")
    (folder / "code_map.md").write_text(code_map(root), encoding="utf-8")
    rules = list(packet.get("rules") or [])
    (folder / "rules.md").write_text("# Lessons from earlier tasks, applied by the team\n\n"
                                     + ("\n".join(f"{i}. {r}" for i, r in enumerate(rules, 1)) if rules else "(none yet)") + "\n",
                                     encoding="utf-8")


def checkout(config: Any, env: Mapping[str, str], dest: Path, *, source: str | Path | None = None,
             runner: Runner = run_command) -> Path:
    """A fresh copy of the code at ``dest``: a clone of the source given, of ``engineer.repo``,
    or of the local checkout; the packet and caches never count as changes."""
    remote = not source and bool(config.engineer.repo)
    origin = str(source) if source else remote_url(config) if remote else str(local_root() or "")
    if not origin:
        raise EngineerError("no code to work on: set engineer.repo, or run tailwatch from a clone")
    auth = {**host_env(env), **(github_auth(env) if remote else {})}
    dest.parent.mkdir(parents=True, exist_ok=True)
    git(dest.parent, "clone", "--quiet", "--no-hardlinks", origin, str(dest), runner=runner, env=auth)
    base = config.engineer.base
    if base:
        runner(["git", "checkout", "--quiet", base], dest, host_env(), 60.0)  # absent base: stay on the clone's head
    exclude = dest / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a", encoding="utf-8") as handle:
        handle.write("\n" + "\n".join(IGNORE_LINES) + "\n")
    return dest


def changed_files(root: Path, *, runner: Runner = run_command) -> list[str]:
    git(root, "add", "-A", ".", runner=runner)
    out = git(root, "diff", "--cached", "--name-only", "-z", "HEAD", runner=runner)
    paths = [p for p in out.split("\x00") if p.strip()]
    return [p for p in paths if not p.startswith(PACKET_DIR + "/") and not p.endswith(".pyc")
            and not any(part in engineer_runner.IGNORED_PARTS for part in p.split("/"))]


def diff_text(root: Path, *, runner: Runner = run_command, limit: int = 60_000) -> str:
    git(root, "add", "-A", ".", runner=runner)
    text = git(root, "diff", "--cached", "HEAD", runner=runner)
    return text[:limit] + ("\n... (diff cut)" if len(text) > limit else "")


def validate(root: Path, changed: list[str], config: Any) -> list[str]:
    """Why a change may not become a pull request: outside the allowed paths, protected, too
    many or too large files, a secret, a change under src/ without a test."""
    eng = config.engineer
    errors = []
    for path in changed:
        if path.startswith("/") or ".." in path.split("/"):
            errors.append(f"{path}: not a path in the repository")
            continue
        if protected(path, eng.protected_paths):
            errors.append(f"{path}: protected; a task never changes it")
        elif not allowed(path, eng.allowed_paths):
            errors.append(f"{path}: outside the paths a task may change")
        item = root / path
        if item.is_file():
            if item.stat().st_size > MAX_BYTES:
                errors.append(f"{path}: larger than {MAX_BYTES // 1000}KB")
            try:
                text = item.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                text = ""
            if looks_secret(text):
                errors.append(f"{path}: looks like it holds a secret")
    if len(changed) > MAX_FILES:
        errors.append(f"{len(changed)} files changed; at most {MAX_FILES}")
    if any(p.startswith("src/") for p in changed) and not any(p.startswith("tests/") for p in changed):
        errors.append("a change under src/ needs a test change under tests/")
    return errors


def run_suite(root: Path) -> tuple[bool, str]:
    """The offline suite in the checkout with the scrubbed environment: (passed, the output's tail)."""
    code, out = run_command(list(TEST_COMMAND), root, engineer_runner.sandbox_env(root), TEST_TIMEOUT_S)
    return code == 0, out[-6000:]


# -- the sessions ---------------------------------------------------------------------------------

def backend(config: Any, env: Mapping[str, str]) -> str:
    """claude when a Claude credential is set and the SDK is installed, else native."""
    chosen = config.engineer.backend
    if chosen != "auto":
        return chosen
    if any(env.get(k) for k in engineer_runner.CLAUDE_CREDENTIALS):
        try:
            import claude_agent_sdk  # noqa: F401
        except ImportError:
            return "native"
        return "claude"
    return "native"


NATIVE_NOTE = ("\n\n# Your tools here\n\nread_file, list_files and search look; edit_file and write_file change files under the "
               "allowed paths; run runs the tests or the linter (python -m pytest -q, one test file while you work, "
               "python -m ruff check). When you are done, answer with the JSON report and nothing else.")
CLAUDE_NOTE = ("\n\n# Your tools here\n\nThe checkout is the current directory. Bash is for the tests and the linter only "
               "(python -m pytest -q, python -m ruff check). End with the JSON report fenced as ```json and nothing after it.")
NATIVE_REVIEW_NOTE = "\n\nYour tools read the checkout: read_file, list_files, search. Answer with the JSON verdict and nothing else."
CLAUDE_REVIEW_NOTE = "\n\nThe checkout is the current directory. End with the JSON verdict fenced as ```json and nothing after it."


def _skill_text(name: str, config: Any) -> str:
    template = prompts.path_of("skills", f"engineer/{name}").read_text(encoding="utf-8")
    values = {"HANDBOOK": HANDBOOK, "PACKET_DIR": PACKET_DIR, "MAX_FILES": MAX_FILES, "TEST_ROUNDS": TEST_ROUNDS,
              "ALLOWED_PATHS": ", ".join(config.engineer.allowed_paths), "PROTECTED_PATHS": ", ".join(config.engineer.protected_paths)}
    return prompts.render(template, values)


def instructions(config: Any, *, flavour: str, review: bool = False) -> str:
    if review:
        return _skill_text("review.md", config) + (CLAUDE_REVIEW_NOTE if flavour == "claude" else NATIVE_REVIEW_NOTE)
    return _skill_text("agent.md", config) + (CLAUDE_NOTE if flavour == "claude" else NATIVE_NOTE)


def agent_prompt(packet: Mapping[str, Any]) -> str:
    return (f"Task #T{packet.get('task_id')}, asked by {packet.get('asked_by') or 'the desk'}:\n\n{packet.get('text')}\n\n"
            f"Read {HANDBOOK} first, then {PACKET_DIR}/task.json, {PACKET_DIR}/code_map.md and {PACKET_DIR}/rules.md. "
            "Work as your instructions say and end with the JSON report.")


def session_for(flavour: str) -> Session:
    return engineer_runner.claude_session if flavour == "claude" else engineer_runner.native_session


@dataclass
class Ledger:
    """What a task's sessions cost, in the shape the run log records."""

    model_calls: int = 0
    tokens_in: int = 0
    tokens_cached: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    priced: bool = True
    stopped: str = ""
    model: str = ""
    trace: list[dict[str, Any]] = field(default_factory=list)

    def add(self, result: Mapping[str, Any], what: str) -> None:
        self.model_calls += int(result.get("model_calls") or 0)
        self.tokens_in += int(result.get("tokens_in") or 0)
        self.tokens_out += int(result.get("tokens_out") or 0)
        self.cost_usd = round(self.cost_usd + float(result.get("cost_usd") or 0.0), 6)
        self.model = str(result.get("model") or self.model)
        self.trace.append({"session": what, "cost_usd": result.get("cost_usd"), "error": result.get("error") or "",
                           "tools": len(result.get("tools") or [])})


def review_diff(root: Path, packet: Mapping[str, Any], report: Mapping[str, Any], *, config: Any, env: Mapping[str, str],
                session: Session, flavour: str, budget_usd: float, runner: Runner = run_command) -> dict[str, Any]:
    """A fresh read-only session over the diff: ship, fix or stop."""
    prompt = (f"The task:\n{packet.get('text')}\n\nThe engineer's report:\n{json.dumps(dict(report), indent=1)}\n\n"
              f"The diff:\n```diff\n{diff_text(root, runner=runner)}\n```\n\nRead any file you need, then answer with the verdict.")
    result = session(root, prompt, system=instructions(config, flavour=flavour, review=True), review=True,
                     budget_usd=budget_usd, resume="", env=env, config=config)
    answer = result.get("answer") or {}
    verdict = answer.get("verdict") if answer.get("verdict") in ("ship", "fix", "stop") else "stop"
    reasons = [str(r)[:300] for r in (answer.get("reasons") or [])][:3] or ([result["error"]] if result.get("error") else [])
    return {"verdict": verdict, "reasons": reasons, "result": result}


def work(root: Path, packet: Mapping[str, Any], *, config: Any, env: Mapping[str, str], budget_usd: float, ledger: Ledger,
         session: Session | None = None, reviewer: Session | None = None, suite: Callable[[Path], tuple[bool, str]] | None = None,
         flavour: str = "native", runner: Runner = run_command) -> dict[str, Any]:
    """The sandbox part on a checkout at ``root``: the packet, the session, the suite fed back up
    to TEST_ROUNDS, the floors, the review with one fix round. Writes nothing outside the checkout."""
    session = session or session_for(flavour)
    suite = suite or run_suite

    def remaining() -> float:
        return round(budget_usd - ledger.cost_usd, 6)

    def ask(prompt: str, resume: str) -> dict[str, Any]:
        result = session(root, prompt, system=system, review=False, budget_usd=remaining(), resume=resume, env=env, config=config)
        ledger.add(result, "repair")
        return result

    write_packet(root, packet)
    system = instructions(config, flavour=flavour)
    result = ask(agent_prompt(packet), "")
    report = dict(result.get("answer") or {})
    changed = changed_files(root, runner=runner)
    tests_ok, tests_out, rounds = True, "", 0
    while changed:
        tests_ok, tests_out = suite(root)
        if tests_ok or rounds >= TEST_ROUNDS or result.get("error") or remaining() <= 0:
            break
        rounds += 1
        result = ask("The test suite failed. Fix the cause (or your change), run it again, and end with the JSON report.\n\n"
                     f"```\n{tests_out[-4000:]}\n```", str(result.get("session_id") or ""))
        report = dict(result.get("answer") or report)
        changed = changed_files(root, runner=runner)
    errors = validate(root, changed, config) if changed else []
    review: dict[str, Any] = {"verdict": "", "reasons": []}
    if changed and tests_ok and not errors:
        if remaining() <= 0:
            review = {"verdict": "stop", "reasons": ["the task's budget ran out before the review"]}
        else:
            found = review_diff(root, packet, report, config=config, env=env, session=reviewer or session, flavour=flavour,
                                budget_usd=min(remaining(), 1.5), runner=runner)
            ledger.add(found.pop("result"), "review")
            review = found
            if review["verdict"] == "fix" and remaining() > 0:
                result = ask("A reviewer read your diff and asks for one more round: " + " ".join(review["reasons"])
                             + "\nMake that change, run the suite, and end with the JSON report.", str(result.get("session_id") or ""))
                report = dict(result.get("answer") or report)
                changed = changed_files(root, runner=runner)
                tests_ok, tests_out = suite(root) if changed else (True, "")
                errors = validate(root, changed, config) if changed else []
                review = {**review, "verdict": "ship" if changed and tests_ok and not errors else "stop", "after_fix": True}
            elif review["verdict"] == "fix":
                review = {**review, "verdict": "stop", "reasons": [*review["reasons"], "no budget left for the fix round"]}
    outcome = ("no_change" if not changed else "failed_tests" if not tests_ok else "refused" if errors else
               "ready" if review.get("verdict") == "ship" else "stopped")
    return {"outcome": outcome, "report": report, "changed": changed, "tests_ok": tests_ok, "tests_out": tests_out[-2000:],
            "errors": errors, "review": review, "rounds": rounds, "agent_error": str(result.get("error") or ""),
            "diff": diff_text(root, runner=runner) if changed else ""}


# -- publishing ----------------------------------------------------------------------------------

def pr_title(report: Mapping[str, Any], packet: Mapping[str, Any]) -> str:
    title = " ".join(str(report.get("title") or "").split()) or util.clip(packet.get("text"), 70)
    pattern = re.compile(r"^\s*tailwatch[\s-]*desk\s*[:\-–—]\s*", re.IGNORECASE)
    while pattern.match(title):
        title = pattern.sub("", title, count=1)
    return TITLE_PREFIX + util.clip(title.rstrip(". "), 90)


def pr_body(task_id: int, packet: Mapping[str, Any], out: Mapping[str, Any]) -> str:
    report = out.get("report") or {}
    changed = "\n".join(f"- `{p}`" for p in out.get("changed") or [])
    return (f"Desk task #T{task_id}, asked by {packet.get('asked_by') or 'the desk'}.\n\n> {util.clip(packet.get('text'), 1500)}\n\n"
            f"## What changed\n\n{report.get('summary') or '(no summary)'}\n\n{changed}\n\n"
            f"Tests: the offline suite passed in the sandbox after {out.get('rounds', 0)} fix round(s). "
            f"Self-review: {(out.get('review') or {}).get('verdict') or '?'}.\n\n"
            "Opened as a draft by tailwatch's desk engineer. A person reviews and merges it; the engineer never merges.")


def publish(root: Path, task_id: int, packet: Mapping[str, Any], out: Mapping[str, Any], *, config: Any, env: Mapping[str, str],
            github: GitHub | None, remote: bool, runner: Runner = run_command) -> tuple[str, str]:
    """Commit the change on ``desk/task-<id>``, push it, open the draft pull request.
    Returns (url, branch); url is '' when there is no GitHub repository to open it on."""
    branch = f"desk/task-{task_id}"
    title = pr_title(out.get("report") or {}, packet)
    git(root, "checkout", "--quiet", "-B", branch, runner=runner)
    git(root, "reset", "--quiet", runner=runner)
    git(root, "add", "-A", "--", *out["changed"], runner=runner)
    git(root, "-c", "user.name=tailwatch desk", "-c", "user.email=desk@tailwatch.invalid", "commit", "--quiet",
        "-m", title, "-m", f"Desk task #T{task_id}.", runner=runner)
    git(root, "push", "--quiet", "--force", "origin", branch, runner=runner,
        env={**host_env(env), **(github_auth(env) if remote else {})})
    if github is None:
        return "", branch
    return github.create_draft(root=root, branch=branch, base=config.engineer.base, title=title,
                               body=pr_body(task_id, packet, out)), branch


def thread_line(task_id: int, out: Mapping[str, Any], url: str, branch: str, title: str = "") -> str:
    report = out.get("report") or {}
    if url:
        said = util.clip(report.get("user_message"), 240)
        return (f"#T{task_id}: draft PR {url} opened: {title.removeprefix(TITLE_PREFIX)}. Tests green, self-review ship. "
                + (f"{said} " if said else "") + f"Review and merge it on GitHub; reply close T{task_id}: why to drop it.")
    if branch:
        return (f"#T{task_id}: the change is on branch {branch} of the local checkout (no engineer.repo is set, so no "
                f"pull request). Review and merge it yourself; reply close T{task_id}: why to drop it.")
    if out.get("outcome") == "no_change":
        answer = util.clip(report.get("user_message") or report.get("summary") or "no change was needed", 600)
        question = util.clip(report.get("question"), 300)
        return f"#T{task_id}: {answer}" + (f" Question: {question}" if question else "")
    why = {"failed_tests": "the suite still failed after the fix rounds", "refused": "; ".join(out.get("errors") or []),
           "stopped": "the reviewer stopped it: " + " ".join((out.get("review") or {}).get("reasons") or [])
           }.get(str(out.get("outcome")), str(out.get("agent_error") or "it did not finish"))
    return f"#T{task_id}: no pull request, {util.clip(why, 400)}"


def _thread_for(store: Any, config: Any, task: Mapping[str, Any]) -> str:
    if task.get("thread"):
        return str(task["thread"])
    return str(store.get_setting(f"DESK:NOTE:{util.local_day(config.schedule.timezone)}", "") or "")


def run_task(store: Any, config: Any, env: Mapping[str, str], task_id: int, *, session: Session | None = None,
             reviewer: Session | None = None, suite: Callable[[Path], tuple[bool, str]] | None = None,
             github: GitHub | None = None, source: str | Path | None = None, runner: Runner = run_command) -> dict[str, Any]:
    """One task end to end. Off unless switches.engineer; capped per task and per day."""
    from tailwatch.store import LockBusy

    task = task_row(store, task_id)
    if task is None:
        return {"error": f"no engineer task #T{task_id}"}
    if task["state"] not in ("proposed", "failed"):
        return {"error": f"#T{task_id} is {task['state']}"}
    if not config.switches.engineer:
        return {"task": task_id, "parked": True, "message": PARKED.format(id=task_id)}
    daily = float(config.budgets.engineer_daily)
    spent = store.spend_today((ROUTINE,), config.schedule.timezone)
    if spent >= daily - 0.01:
        return {"task": task_id, "error": f"the engineer's daily budget (${daily:.2f}) is spent; #T{task_id} waits for tomorrow",
                "retry_later": True}
    budget = round(min(float(config.budgets.engineer_per_task), daily - spent), 4)
    try:
        with store.lock("engineer", ttl_s=3 * 3600):
            if not store.claim_task(int(task_id)):
                return {"error": f"#T{task_id} is running already"}
            return _run_claimed(store, config, env, task, budget=budget, session=session, reviewer=reviewer, suite=suite,
                                github=github, source=source, runner=runner)
    except LockBusy:
        return {"task": task_id, "error": "another engineer task is running; this one waits", "retry_later": True}


def _run_claimed(store: Any, config: Any, env: Mapping[str, str], task: Mapping[str, Any], *, budget: float,
                 session: Session | None, reviewer: Session | None, suite: Callable[[Path], tuple[bool, str]] | None,
                 github: GitHub | None, source: str | Path | None, runner: Runner) -> dict[str, Any]:
    task_id = int(task["id"])
    flavour = "native" if session is not None else backend(config, env)
    run_id = store.open_run(ROUTINE, subject=f"T{task_id}", model=config.models.for_("engineer"))
    ledger = Ledger()
    packet = packet_for(store, config, task)
    remote = not source and bool(config.engineer.repo)
    github = github if github is not None else (github_for(config, env) if remote else None)
    url = branch = ""
    try:
        with tempfile.TemporaryDirectory(prefix="tailwatch-engineer-") as folder:
            root = checkout(config, env, Path(folder) / "repo", source=source, runner=runner)
            out = work(root, packet, config=config, env=env, budget_usd=budget, ledger=ledger, session=session, reviewer=reviewer,
                       suite=suite, flavour=flavour, runner=runner)
            if out["outcome"] == "ready":
                url, branch = publish(root, task_id, packet, out, config=config, env=env, github=github, remote=remote,
                                      runner=runner)
    except Exception as error:  # the task row and the run log record it; the thread hears one line
        out = {"outcome": "failed", "report": {}, "changed": [], "tests_ok": False, "errors": [], "review": {}, "rounds": 0,
               "agent_error": f"{type(error).__name__}: {util.clip(error, 300)}"}
    outcome = out["outcome"]
    state = "open" if url or branch else "answered" if outcome == "no_change" else "failed"
    title = pr_title(out.get("report") or {}, packet)
    record = {"outcome": outcome, "pr": url, "branch": branch, "title": title,
              "report": out.get("report"), "changed": out.get("changed"), "tests_ok": out.get("tests_ok"),
              "errors": out.get("errors"), "review": (out.get("review") or {}).get("verdict"), "rounds": out.get("rounds"),
              "cost_usd": ledger.cost_usd, "ran_at": util.iso(), "error": out.get("agent_error") or ""}
    store.set_proposal(task_id, state=state, pr_url=url, value={**(task.get("value") or {}), "run": record})
    store.finish_run(run_id, ledger, state="done" if outcome in ("ready", "no_change") else "failed", answer=record,
                     error=out.get("agent_error") or "; ".join(out.get("errors") or []))
    learned = str((out.get("report") or {}).get("learned") or "").strip()
    text = thread_line(task_id, out, url, branch, title)
    post_desk(store, config, env, text, thread=_thread_for(store, config, task), tag=TAG)
    return {"task": task_id, "state": state, **record, "learned": learned, "message": text}


def run_waiting(store: Any, config: Any, env: Mapping[str, str], *, limit: int = 1) -> dict[str, Any]:
    """The tick's job: the oldest waiting tasks, one at a time, while the switch and the caps allow."""
    waiting = sorted(store.code_tasks(states=("proposed",), limit=50), key=lambda r: int(r["id"]))
    if not config.switches.engineer:
        return {"parked": True, "waiting": len(waiting)}
    ran = []
    for task in waiting[:limit]:
        result = run_task(store, config, env, int(task["id"]))
        ran.append({k: result.get(k) for k in ("task", "state", "outcome", "pr", "error")})
        if result.get("retry_later"):
            break
    return {"ran": ran, "waiting": max(0, len(waiting) - len(ran))}


# -- settling and closing ----------------------------------------------------------------------------

def settle(store: Any, config: Any, env: Mapping[str, str], *, github: GitHub | None = None) -> dict[str, list[int]]:
    """Tasks whose pull request a person merged or closed on GitHub: applied or rejected, so the
    note stops listing them. GitHub unreachable: the tasks wait."""
    github = github or github_for(config, env)
    out: dict[str, list[int]] = {"merged": [], "closed": []}
    if github is None:
        return out
    for task in store.code_tasks(states=("open",), limit=100):
        url = str(task.get("pr_url") or "")
        if not url:
            continue
        try:
            state = github.state(url)
        except Exception:  # GitHub down or the token refused: ask again tomorrow
            continue
        if state == "merged":
            store.decide_proposal(int(task["id"]), "applied", by="github", expect="open")
            out["merged"].append(int(task["id"]))
        elif state == "closed":
            store.decide_proposal(int(task["id"]), "rejected", by="github", expect="open")
            out["closed"].append(int(task["id"]))
    return out


def close_task(store: Any, config: Any, env: Mapping[str, str], task_id: int, *, reason: str, by: str,
               github: GitHub | None = None) -> dict[str, Any]:
    """A person's close: the pull request closed on GitHub with the reason, the task rejected."""
    task = task_row(store, task_id)
    if task is None:
        return {"error": f"no engineer task #T{task_id}"}
    if not str(reason or "").strip():
        return {"error": f"say why: close T{task_id}: why"}
    if task["state"] in ("applied", "rejected"):
        return {"error": f"#T{task_id} is {task['state']} already"}
    url = str(task.get("pr_url") or "")
    if url:
        github = github or github_for(config, env)
        if github is None:
            return {"error": "no engineer.repo is set, so the pull request cannot be closed from here"}
        github.close(url, f"Closed from the desk by {by}: {reason}")
    store.decide_proposal(int(task_id), "rejected", by=by, note=reason)
    return {"task": int(task_id), "state": "rejected", "pr": url, "closed_on_github": bool(url)}


# -- the evaluation set -------------------------------------------------------------------------------

EVAL_KEYS = {"task", "must_change", "must_contain"}


def eval_folder() -> Path:
    root = local_root()
    return (root or Path.cwd()) / "tests" / "engineer_tasks"


def check_eval_task(data: Any) -> list[str]:
    """Why a file of the evaluation set is malformed."""
    if not isinstance(data, dict):
        return ["not a JSON object"]
    errors = [f"unknown key {k}" for k in sorted(set(data) - EVAL_KEYS)]
    if not isinstance(data.get("task"), str) or len(data["task"].strip()) < MIN_TEXT:
        errors.append("task is a sentence or two of text")
    for key in ("must_change", "must_contain"):
        value = data.get(key)
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
            errors.append(f"{key} is a list of strings")
    if not data.get("must_change"):
        errors.append("must_change names at least one path")
    return errors


def eval_tasks(folder: Path | None = None) -> list[dict[str, Any]]:
    out = []
    for path in sorted((folder or eval_folder()).glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        out.append({**data, "name": path.stem})
    return out


def check_eval(task: Mapping[str, Any], out: Mapping[str, Any]) -> list[str]:
    failures = []
    changed = list(out.get("changed") or [])
    for path in task.get("must_change") or []:
        if not any(p == path or p.startswith(path.rstrip("/") + "/") for p in changed):
            failures.append(f"no change to {path}")
    added = "\n".join(line[1:] for line in str(out.get("diff") or "").splitlines() if line.startswith("+"))
    for text in task.get("must_contain") or []:
        if text not in added:
            failures.append(f"the diff adds no {util.clip(text, 40)!r}")
    if changed and not out.get("tests_ok"):
        failures.append("the suite failed")
    if out.get("errors"):
        failures.append("refused: " + "; ".join(out["errors"]))
    return failures


def run_eval(store: Any, config: Any, env: Mapping[str, str], *, session: Session | None = None, reviewer: Session | None = None,
             suite: Callable[[Path], tuple[bool, str]] | None = None, folder: Path | None = None,
             source: str | Path | None = None, only: str = "", runner: Runner = run_command) -> dict[str, Any]:
    """Every evaluation task on a fresh copy, with the real model unless a session is given:
    nothing pushed, no pull request. Each task is a row in the run log with its cost."""
    flavour = "native" if session is not None else backend(config, env)
    results = []
    for task in eval_tasks(folder):
        if only and task["name"] != only:
            continue
        ledger = Ledger()
        run_id = store.open_run(ROUTINE, subject=f"eval:{task['name']}", model=config.models.for_("engineer"))
        packet = {"task_id": task["name"], "text": task["task"], "asked_by": "the evaluation", "evidence": [], "rules": [],
                  "handbook": HANDBOOK, "allowed_paths": list(config.engineer.allowed_paths),
                  "protected_paths": list(config.engineer.protected_paths)}
        try:
            with tempfile.TemporaryDirectory(prefix="tailwatch-eval-") as folder_name:
                root = checkout(config, env, Path(folder_name) / "repo", source=source or local_root(), runner=runner)
                out = work(root, packet, config=config, env=env, budget_usd=float(config.budgets.engineer_per_task), ledger=ledger,
                           session=session, reviewer=reviewer, suite=suite, flavour=flavour, runner=runner)
        except Exception as error:
            out = {"outcome": "failed", "changed": [], "tests_ok": False, "errors": [f"{type(error).__name__}: {error}"]}
        failures = check_eval(task, out)
        result = {"name": task["name"], "passed": not failures, "failures": failures, "outcome": out["outcome"],
                  "changed": out.get("changed"), "cost_usd": ledger.cost_usd}
        store.finish_run(run_id, ledger, state="done" if not failures else "failed", answer=result, error="; ".join(failures))
        results.append(result)
    return {"passed": sum(1 for r in results if r["passed"]), "total": len(results),
            "cost_usd": round(sum(r["cost_usd"] for r in results), 4), "results": results}
