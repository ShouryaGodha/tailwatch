"""The engineer: parked while off; one task on a fresh clone of a temporary git repository with a
scripted coding session; the floors in code (paths, protected paths, secrets, a test beside a
code change); the draft pull request through a fake gh or a fake REST API; the caps; the
sandboxed tools; the evaluation set's schema. No network, no real model."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tailwatch.agent import loop
from tailwatch.agent.providers.scripted import turn
from tailwatch.desk import engineer, engineer_runner, maintainer, notices

FAKE_KEY = "sk-ant-" + "x" * 32  # built, so this file never holds a key-shaped literal


@pytest.fixture
def posts() -> Any:
    sent: list[tuple[str, str]] = []
    notices.set_poster(lambda text, thread: sent.append((text, thread)) or f"ts-{len(sent)}")
    yield sent
    notices.set_poster(None)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A small repository shaped like tailwatch, with one commit on main."""
    root = tmp_path / "origin"
    (root / "src" / "tailwatch").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "docs").mkdir()
    (root / "src" / "tailwatch" / "__init__.py").write_text('"""The package."""\n')
    (root / "src" / "tailwatch" / "util.py").write_text('"""Small helpers."""\n\n\ndef one() -> int:\n    return 1\n')
    (root / "tests" / "test_util.py").write_text("from tailwatch.util import one\n\n\ndef test_one():\n    assert one() == 1\n")
    (root / "docs" / "engineer.md").write_text("# The handbook\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "first")
    return root


@pytest.fixture
def on(config: Any) -> Any:
    config.switches.engineer = True
    return config


REPORT = {"outcome": "fixed", "title": "tailwatch desk: Add two", "summary": "Adds two() to util with a test.",
          "user_message": "Done: two() is there.", "question": "", "learned": "util has no tests of its own yet."}


def coder(*edits: dict[str, str], answer: dict[str, Any] | None = None, cost: float = 0.4) -> Any:
    """A scripted coding session: each call writes the next set of files and answers."""
    calls: list[dict[str, Any]] = []
    queue = list(edits)

    def session(root: Path, prompt: str, *, system: str, review: bool, budget_usd: float, resume: str, env: Any,
                config: Any) -> dict[str, Any]:
        calls.append({"prompt": prompt, "system": system, "review": review, "budget": budget_usd, "resume": resume})
        if review:
            return {"answer": {"verdict": "ship", "reasons": ["small and tested"]}, "cost_usd": 0.1, "session_id": "r1"}
        for path, text in (queue.pop(0) if queue else {}).items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        return {"answer": answer or REPORT, "cost_usd": cost, "session_id": "s1", "model_calls": 3}

    session.calls = calls  # type: ignore[attr-defined]
    return session


GOOD = {"src/tailwatch/util.py": '"""Small helpers."""\n\n\ndef one() -> int:\n    return 1\n\n\ndef two() -> int:\n    return 2\n',
        "tests/test_two.py": "from tailwatch.util import two\n\n\ndef test_two():\n    assert two() == 2\n"}


def green(root: Path) -> tuple[bool, str]:
    return True, "2 passed"


def task(store: Any, config: Any, text: str = "Add a two() helper to util with a test for it.") -> int:
    return int(engineer.start_task(store, config, {}, text, by="owner")["task"])


class FakeGh:
    """Runs git for real and answers gh like GitHub would."""

    def __init__(self, url: str = "https://github.com/acme/tailwatch/pull/7") -> None:
        self.url = url
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], cwd: Path, env: Any, timeout: float = 600.0) -> tuple[int, str]:
        if argv[0] == "gh":
            self.calls.append(argv)
            if argv[1:3] == ["pr", "create"]:
                return 0, f"Creating draft pull request\n{self.url}\n"
            if argv[1:3] == ["pr", "view"]:
                return 0, json.dumps({"state": "OPEN", "mergedAt": None})
            return 0, ""
        return engineer.run_command(argv, cwd, env, timeout)


def test_a_task_is_parked_while_the_engineer_is_off(store: Any, config: Any, posts: Any) -> None:
    started = engineer.start_task(store, config, {}, "Add a two() helper to util, see #R12.", by="owner", thread="t1")
    assert started["parked"] is True and "switched off" in started["message"] and started["id"] == f"#T{started['task']}"
    row = store.proposal(started["task"])
    assert (row["setting"], row["skill"], row["state"], row["thread"]) == ("CODE", "engineer", "proposed", "t1")
    assert row["evidence"] == ["R12"]
    assert engineer.run_task(store, config, {}, started["task"])["parked"] is True
    assert engineer.run_waiting(store, config, {}) == {"parked": True, "waiting": 1}
    assert not posts and not store.runs(routine="engineer")


def test_a_task_needs_words_and_no_secret(store: Any, config: Any) -> None:
    assert "sentence" in engineer.start_task(store, config, {}, "fix it", by="owner")["error"]
    assert "secret" in engineer.start_task(store, config, {}, f"Use the key {FAKE_KEY} for the judge.", by="owner")["error"]
    assert not store.proposals(state="all")


def test_the_tools_are_registered_and_record_through_the_registry(store: Any, config: Any) -> None:
    from tailwatch.agent import registry

    tools = {t.name: t for t in registry.owned_by("engineer")}
    assert set(tools) == {"engineer", "tasks", "code_map"} and tools["engineer"].writes
    ctx = registry.ToolContext(store=store, config=config, user="local", thread="t9")
    made = registry.execute(tools["engineer"], ctx, {"text": "Add a two() helper to util with a test."})
    assert made["parked"] and store.proposal(made["task"])["thread"] == "t9"
    listed = registry.execute(tools["tasks"], ctx, {"state": "open"})
    assert listed["tasks"][0]["id"] == f"#T{made['task']}" and listed["switched_on"] is False
    outsider = registry.ToolContext(store=store, config=config.model_copy(update={"team": ["owner"]}), user="stranger")
    assert "team list" in registry.execute(tools["engineer"], outsider, {"text": "Add a two() helper to util."})["error"]


def test_one_task_opens_a_draft_pull_request(store: Any, on: Any, repo: Path, tmp_path: Path, posts: Any,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(bare)], check=True)
    monkeypatch.setattr(engineer, "remote_url", lambda config: str(bare))
    on.engineer.repo = "acme/tailwatch"
    gh = FakeGh()
    session = coder(GOOD)
    tid = task(store, on)
    result = engineer.run_task(store, on, {}, tid, session=session, suite=green,
                               github=engineer.GitHub("acme/tailwatch", runner=gh, use_gh=True), runner=gh)
    assert result["state"] == "open" and result["pr"] == gh.url and result["outcome"] == "ready"
    create = gh.calls[0]
    assert create[:4] == ["gh", "pr", "create", "--draft"]
    flags = dict(zip(create[4::2], create[5::2], strict=False))
    assert flags["--repo"] == "acme/tailwatch" and flags["--base"] == "main" and flags["--head"] == f"desk/task-{tid}"
    assert flags["--title"] == "tailwatch desk: Add two"
    assert f"#T{tid}" in flags["--body"] and "never merges" in flags["--body"]
    assert f"desk/task-{tid}" in _git(bare, "branch", "--list")
    shipped = _git(bare, "show", "--stat", "--format=%s", f"desk/task-{tid}")
    assert "tests/test_two.py" in shipped and ".engineer" not in shipped
    assert "def two" in _git(bare, "show", f"desk/task-{tid}:src/tailwatch/util.py")
    row = store.proposal(tid)
    assert row["state"] == "open" and row["pr_url"] == gh.url and row["value"]["run"]["review"] == "ship"
    run = store.runs(routine="engineer")[0]
    assert run["state"] == "done" and run["cost_usd"] == pytest.approx(0.5) and run["subject"] == f"T{tid}"
    assert posts[-1][0].startswith(f"#T{tid}: draft PR {gh.url} opened: Add two.")
    first = session.calls[0]
    assert "docs/engineer.md" in first["prompt"] and "Read docs/engineer.md first" in first["prompt"]
    assert first["budget"] == pytest.approx(on.budgets.engineer_per_task)
    assert session.calls[1]["review"] is True


def test_the_packet_is_written_and_never_shipped(store: Any, on: Any, repo: Path, posts: Any) -> None:
    seen: dict[str, Any] = {}

    def session(root: Path, prompt: str, **kwargs: Any) -> dict[str, Any]:
        seen["task"] = json.loads((root / ".engineer" / "task.json").read_text())
        seen["map"] = (root / ".engineer" / "code_map.md").read_text()
        seen["rules"] = (root / ".engineer" / "rules.md").read_text()
        return {"answer": {**REPORT, "outcome": "no_change", "user_message": "util already does that."}, "cost_usd": 0.1}

    store.set_setting("ENGINEER:RULES", ["Run the one test file first."], by="owner")
    tid = task(store, on)
    result = engineer.run_task(store, on, {}, tid, session=session, suite=green, source=repo)
    assert seen["task"]["text"].startswith("Add a two()") and seen["task"]["handbook"] == "docs/engineer.md"
    assert "`src/tailwatch/util.py`: Small helpers." in seen["map"] and "1. Run the one test file first." in seen["rules"]
    assert result["state"] == "answered" and posts[-1][0].startswith(f"#T{tid}: util already does that.")


@pytest.mark.parametrize(("files", "why"), [
    ({".github/workflows/ci.yml": "on: push\n", "tests/test_x.py": "def test_x():\n    pass\n"}, "protected"),
    ({"src/tailwatch/desk/engineer.py": "x = 1\n", "tests/test_x.py": "def test_x():\n    pass\n"}, "protected"),
    ({"setup.cfg": "[x]\n"}, "outside the paths"),
    ({"src/tailwatch/util.py": "def one():\n    return 2\n"}, "needs a test change"),
    ({"docs/keys.md": f"the key is {FAKE_KEY}\n"}, "secret"),
])
def test_the_floors_refuse_a_diff(store: Any, on: Any, repo: Path, posts: Any, files: dict[str, str], why: str) -> None:
    tid = task(store, on)
    result = engineer.run_task(store, on, {}, tid, session=coder(files), suite=green, source=repo)
    assert result["outcome"] == "refused" and result["state"] == "failed" and not result["pr"]
    assert any(why in e for e in result["errors"])
    assert "no pull request" in posts[-1][0]
    assert store.runs(routine="engineer")[0]["state"] == "failed"


def test_validate_counts_files_and_bytes(tmp_path: Path, config: Any) -> None:
    big = tmp_path / "docs" / "big.md"
    big.parent.mkdir()
    big.write_text("x" * (engineer.MAX_BYTES + 1))
    assert any("larger than" in e for e in engineer.validate(tmp_path, ["docs/big.md"], config))
    many = [f"docs/f{n}.md" for n in range(engineer.MAX_FILES + 1)]
    assert any("at most 30" in e for e in engineer.validate(tmp_path, many, config))
    assert any("not a path" in e for e in engineer.validate(tmp_path, ["../outside.py"], config))
    assert any("protected" in e for e in engineer.validate(tmp_path, ["tests/.env"], config))
    assert engineer.validate(tmp_path, ["docs/engineer.md", "skills/x/SKILL.md"], config) == []


def test_failing_tests_go_back_to_the_session(store: Any, on: Any, repo: Path, posts: Any) -> None:
    results = iter([(False, "FAILED tests/test_two.py"), (False, "FAILED again"), (True, "2 passed")])
    session = coder(GOOD, {}, {})
    tid = task(store, on)
    result = engineer.run_task(store, on, {}, tid, session=session, suite=lambda root: next(results), source=repo)
    assert result["rounds"] == 2 and result["outcome"] == "ready"
    assert "FAILED tests/test_two.py" in session.calls[1]["prompt"] and session.calls[1]["resume"] == "s1"
    assert result["state"] == "open" and result["branch"] == f"desk/task-{tid}" and not result["pr"]
    assert f"desk/task-{tid}" in _git(repo, "branch", "--list")  # local mode: the branch lands in the checkout
    assert "no engineer.repo is set" in posts[-1][0]


def test_tests_that_never_pass_open_nothing(store: Any, on: Any, repo: Path, posts: Any) -> None:
    session = coder(GOOD)
    tid = task(store, on)
    result = engineer.run_task(store, on, {}, tid, session=session, suite=lambda root: (False, "FAILED"), source=repo)
    assert result["outcome"] == "failed_tests" and result["rounds"] == engineer.TEST_ROUNDS
    assert len([c for c in session.calls if not c["review"]]) == engineer.TEST_ROUNDS + 1


def test_the_reviewer_asks_for_one_fix_round(store: Any, on: Any, repo: Path, posts: Any) -> None:
    verdicts = iter(["fix", "ship"])
    session = coder(GOOD, {"tests/test_two.py": "from tailwatch.util import two\n\n\ndef test_two():\n    assert two() == 2  # checked\n"})

    def reviewer(root: Path, prompt: str, **kwargs: Any) -> dict[str, Any]:
        assert "```diff" in prompt and "def two" in prompt and kwargs["review"] is True
        return {"answer": {"verdict": next(verdicts), "reasons": ["say what the test checks"]}, "cost_usd": 0.1}

    tid = task(store, on)
    result = engineer.run_task(store, on, {}, tid, session=session, reviewer=reviewer, suite=green, source=repo)
    assert result["outcome"] == "ready" and "say what the test checks" in session.calls[-1]["prompt"]
    stopped = coder(GOOD)
    tid2 = task(store, on, "Add a three() helper to util with a test.")
    result2 = engineer.run_task(store, on, {}, tid2, session=stopped, suite=green, source=repo,
                                reviewer=lambda root, prompt, **kw: {"answer": {"verdict": "stop", "reasons": ["out of scope"]},
                                                                      "cost_usd": 0.0})
    assert result2["outcome"] == "stopped" and "out of scope" in posts[-1][0]


def test_the_daily_and_the_per_task_caps(store: Any, on: Any, repo: Path, posts: Any) -> None:
    store.log_run("engineer", loop.Run(cost_usd=on.budgets.engineer_daily - 2.0))
    session = coder(GOOD)
    tid = task(store, on)
    engineer.run_task(store, on, {}, tid, session=session, suite=green, source=repo)
    assert session.calls[0]["budget"] == pytest.approx(2.0)
    store.log_run("engineer", loop.Run(cost_usd=5.0))
    later = task(store, on, "Add a three() helper to util with a test.")
    refused = engineer.run_task(store, on, {}, later, session=coder(GOOD), suite=green, source=repo)
    assert refused["retry_later"] and "daily budget" in refused["error"] and store.proposal(later)["state"] == "proposed"


def test_a_session_that_spends_the_task_budget_gets_no_review(store: Any, on: Any, repo: Path, posts: Any) -> None:
    session = coder(GOOD, cost=on.budgets.engineer_per_task)
    tid = task(store, on)
    result = engineer.run_task(store, on, {}, tid, session=session, suite=green, source=repo)
    assert result["outcome"] == "stopped" and not any(c["review"] for c in session.calls)


def test_a_failing_session_is_recorded_and_can_run_again(store: Any, on: Any, repo: Path, posts: Any) -> None:
    def broken(root: Path, prompt: str, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("the model went away")

    tid = task(store, on)
    result = engineer.run_task(store, on, {}, tid, session=broken, suite=green, source=repo)
    assert result["state"] == "failed" and "went away" in result["error"]
    assert store.runs(routine="engineer")[0]["state"] == "failed"
    again = engineer.run_task(store, on, {}, tid, session=coder(GOOD), suite=green, source=repo)
    assert again["outcome"] == "ready"


def test_the_rest_api_when_gh_is_missing() -> None:
    calls: list[tuple[str, str, Any]] = []

    def http(method: str, url: str, *, token: str, body: Any = None) -> Any:
        calls.append((method, url, body))
        return {"html_url": "https://github.com/acme/tw/pull/9"} if method == "POST" else {"merged": True}

    hub = engineer.GitHub("acme/tw", "token", http=http, use_gh=False)
    url = hub.create_draft(root=Path("."), branch="desk/task-3", base="main", title="tailwatch desk: x", body="b")
    assert url.endswith("/pull/9")
    assert calls[0] == ("POST", "https://api.github.com/repos/acme/tw/pulls",
                        {"title": "tailwatch desk: x", "head": "desk/task-3", "base": "main", "body": "b", "draft": True})
    assert hub.state(url) == "merged" and calls[1][:2] == ("GET", "https://api.github.com/repos/acme/tw/pulls/9")
    with pytest.raises(engineer.EngineerError):
        engineer.GitHub("acme/tw", "", http=http, use_gh=False).create_draft(root=Path("."), branch="b", base="main", title="t", body="")


def test_close_and_settle(store: Any, config: Any) -> None:
    closed: list[tuple[str, str]] = []

    class Hub:
        def close(self, url: str, reason: str) -> None:
            closed.append((url, reason))

        def state(self, url: str) -> str:
            return "closed"

    tid = task(store, config)
    store.set_proposal(tid, state="open", pr_url="https://github.com/acme/tw/pull/72")
    assert "say why" in maintainer.decide(store, config, {}, tid, "close", by="owner")["error"]
    done = engineer.close_task(store, config, {}, tid, reason="not needed", by="owner", github=Hub())  # type: ignore[arg-type]
    assert done["state"] == "rejected" and closed == [("https://github.com/acme/tw/pull/72", "Closed from the desk by owner: not needed")]
    other = task(store, config, "Add a three() helper to util with a test.")
    store.set_proposal(other, state="open", pr_url="https://github.com/acme/tw/pull/73")
    assert engineer.settle(store, config, {}, github=Hub()) == {"merged": [], "closed": [other]}  # type: ignore[arg-type]
    assert store.proposal(other)["state"] == "rejected" and store.proposal(other)["decided_by"] == "github"
    parked = task(store, config, "Add a four() helper to util with a test.")
    assert maintainer.decide(store, config, {}, parked, "reject", note="not now", by="owner")["state"] == "rejected"


def test_the_native_session_runs_the_loop_with_the_checkout_tools(model: Any, config: Any, tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    replies = iter([turn("", ("write_file", {"path": "docs/note.md", "content": "hello\n"}),
                         ("write_file", {"path": ".github/x.yml", "content": "no"})),
                    turn(json.dumps(REPORT))])
    adapter = model(lambda request: next(replies))
    result = engineer_runner.native_session(tmp_path, "do it", system="sys", review=False, budget_usd=1.0, env={},
                                            config=config)
    assert result["answer"]["outcome"] == "fixed" and (tmp_path / "docs" / "note.md").read_text() == "hello\n"
    assert not (tmp_path / ".github").exists()
    names = {t["name"] for t in adapter.requests[0].tools}
    assert names == {"read_file", "list_files", "search", "edit_file", "write_file", "run"}
    tool_result = next(m for m in adapter.requests[1].messages if m["role"] == "tool" and "protected" in m["content"])
    assert tool_result["is_error"]
    review_replies = iter([turn(json.dumps({"verdict": "ship", "reasons": []}))])
    reviewer = model(lambda request: next(review_replies))
    engineer_runner.native_session(tmp_path, "review it", system="sys", review=True, budget_usd=1.0, env={}, config=config)
    assert {t["name"] for t in reviewer.requests[0].tools} == {"read_file", "list_files", "search"}


def test_the_checkout_tools_stay_inside(tmp_path: Path) -> None:
    (tmp_path / "src" / "tailwatch").mkdir(parents=True)
    (tmp_path / "src" / "tailwatch" / "a.py").write_text("x = 1\n")
    box = engineer_runner.Checkout(tmp_path, allowed_paths=["src/tailwatch/", "tests/"], protected_paths=[".github/"])
    assert "outside the checkout" in box.call("read_file", {"path": "../secret.txt"})["error"]
    assert ".git" in box.call("read_file", {"path": ".git/config"})["error"]
    assert "outside the paths" in box.call("write_file", {"path": "setup.py", "content": "x"})["error"]
    assert "packet" in box.call("write_file", {"path": ".engineer/task.json", "content": "{}"})["error"]
    assert box.call("edit_file", {"path": "src/tailwatch/a.py", "old": "x = 1", "new": "x = 2"})["ok"]
    assert box.call("search", {"pattern": "x = 2"})["matches"] == ["src/tailwatch/a.py:1:x = 2"]
    reader = engineer_runner.Checkout(tmp_path, allowed_paths=["src/"], protected_paths=[], review=True)
    assert "no tool" in reader.call("write_file", {"path": "src/b.py", "content": ""})["error"]


@pytest.mark.parametrize("command", ["curl https://example.com", "pip install requests", "git push origin main",
                                     "python -m pytest; rm -rf /", "pytest | tee out", "python -c 'print(1)'",
                                     "bash -c pytest", "python -m http.server", "echo $HOME"])
def test_run_refuses_anything_but_the_tests_and_the_linter(command: str) -> None:
    with pytest.raises(ValueError):
        engineer_runner.command_argv(command)


def test_run_allows_the_tests_and_the_linter() -> None:
    assert engineer_runner.command_argv("python -m pytest -q tests/test_x.py")[1:] == ["-m", "pytest", "-q", "tests/test_x.py"]
    assert engineer_runner.command_argv("ruff check src")[1:] == ["-m", "ruff", "check", "src"]


def test_the_sandbox_environment_holds_no_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = engineer_runner.sandbox_env(tmp_path, {"PATH": "/bin", "ANTHROPIC_API_KEY": "k", "GITHUB_TOKEN": "t"})
    assert "ANTHROPIC_API_KEY" not in env and "GITHUB_TOKEN" not in env and env["HOME"] == str(tmp_path)
    assert env["HTTPS_PROXY"] == engineer_runner.NOWHERE_PROXY
    monkeypatch.setenv("SOME_SERVICE_TOKEN", "secret-value")
    claude = engineer_runner.claude_env(tmp_path, {"ANTHROPIC_API_KEY": "k", "GITHUB_TOKEN": "t"})
    assert claude["ANTHROPIC_API_KEY"] == "k" and claude["SOME_SERVICE_TOKEN"] == "" and "GITHUB_TOKEN" not in claude


def test_the_real_suite_runs_in_the_sandbox(repo: Path) -> None:
    ok, output = engineer.run_suite(repo)
    assert ok, output


def test_backend_choice(config: Any) -> None:
    assert engineer.backend(config, {}) == "native"
    assert engineer.backend(config, {"CLAUDE_CODE_OAUTH_TOKEN": "x"}) == "claude"
    config.engineer.backend = "native"
    assert engineer.backend(config, {"ANTHROPIC_API_KEY": "x"}) == "native"


def test_the_evaluation_files_follow_the_schema(config: Any) -> None:
    files = sorted((Path(__file__).parent / "engineer_tasks").glob("*.json"))
    assert len(files) >= 3
    for path in files:
        data = json.loads(path.read_text())
        assert engineer.check_eval_task(data) == [], path.name
        for changed in data["must_change"]:
            assert engineer_runner.allowed(changed, config.engineer.allowed_paths), (path.name, changed)
            assert not engineer_runner.protected(changed, config.engineer.protected_paths), (path.name, changed)
    assert engineer.check_eval_task({"task": "x", "must_change": [], "extra": 1}) != []


def test_run_eval_on_a_copy_logs_each_task(store: Any, config: Any, repo: Path, tmp_path: Path) -> None:
    folder = tmp_path / "tasks"
    folder.mkdir()
    (folder / "two.json").write_text(json.dumps({"task": "Add a two() helper to util with a test.",
                                                 "must_change": ["src/tailwatch/util.py", "tests/"], "must_contain": ["def two"]}))
    (folder / "three.json").write_text(json.dumps({"task": "Add a three() helper to util with a test.",
                                                   "must_change": ["src/tailwatch/util.py"], "must_contain": ["def three"]}))
    result = engineer.run_eval(store, config, {}, session=coder(GOOD, GOOD), suite=green, folder=folder, source=repo)
    by_name = {r["name"]: r for r in result["results"]}
    assert result["total"] == 2 and by_name["two"]["passed"] and not by_name["three"]["passed"]
    assert "def three" in by_name["three"]["failures"][0]
    assert {r["subject"] for r in store.runs(routine="engineer")} == {"eval:two", "eval:three"}
    assert "def two" not in (repo / "src" / "tailwatch" / "util.py").read_text()  # the copy, never the source


def test_the_claude_session_uses_the_agent_sdk(monkeypatch: pytest.MonkeyPatch, config: Any, tmp_path: Path) -> None:
    import claude_agent_sdk
    from claude_agent_sdk import ResultMessage

    seen: list[Any] = []

    async def fake_query(*, prompt: str, options: Any) -> Any:
        seen.append((prompt, options))
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=4, session_id="abc",
                            total_cost_usd=0.75, result="Done.\n```json\n" + json.dumps(REPORT) + "\n```")

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    result = engineer_runner.claude_session(tmp_path, "do it", system="sys", review=False, budget_usd=3.0,
                                            env={"ANTHROPIC_API_KEY": "k"}, config=config)
    assert result["answer"]["outcome"] == "fixed" and result["cost_usd"] == 0.75 and result["session_id"] == "abc"
    prompt, options = seen[0]
    assert prompt == "do it" and options.cwd == str(tmp_path) and options.permission_mode == "acceptEdits"
    assert options.allowed_tools == ["Read", "Edit", "Write", "Bash", "Grep", "Glob"]
    assert {"WebFetch", "WebSearch", "Task", "Agent"} <= set(options.disallowed_tools)
    assert options.max_turns == 60 and options.max_budget_usd == 3.0 and options.setting_sources == []
    assert options.system_prompt == "sys" and options.env["ANTHROPIC_API_KEY"] == "k"
    review = engineer_runner.claude_session(tmp_path, "review", system="sys", review=True, budget_usd=1.0, env={}, config=config)
    assert review["answer"] is None and "verdict" in review["error"]
    assert seen[1][1].allowed_tools == ["Read", "Grep", "Glob"] and "Bash" in seen[1][1].disallowed_tools
