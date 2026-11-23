"""A Bluesky announcer account (a lab, a newsletter, a person who posts about moves): its new
posts, with the accounts each one mentions, through the public AppView (no key).

Options: ``handle`` (required). Mentioned handles come from the post's facets (a mention facet
covers the ``@handle`` text) and links from link facets and link cards. The announcer itself
is never a mention. The marker is the newest post's ``indexedAt`` time.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from tailwatch import util
from tailwatch.connectors._text import clean, iso_date, mentions
from tailwatch.connectors.base import CheckContext, FeedItem
from tailwatch.connectors.bluesky import APPVIEW, handle_of, post_url
from tailwatch.connectors.feeds import FeedError
from tailwatch.connectors.http import HttpError

PAGE = 50


def facet_links(record: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """``(mentioned handles, linked urls)`` of a post record."""
    text = str(record.get("text") or "").encode("utf-8")
    handles: list[str] = []
    urls: list[str] = []
    for facet in record.get("facets") or []:
        index = facet.get("index") or {}
        for feature in facet.get("features") or []:
            kind = str(feature.get("$type") or "")
            if kind.endswith("#mention"):
                start, end = int(index.get("byteStart") or 0), int(index.get("byteEnd") or 0)
                handle = text[start:end].decode("utf-8", errors="ignore").strip().lstrip("@").lower()
                if handle:
                    handles.append(handle)
            elif kind.endswith("#link") and feature.get("uri"):
                urls.append(str(feature["uri"]))
    external = ((record.get("embed") or {}).get("external") or {}).get("uri")
    if external:
        urls.append(str(external))
    return handles, urls


class BlueskyAccountFeed:
    kind: ClassVar[str] = "bluesky_account"
    label: ClassVar[str] = "Bluesky announcer account"
    paid: ClassVar[bool] = False
    requires: ClassVar[tuple[str, ...]] = ()

    def poll(
        self, marker: str | None, options: Mapping[str, Any], ctx: CheckContext
    ) -> tuple[list[FeedItem], str]:
        handle = (
            handle_of(str(options.get("handle") or "")) or clean(options.get("handle")).lstrip("@").lower()
        )
        if not handle:
            raise FeedError("the bluesky_account feed needs options.handle")
        try:
            response = ctx.http.get(
                f"{APPVIEW}/app.bsky.feed.getAuthorFeed",
                params={"actor": handle, "limit": PAGE, "filter": "posts_no_replies"},
            )
        except HttpError as error:
            raise FeedError(str(error)) from error
        if response.status != 200:
            raise FeedError(
                f"Bluesky answered HTTP {response.status}",
                outage="rate limited" if response.status == 429 else "",
            )
        try:
            feed = response.json().get("feed") or []
        except (ValueError, AttributeError) as error:
            raise FeedError("Bluesky answered something that is not JSON") from error
        posts = []
        for entry in feed:
            post = (entry or {}).get("post") or {}
            if entry.get("reason") or not post.get("uri"):
                continue  # a repost or a pin
            posts.append(post)
        indexed = [iso_date(p.get("indexedAt")) for p in posts]
        newest = max(indexed, default="")
        if marker is None:
            return [], newest or util.iso()
        items = []
        for post in sorted(posts, key=lambda p: iso_date(p.get("indexedAt"))):
            at = iso_date(post.get("indexedAt"))
            if at <= marker:
                continue
            record = post.get("record") or {}
            text = clean(record.get("text"))
            handles, urls = facet_links(record)
            author = ((post.get("author") or {}).get("handle") or handle).lower()
            items.append(
                FeedItem(
                    id=str(post["uri"]),
                    url=post_url(author, str(post["uri"])),
                    text=text,
                    author=author,
                    published_at=iso_date(record.get("createdAt")) or at,
                    mentions=mentions(
                        text, urls, bluesky_handles=handles, exclude={"bluesky": [handle, author]}
                    ),
                    raw=post,
                )
            )
        return items, max(newest, marker)
