from __future__ import annotations

from typing import Any

from tailwatch.connectors.base import CheckContext
from tailwatch.connectors.github import GitHubChecker
from tailwatch.connectors.http import Call, Http, HttpError, Response, fixture_transport

URL = "https://github.com/ada"
USER = {"login": "Ada", "name": "Ada Lovelace", "bio": "Engines.", "company": "@analytical",
        "location": "London", "blog": "https://ada.dev", "twitter_username": "ada", "hireable": None,
        "public_repos": 2, "followers": 1234}
REPOS = [{"name": "engine", "description": "Difference engine", "created_at": "2026-01-02T00:00:00Z",
          "html_url": "https://github.com/ada/engine", "stargazers_count": 1234, "fork": False},
         {"name": "notes", "description": None, "created_at": "2025-05-01T00:00:00Z",
          "html_url": "https://github.com/ada/notes", "stargazers_count": 3, "fork": False},
         {"name": "forked", "created_at": "2026-02-01T00:00:00Z", "fork": True}]


def ctx(routes: dict[str, Any], env: dict[str, str] | None = None, **options: Any) -> CheckContext:
    return CheckContext(http=Http(transport=fixture_transport(routes)), env=env or {}, options=options)


def routes(user: dict | None = None, repos: list | None = None) -> dict[str, Any]:
    def repos_route(call: Call) -> Any:
        return repos if repos is not None else REPOS
    return {"https://api.github.com/users/ada/repos": repos_route,
            "https://api.github.com/users/ada": user or USER}


def test_first_read_normalises_the_profile() -> None:
    read = GitHubChecker().read(URL, None, ctx(routes()))
    assert read.status == "ok"
    content = read.content
    assert content["name"] == "Ada Lovelace" and content["organization"] == "@analytical"
    assert content["website"] == "https://ada.dev" and content["x_handle"] == "ada"
    assert content["flags"] == {"hireable": False}
    assert [r["name"] for r in content["repos"]] == ["engine", "notes"]  # forks dropped, newest first
    assert content["repos"][0]["stars"] == 1200  # rounded, so stars do not churn the content
    assert "releases" not in content  # anonymous: releases are off by default
    assert "followers" not in content


def test_unchanged_read_gives_no_changes() -> None:
    checker = GitHubChecker()
    first = checker.read(URL, None, ctx(routes())).content
    second = checker.read(URL, first, ctx(routes())).content
    assert first == second and checker.diff(first, second) == []


def test_each_profile_field_change() -> None:
    checker = GitHubChecker()
    before = checker.read(URL, None, ctx(routes())).content
    user = {**USER, "bio": "Now at a new lab.", "company": "NewLab", "hireable": True, "blog": "https://new.dev",
            "location": "Paris", "name": "Ada L.", "twitter_username": "ada_l"}
    after = checker.read(URL, before, ctx(routes(user=user))).content
    labels = [c.label for c in checker.diff(before, after)]
    assert labels == ["name changed", "bio changed", "company changed", "location changed", "website changed",
                      "x handle changed", "hireable flag changed"]
    bio = next(c for c in checker.diff(before, after) if c.label == "bio changed")
    assert (bio.before, bio.after) == ("Engines.", "Now at a new lab.")


def test_new_repository() -> None:
    checker = GitHubChecker()
    before = checker.read(URL, None, ctx(routes())).content
    new = {"name": "agent", "description": "An agent", "created_at": "2026-03-01T00:00:00Z",
           "html_url": "https://github.com/ada/agent", "stargazers_count": 0}
    after = checker.read(URL, before, ctx(routes(repos=[new, *REPOS]))).content
    changes = checker.diff(before, after)
    assert [c.label for c in changes] == ["new repository"]
    assert "agent: An agent" in changes[0].after and changes[0].content_date == "2026-03-01"


def test_an_old_repository_sliding_into_a_full_window_is_not_new() -> None:
    checker = GitHubChecker()
    full = [{"name": f"r{i}", "created_at": f"2026-01-{10 + i:02d}T00:00:00Z"} for i in range(10)]
    before = checker.read(URL, None, ctx(routes(repos=full))).content
    slid = [*full[1:], {"name": "old", "created_at": "2020-01-01T00:00:00Z"}]  # r0 deleted
    after = checker.read(URL, before, ctx(routes(repos=slid))).content
    assert checker.diff(before, after) == []


def test_new_release_with_a_token() -> None:
    def releases(tag: str, rid: int, date: str) -> list[dict]:
        return [{"id": rid, "tag_name": tag, "name": tag, "published_at": date,
                 "html_url": f"https://github.com/ada/engine/releases/{tag}"}]

    pushed = [{"name": "engine", "stargazers_count": 1234}, {"name": "notes", "stargazers_count": 3}]

    def all_routes(rel: list[dict]) -> dict[str, Any]:
        def repos_route(call: Call) -> Any:
            return pushed if call.params.get("sort") == "pushed" else REPOS
        return {"https://api.github.com/users/ada/repos": repos_route, "https://api.github.com/users/ada": USER,
                "https://api.github.com/repos/ada/engine/releases": rel, "https://api.github.com/repos/ada/notes/releases": []}

    env = {"GITHUB_TOKEN": "t1"}
    checker = GitHubChecker()
    before = checker.read(URL, None, ctx(all_routes(releases("v1", 1, "2026-01-01T00:00:00Z")), env)).content
    assert [r["tag"] for r in before["releases"]] == ["v1"]
    after = checker.read(URL, before, ctx(all_routes(
        releases("v2", 2, "2026-02-01T00:00:00Z") + releases("v1", 1, "2026-01-01T00:00:00Z")), env)).content
    changes = checker.diff(before, after)
    assert [c.label for c in changes] == ["new release"]
    assert changes[0].after.startswith("engine v2") and changes[0].content_date == "2026-02-01"


def test_404_is_gone() -> None:
    read = GitHubChecker().read(URL, None, ctx({"https://api.github.com/users/ada": (404, {"message": "Not Found"})}))
    assert read.status == "gone"


def test_server_errors_and_timeouts_are_errors_not_gone() -> None:
    assert GitHubChecker().read(URL, None, ctx({"https://api.github.com/users/ada": (502, "bad gateway")})).status == "error"

    def timeout(call: Call) -> Any:
        raise HttpError("timeout reading")
    read = GitHubChecker().read(URL, None, ctx({"https://api.github.com/users/ada": timeout}))
    assert read.status == "error" and "timeout" in read.error


def test_a_rate_limited_token_rotates_to_the_next() -> None:
    used: list[str] = []

    def user(call: Call) -> Response:
        used.append(call.headers.get("Authorization", ""))
        if call.headers.get("Authorization") == "Bearer t1":
            return Response(403, '{"message": "API rate limit exceeded"}', {"X-RateLimit-Remaining": "0",
                                                                          "X-RateLimit-Reset": "9999999999"})
        return Response(200, __import__("json").dumps(USER))
    checker = GitHubChecker()
    read = checker.read(URL, None, ctx({"https://api.github.com/users/ada/repos": REPOS,
                                        "https://api.github.com/users/ada": user}, {"GITHUB_TOKEN": "t1, t2"}))
    assert read.status == "ok" and used == ["Bearer t1", "Bearer t2"]
    # the exhausted token is set aside for the next read
    checker.read(URL, None, ctx({"https://api.github.com/users/ada/repos": REPOS,
                                 "https://api.github.com/users/ada": user}, {"GITHUB_TOKEN": "t1,t2"}))
    assert used[2:] == ["Bearer t2"]


def test_every_credential_rate_limited_is_an_outage() -> None:
    read = GitHubChecker().read(URL, None, ctx({"https://api.github.com/users/ada": (429, "slow down")}))
    assert read.status == "error" and read.outage == "rate limited"


def test_empty_repository_list_for_an_account_with_repos_is_guarded() -> None:
    checker = GitHubChecker()
    before = checker.read(URL, None, ctx(routes())).content
    after = checker.read(URL, before, ctx(routes(repos=[]))).content
    assert checker.guard(before, after)
    assert checker.guard(before, before) == ""


def test_not_a_profile_url_is_an_error() -> None:
    assert GitHubChecker().read("https://gitlab.com/ada", None, ctx({})).status == "error"


def test_describe_is_compact_text() -> None:
    checker = GitHubChecker()
    text = checker.describe(checker.read(URL, None, ctx(routes())).content)
    assert "Ada Lovelace" in text and "engine" in text and len(text) <= 4000
