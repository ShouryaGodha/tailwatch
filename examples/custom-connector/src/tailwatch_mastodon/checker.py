"""Mastodon: a person's profile and newest own posts, through their instance's public API.

No key: every Mastodon instance answers these two calls for a public account.

  GET https://<instance>/api/v1/accounts/lookup?acct=<user>
  GET https://<instance>/api/v1/accounts/<id>/statuses?limit=20&exclude_reblogs=true

A source URL is the profile, ``https://<instance>/@<user>``.
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping
from typing import Any, ClassVar
from urllib.parse import urlsplit

from tailwatch.connectors.base import BaseChecker, Change, CheckContext, Read
from tailwatch.connectors.http import HttpError

POSTS = 20


def account_of(url: str) -> tuple[str, str]:
    """``https://mastodon.social/@ada`` → ``("mastodon.social", "ada")``; ``@ada@mastodon.social``
    works too. ``("", "")`` when it is neither."""
    text = url.strip()
    if found := re.fullmatch(r"@?([\w.]+)@([\w.-]+\.[a-z]{2,})", text, re.IGNORECASE):
        return found.group(2).lower(), found.group(1)
    parts = urlsplit(text if "://" in text else f"https://{text}")
    segments = [s for s in parts.path.split("/") if s]
    if parts.hostname and segments and segments[0].startswith("@"):
        return parts.hostname.lower(), segments[0][1:]
    return "", ""


def plain(markup: Any) -> str:
    """Mastodon sends bios and posts as HTML: tags out, entities decoded, whitespace collapsed."""
    text = re.sub(r"<br\s*/?>|</p>", " ", str(markup or ""))
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", text)).split())


class MastodonChecker(BaseChecker):
    kind: ClassVar[str] = "mastodon"
    label: ClassVar[str] = "Mastodon"
    cadence_hours: ClassVar[float] = 24.0
    paid: ClassVar[bool] = False
    requires: ClassVar[tuple[str, ...]] = ()

    def read(self, url: str, previous: Mapping[str, Any] | None, ctx: CheckContext) -> Read:
        instance, user = account_of(url)
        if not instance:
            return Read.failed(f"not a Mastodon profile URL: {url!r}")
        try:
            account = ctx.http.get(f"https://{instance}/api/v1/accounts/lookup", params={"acct": user})
            if account.status in (404, 410):
                return Read(status="gone", error=f"{instance} has no account {user}")  # the instance says so
            if account.status != 200:
                # 429, 5xx, a down instance: an error, retried; never "gone" on a guess
                return Read.failed(
                    f"{instance} answered HTTP {account.status}",
                    outage="rate limited" if account.status == 429 else "",
                )
            profile = account.json()
            statuses = ctx.http.get(
                f"https://{instance}/api/v1/accounts/{profile['id']}/statuses",
                params={"limit": POSTS, "exclude_reblogs": "true"},
            )
            if statuses.status != 200:
                return Read.failed(f"{instance} statuses answered HTTP {statuses.status}")
            posts_json = statuses.json()
        except HttpError as error:
            return Read.failed(str(error))
        except (ValueError, KeyError) as error:
            return Read.failed(f"{instance} answered an unexpected shape: {error}")
        if profile.get("suspended"):
            return Read(status="gone", error=f"{user}@{instance} is suspended")
        posts = [
            {
                "id": str(p["id"]),
                "text": plain(p.get("content")),
                "date": str(p.get("created_at") or ""),
                "url": str(p.get("url") or ""),
            }
            for p in posts_json
            if isinstance(p, dict) and p.get("id") and not p.get("reblog")
        ]
        posts.sort(key=lambda p: (p["date"], p["id"]), reverse=True)
        # Keep only what means something: no follower counts, which change on their own.
        content = {
            "handle": f"{user}@{instance}".lower(),
            "name": " ".join(str(profile.get("display_name") or "").split()),
            "bio": plain(profile.get("note")),
            "posts": posts[:POSTS],
        }
        return Read(status="ok", content=content, raw=profile)

    def guard(self, before: Mapping[str, Any], after: Mapping[str, Any]) -> str:
        if before.get("posts") and not after.get("posts"):
            return "the timeline came back empty for an account that had posts"
        return ""

    def diff(self, before: Mapping[str, Any], after: Mapping[str, Any]) -> list[Change]:
        changes = []
        for field, label in (("name", "name changed"), ("bio", "bio changed")):
            if (before.get(field) or "") != (after.get(field) or ""):
                changes.append(Change(label, str(before.get(field) or ""), str(after.get(field) or "")))
        old = before.get("posts") or []
        seen = {p["id"] for p in old}
        oldest = min((p["date"] for p in old), default="")  # older unseen posts slid in after a deletion
        for post in sorted(after.get("posts") or [], key=lambda p: p["date"]):
            if post["id"] not in seen and post["date"] >= oldest:
                changes.append(
                    Change("new post", "", f"{post['text']}\n{post['url']}".strip(), post["date"][:10])
                )
        return changes

    def describe(self, content: Mapping[str, Any]) -> str:
        lines = [
            f"Mastodon @{content.get('handle', '')}: {content.get('name', '')}",
            f"Bio: {content.get('bio', '')}",
        ]
        lines += [f"- {p['date'][:10]} {p['text']}" for p in content.get("posts") or []]
        return "\n".join(lines)[:4000]
