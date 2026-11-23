"""GitHub: a person's public profile, their newest repositories and recent releases.

Calls per read, all on the REST API (``https://api.github.com``):

- ``/users/<login>``: the profile (404 is ``gone``).
- ``/users/<login>/repos?sort=created&per_page=10``: the newest repositories they own.
- with releases on: ``/users/<login>/repos?sort=pushed`` and ``/repos/<login>/<repo>/releases``
  for the most starred recently pushed repositories (``release_repos`` of them).

Credentials are optional. ``GITHUB_TOKEN`` may hold several tokens separated by commas; a token
that answers with an exhausted rate limit (403 with ``x-ratelimit-remaining: 0``, or 429) is
set aside until its reset time and the call moves to the next. Anonymous calls get 60 an hour,
so releases are only read by default when a token is set.

Options (``connectors: {github: {options: {...}}}``):
  ``release_repos``: how many repositories to read releases of (default 3 with a token, 0 without).
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, ClassVar

from tailwatch.connectors._text import clean, clip, flag_change, round2, text_change, unseen
from tailwatch.connectors.base import BaseChecker, Change, CheckContext, Read
from tailwatch.connectors.http import HttpError, Response

API = "https://api.github.com"
REPOS = 10  # the newest repositories kept per read
RELEASES_PER_REPO = 3
RELEASES = 20
PROFILE_FIELDS = (
    ("name", "name changed"),
    ("bio", "bio changed"),
    ("organization", "company changed"),
    ("location", "location changed"),
    ("website", "website changed"),
    ("x_handle", "x handle changed"),
)


class _RateLimited(Exception):
    pass


def login_of(url: str) -> str:
    """``https://github.com/<login>`` → ``<login>``; ``""`` when the URL is not a profile."""
    text = str(url or "").split("://", 1)[-1]
    host, _, path = text.partition("/")
    if host.lower().removeprefix("www.") != "github.com":
        return ""
    return path.split("/", 1)[0].split("?", 1)[0].strip().lstrip("@").lower()


class GitHubChecker(BaseChecker):
    kind: ClassVar[str] = "github"
    label: ClassVar[str] = "GitHub"
    cadence_hours: ClassVar[float] = 24.0
    paid: ClassVar[bool] = False
    requires: ClassVar[tuple[str, ...]] = ()

    def __init__(self) -> None:
        # token index -> epoch second its rate limit resets; lives as long as the checker
        self._exhausted: dict[int, float] = {}

    # ------------------------------------------------------------------ HTTP

    def _get(self, ctx: CheckContext, path: str, params: Mapping[str, Any] | None = None) -> Response:
        """GET through the first token that is not rate limited (anonymous when there is none).
        Raises ``_RateLimited`` when every credential is exhausted."""
        tokens = [t.strip() for t in str(ctx.env.get("GITHUB_TOKEN") or "").split(",") if t.strip()]
        credentials: list[tuple[int, dict[str, str]]] = [
            (i, {"Authorization": f"Bearer {t}"}) for i, t in enumerate(tokens)
        ] or [(-1, {})]
        now = time.time()
        for index, auth in credentials:
            if self._exhausted.get(index, 0) > now:
                continue
            headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", **auth}
            response = ctx.http.get(f"{API}{path}", params=params or {}, headers=headers)
            lowered = {str(k).lower(): str(v) for k, v in (response.headers or {}).items()}
            limited = response.status == 429 or (
                response.status == 403
                and (lowered.get("x-ratelimit-remaining") == "0" or "rate limit" in response.text.lower())
            )
            if limited:
                reset = lowered.get("x-ratelimit-reset", "")
                self._exhausted[index] = float(reset) if reset.isdigit() else now + 60
                continue
            return response
        raise _RateLimited("every GitHub credential is rate limited")

    # ------------------------------------------------------------------ read

    def read(self, url: str, previous: Mapping[str, Any] | None, ctx: CheckContext) -> Read:
        login = login_of(url)
        if not login:
            return Read.failed(f"not a GitHub profile URL: {url!r}")
        has_token = bool(str(ctx.env.get("GITHUB_TOKEN") or "").strip())
        release_repos = int(ctx.options.get("release_repos", 3 if has_token else 0) or 0)
        try:
            user = self._get(ctx, f"/users/{login}")
            if user.status == 404:
                return Read(status="gone", error=f"GitHub says {login} does not exist")
            if user.status != 200:
                return Read.failed(f"GitHub user {login} answered HTTP {user.status}")
            repos = self._get(ctx, f"/users/{login}/repos", {"sort": "created", "per_page": REPOS})
            if repos.status != 200:
                return Read.failed(f"GitHub repositories of {login} answered HTTP {repos.status}")
            profile, created = user.json(), repos.json()
            releases = self._releases(ctx, login, release_repos, previous) if release_repos > 0 else None
        except _RateLimited as error:
            return Read.failed(str(error), outage="rate limited")
        except HttpError as error:
            return Read.failed(str(error))
        except ValueError as error:  # a body that is not JSON
            return Read.failed(f"GitHub answered something that is not JSON: {error}")
        if not isinstance(profile, dict) or not isinstance(created, list):
            return Read.failed("GitHub answered an unexpected shape")
        content = normalise(profile, created, releases)
        return Read(status="ok", content=content, raw={"user": profile, "repos": created})

    def _releases(
        self, ctx: CheckContext, login: str, count: int, previous: Mapping[str, Any] | None
    ) -> list[dict[str, Any]]:
        """Recent releases of the most starred recently pushed repositories. A repository whose
        releases cannot be read keeps the ones the last read had, so a failed call never makes
        old releases look new on the next read."""
        try:
            pushed = self._get(ctx, f"/users/{login}/repos", {"sort": "pushed", "per_page": 30})
            listing = pushed.json() if pushed.status == 200 else None
        except (HttpError, _RateLimited, ValueError):
            listing = None
        if not isinstance(listing, list):
            return list((previous or {}).get("releases") or [])
        candidates = [r for r in listing if isinstance(r, dict) and not r.get("fork") and r.get("name")]
        candidates.sort(key=lambda r: (-int(r.get("stargazers_count") or 0), str(r.get("name"))))
        old = [r for r in (previous or {}).get("releases") or [] if isinstance(r, dict)]
        out: list[dict[str, Any]] = []
        for repo in candidates[:count]:
            name = str(repo["name"])
            try:
                answer = self._get(ctx, f"/repos/{login}/{name}/releases", {"per_page": RELEASES_PER_REPO})
                body = answer.json() if answer.status == 200 else None
            except (HttpError, _RateLimited, ValueError):  # releases are extra: keep the last ones
                body = None
            if not isinstance(body, list):
                out += [r for r in old if r.get("repo") == name]
                continue
            for release in body:
                if not isinstance(release, dict) or release.get("draft"):
                    continue
                out.append(
                    {
                        "id": str(release.get("id") or release.get("tag_name")),
                        "repo": name,
                        "tag": clean(release.get("tag_name")),
                        "name": clean(release.get("name")),
                        "date": clean(release.get("published_at") or release.get("created_at")),
                        "url": clean(release.get("html_url")),
                    }
                )
        out.sort(key=lambda r: (r["date"], r["id"]), reverse=True)
        return out[:RELEASES]

    # ------------------------------------------------------------------ compare

    def diff(self, before: Mapping[str, Any], after: Mapping[str, Any]) -> list[Change]:
        changes: list[Change] = []
        for field, label in PROFILE_FIELDS:
            changes += text_change(label, before.get(field), after.get(field))
        changes += flag_change(
            "hireable flag changed",
            (before.get("flags") or {}).get("hireable"),
            (after.get("flags") or {}).get("hireable"),
        )
        for repo in unseen(
            before.get("repos") or [], after.get("repos") or [], cap=REPOS, key="name", date="created"
        ):
            text = repo["name"] + (f": {repo['description']}" if repo.get("description") else "")
            changes.append(
                Change(
                    "new repository",
                    "",
                    f"{text}\n{repo.get('url', '')}".strip(),
                    str(repo.get("created") or "")[:10],
                )
            )
        # Releases are compared only when both reads carried them; the newest one seen is the
        # floor, so a repository entering the top set does not replay its history.
        if "releases" in before and "releases" in after:
            old = before.get("releases") or []
            newest = max((str(r.get("date") or "") for r in old), default="")
            seen = {str(r.get("id")) for r in old}
            for release in sorted(after.get("releases") or [], key=lambda r: str(r.get("date") or "")):
                if str(release.get("id")) not in seen and str(release.get("date") or "") > newest:
                    title = f"{release.get('repo')} {release.get('tag')}".strip()
                    if release.get("name") and release.get("name") != release.get("tag"):
                        title += f" ({release['name']})"
                    changes.append(
                        Change(
                            "new release",
                            "",
                            f"{title}\n{release.get('url', '')}".strip(),
                            str(release.get("date") or "")[:10],
                        )
                    )
        return changes

    def guard(self, before: Mapping[str, Any], after: Mapping[str, Any]) -> str:
        if before.get("repos") and not after.get("repos") and int(after.get("public_repos") or 0) > 0:
            return "the repository list came back empty for an account with public repositories"
        return ""

    def describe(self, content: Mapping[str, Any]) -> str:
        lines = [f"GitHub @{content.get('login', '')}: {content.get('name', '')}".rstrip(": ")]
        for key, title in (
            ("bio", "Bio"),
            ("organization", "Company"),
            ("location", "Location"),
            ("website", "Website"),
            ("x_handle", "X"),
        ):
            if content.get(key):
                lines.append(f"{title}: {content[key]}")
        if (content.get("flags") or {}).get("hireable"):
            lines.append("Marked hireable")
        for repo in content.get("repos") or []:
            lines.append(
                f"- repo {repo['name']} (created {str(repo.get('created'))[:10]}, ~{repo.get('stars', 0)}★)"
                + (f": {repo['description']}" if repo.get("description") else "")
            )
        for release in content.get("releases") or []:
            lines.append(
                f"- release {release.get('repo')} {release.get('tag')} ({str(release.get('date'))[:10]})"
            )
        return clip("\n".join(lines), 4000)


def normalise(
    user: Mapping[str, Any], repos: list[Any], releases: list[dict[str, Any]] | None
) -> dict[str, Any]:
    """The stored content. Follower counts are left out and stars rounded: they churn on their own."""
    kept = []
    for repo in repos:
        if not isinstance(repo, dict) or not repo.get("name") or repo.get("fork"):
            continue
        kept.append(
            {
                "name": clean(repo.get("name")),
                "description": clean(repo.get("description")),
                "created": clean(repo.get("created_at")),
                "url": clean(repo.get("html_url")),
                "stars": round2(repo.get("stargazers_count")),
            }
        )
    kept.sort(key=lambda r: (r["created"], r["name"]), reverse=True)
    content: dict[str, Any] = {
        "login": clean(user.get("login")).lower(),
        "name": clean(user.get("name")),
        "bio": clean(user.get("bio")),
        "organization": clean(user.get("company")),
        "location": clean(user.get("location")),
        "website": clean(user.get("blog")),
        "x_handle": clean(user.get("twitter_username")).lower(),
        "flags": {"hireable": bool(user.get("hireable"))},
        "public_repos": int(user.get("public_repos") or 0),
        "repos": kept[:REPOS],
    }
    if releases is not None:
        content["releases"] = releases
    return content
