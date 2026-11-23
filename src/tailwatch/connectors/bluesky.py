"""Bluesky: a person's profile and newest own posts, through the public AppView (no key).

- ``app.bsky.actor.getProfile?actor=<handle>``: display name, description.
- ``app.bsky.feed.getAuthorFeed?actor=<handle>&limit=20``: posts; reposts and posts by
  others (a reply thread's root) are left out.

``gone`` is the AppView saying the profile does not exist or the account was taken down or
deactivated. Everything else is an error.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from tailwatch.connectors._text import clean, clip, iso_date, text_change, unseen
from tailwatch.connectors.base import BaseChecker, Change, CheckContext, Read
from tailwatch.connectors.http import HttpError, Response

APPVIEW = "https://public.api.bsky.app/xrpc"
POSTS = 20
GONE_ERRORS = (
    "profile not found",
    "accounttakedown",
    "accountdeactivated",
    "actor not found",
    "unable to resolve handle",
    "could not find user",
)


def handle_of(url: str) -> str:
    text = str(url or "").strip().rstrip("/")
    if "bsky.app/profile/" in text:
        return text.split("bsky.app/profile/", 1)[1].split("/", 1)[0].lower()
    return text.lstrip("@").lower() if "." in text and "/" not in text else ""


def post_url(handle: str, uri: str) -> str:
    """``at://did:plc:x/app.bsky.feed.post/3k...`` → the bsky.app link to that post."""
    return f"https://bsky.app/profile/{handle}/post/{uri.rsplit('/', 1)[-1]}" if uri else ""


def own_posts(feed: Any, did: str, handle: str) -> list[dict[str, Any]]:
    """The author's own posts in a getAuthorFeed answer, newest first."""
    posts = []
    for item in (feed or {}).get("feed") or []:
        if not isinstance(item, dict) or item.get("reason"):  # a repost (or a pinned repeat)
            continue
        post = item.get("post") or {}
        author = post.get("author") or {}
        if did and author.get("did") != did:
            continue
        record = post.get("record") or {}
        uri = str(post.get("uri") or "")
        if not uri:
            continue
        posts.append(
            {
                "id": uri,
                "text": clean(record.get("text")),
                "date": iso_date(record.get("createdAt") or post.get("indexedAt")),
                "url": post_url(handle, uri),
            }
        )
    posts.sort(key=lambda p: (p["date"], p["id"]), reverse=True)
    return posts


def _error_text(response: Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    return f"{body.get('error', '')} {body.get('message', '')}".strip() if isinstance(body, dict) else ""


class BlueskyChecker(BaseChecker):
    kind: ClassVar[str] = "bluesky"
    label: ClassVar[str] = "Bluesky"
    cadence_hours: ClassVar[float] = 24.0
    paid: ClassVar[bool] = False
    requires: ClassVar[tuple[str, ...]] = ()

    def read(self, url: str, previous: Mapping[str, Any] | None, ctx: CheckContext) -> Read:
        handle = handle_of(url)
        if not handle:
            return Read.failed(f"not a Bluesky profile: {url!r}")
        try:
            profile = ctx.http.get(f"{APPVIEW}/app.bsky.actor.getProfile", params={"actor": handle})
            if profile.status != 200:
                message = _error_text(profile)
                if profile.status in (400, 404) and any(g in message.lower() for g in GONE_ERRORS):
                    return Read(status="gone", error=f"Bluesky: {message}")
                outage = "rate limited" if profile.status == 429 else ""
                return Read.failed(
                    f"Bluesky profile answered HTTP {profile.status}: {message}".strip(), outage=outage
                )
            feed = ctx.http.get(
                f"{APPVIEW}/app.bsky.feed.getAuthorFeed",
                params={"actor": handle, "limit": POSTS, "filter": "posts_with_replies"},
            )
            if feed.status != 200:
                return Read.failed(
                    f"Bluesky feed answered HTTP {feed.status}: {_error_text(feed)}".strip(),
                    outage="rate limited" if feed.status == 429 else "",
                )
            user, posts_json = profile.json(), feed.json()
        except HttpError as error:
            return Read.failed(str(error))
        except ValueError as error:
            return Read.failed(f"Bluesky answered something that is not JSON: {error}")
        did = str(user.get("did") or "")
        content = {
            "handle": clean(user.get("handle")).lower() or handle,
            "did": did,
            "name": clean(user.get("displayName")),
            "bio": clean(user.get("description")),
            "posts": own_posts(posts_json, did, handle)[:POSTS],
        }
        return Read(status="ok", content=content, raw={"profile": user})

    def guard(self, before: Mapping[str, Any], after: Mapping[str, Any]) -> str:
        if before.get("posts") and not after.get("posts"):
            return "the feed came back empty for an account that had posts"
        return ""

    def diff(self, before: Mapping[str, Any], after: Mapping[str, Any]) -> list[Change]:
        if before.get("did") and after.get("did") and before["did"] != after["did"]:
            return []  # a different account behind the handle: a new baseline, not a change
        changes = text_change("name changed", before.get("name"), after.get("name"))
        changes += text_change("bio changed", before.get("bio"), after.get("bio"))
        # Posts are never backdated, so anything older than the oldest post seen slid into the
        # window after a deletion (or a repost dropped out): never new.
        for post in unseen(before.get("posts") or [], after.get("posts") or [], cap=1):
            changes.append(
                Change(
                    "new post",
                    "",
                    f"{post.get('text')}\n{post.get('url')}".strip(),
                    str(post.get("date") or "")[:10],
                )
            )
        return changes

    def describe(self, content: Mapping[str, Any]) -> str:
        lines = [f"Bluesky @{content.get('handle', '')}: {content.get('name', '')}"]
        if content.get("bio"):
            lines.append(f"Bio: {content['bio']}")
        for post in content.get("posts") or []:
            lines.append(f"- {str(post.get('date'))[:10]} {post.get('text')}")
        return clip("\n".join(lines), 4000)
