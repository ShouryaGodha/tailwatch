"""Any RSS or Atom feed (a lab's news page, a newsletter, a jobs board), read with the
standard library.

Options: ``url`` (required). The marker is the id (``guid`` / Atom ``id``, else the link) of
the newest entry; a poll returns the entries above it. When the marker has rolled off the
feed, every entry the feed holds is returned once.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from tailwatch.connectors._text import clean, mentions, parse_feed
from tailwatch.connectors.base import CheckContext, FeedItem
from tailwatch.connectors.feeds import FeedError
from tailwatch.connectors.http import HttpError


class RssFeed:
    kind: ClassVar[str] = "rss_feed"
    label: ClassVar[str] = "RSS or Atom feed"
    paid: ClassVar[bool] = False
    requires: ClassVar[tuple[str, ...]] = ()

    def poll(
        self, marker: str | None, options: Mapping[str, Any], ctx: CheckContext
    ) -> tuple[list[FeedItem], str]:
        url = clean(options.get("url"))
        if not url:
            raise FeedError("the rss_feed feed needs options.url")
        try:
            response = ctx.http.get(
                url, headers={"Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9"}
            )
        except HttpError as error:
            raise FeedError(str(error)) from error
        if response.status != 200:
            raise FeedError(f"{url} answered HTTP {response.status}")
        try:
            feed = parse_feed(response.text)
        except ValueError as error:
            raise FeedError(str(error)) from error
        entries = feed.entries  # newest first
        if marker is None:
            return [], entries[0].id if entries else ""
        fresh = []
        for entry in entries:
            if entry.id == marker:
                break
            fresh.append(entry)
        items = [
            FeedItem(
                id=entry.id,
                url=entry.url,
                text="\n".join(x for x in (entry.title, entry.summary) if x),
                author=", ".join(entry.authors),
                published_at=entry.date,
                # the entry's own link is the feed's article, not anyone's page
                mentions=mentions(f"{entry.title}\n{entry.summary}", entry.links),
            )
            for entry in reversed(fresh)
        ]
        return items, entries[0].id if entries else marker
