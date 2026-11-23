"""RSS: a person's own blog feed (RSS 2.0, RSS 1.0 or Atom), read with the standard library.

Changes: ``new post`` for each entry id not seen before. A feed that suddenly has no entries
when it had some is a failed read (the guard), not a deleted blog.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from tailwatch.connectors._text import clean, clip, parse_feed, text_change, unseen
from tailwatch.connectors.base import BaseChecker, Change, CheckContext, Read
from tailwatch.connectors.http import HttpError

POSTS = 20
SUMMARY = 500  # characters of an entry's summary kept


class RssChecker(BaseChecker):
    kind: ClassVar[str] = "rss"
    label: ClassVar[str] = "Blog feed"
    cadence_hours: ClassVar[float] = 24.0
    paid: ClassVar[bool] = False
    requires: ClassVar[tuple[str, ...]] = ()

    def read(self, url: str, previous: Mapping[str, Any] | None, ctx: CheckContext) -> Read:
        try:
            response = ctx.http.get(url, headers={"Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.5"})
        except HttpError as error:
            return Read.failed(str(error))
        if response.status in (404, 410):
            return Read(status="gone", error=f"HTTP {response.status}")
        if response.status != 200:
            return Read.failed(f"HTTP {response.status}")
        try:
            feed = parse_feed(response.text)
        except ValueError as error:
            return Read.failed(str(error))
        posts = [{"id": e.id, "title": e.title, "text": clip(e.summary, SUMMARY), "date": e.date, "url": e.url}
                 for e in feed.entries[:POSTS]]
        content = {"name": feed.title, "website": feed.url, "posts": posts}
        return Read(status="ok", content=content, raw=response.text[:200_000])

    def guard(self, before: Mapping[str, Any], after: Mapping[str, Any]) -> str:
        if before.get("posts") and not after.get("posts"):
            return "the feed came back with no entries when it had some"
        return ""

    def diff(self, before: Mapping[str, Any], after: Mapping[str, Any]) -> list[Change]:
        changes = text_change("feed title changed", before.get("name"), after.get("name"))
        for post in unseen(before.get("posts") or [], after.get("posts") or [], cap=POSTS):
            body = clean(post.get("title")) or clean(post.get("text"))
            summary = clean(post.get("text")) if post.get("title") else ""
            changes.append(Change("new post", "", "\n".join(x for x in (body, summary, post.get("url")) if x),
                                  str(post.get("date") or "")[:10]))
        return changes

    def describe(self, content: Mapping[str, Any]) -> str:
        lines = [f"Blog feed {content.get('name', '')} {content.get('website', '')}".strip()]
        for post in content.get("posts") or []:
            lines.append(f"- {str(post.get('date'))[:10]} {post.get('title') or post.get('text')} {post.get('url')}")
        return clip("\n".join(lines), 4000)
