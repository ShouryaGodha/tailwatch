"""Hacker News: new stories matching a search, through the Algolia HN API (free, no key).

  GET https://hn.algolia.com/api/v1/search_by_date?query=<q>&tags=story

Options: ``query`` (required), e.g. ``"joins OpenAI"``. The marker is the newest story's
``created_at_i`` (epoch seconds); later polls ask only for stories after it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from tailwatch import util
from tailwatch.connectors._text import clean, iso_date, mentions, strip_html
from tailwatch.connectors.base import CheckContext, FeedItem
from tailwatch.connectors.feeds import FeedError
from tailwatch.connectors.http import HttpError

API = "https://hn.algolia.com/api/v1/search_by_date"
PAGE = 50


class HackerNewsFeed:
    kind: ClassVar[str] = "hackernews"
    label: ClassVar[str] = "Hacker News search"
    paid: ClassVar[bool] = False
    requires: ClassVar[tuple[str, ...]] = ()

    def poll(
        self, marker: str | None, options: Mapping[str, Any], ctx: CheckContext
    ) -> tuple[list[FeedItem], str]:
        query = clean(options.get("query"))
        if not query:
            raise FeedError("the hackernews feed needs options.query")
        params: dict[str, Any] = {"query": query, "tags": "story", "hitsPerPage": PAGE}
        since = int(marker) if marker and str(marker).isdigit() else None
        if since is not None:
            params["numericFilters"] = f"created_at_i>{since}"
        try:
            response = ctx.http.get(API, params=params)
        except HttpError as error:
            raise FeedError(str(error)) from error
        if response.status != 200:
            raise FeedError(
                f"HN Algolia answered HTTP {response.status}",
                outage="rate limited" if response.status == 429 else "",
            )
        try:
            hits = [
                h for h in (response.json().get("hits") or []) if isinstance(h, dict) and h.get("objectID")
            ]
        except (ValueError, AttributeError) as error:
            raise FeedError("HN Algolia answered something that is not JSON") from error
        newest = max((int(h.get("created_at_i") or 0) for h in hits), default=0)
        if since is None:  # the first poll only marks where the search is
            return [], str(newest or int(util.now().timestamp()))
        items = []
        for hit in sorted(hits, key=lambda h: (int(h.get("created_at_i") or 0), str(h["objectID"]))):
            if int(hit.get("created_at_i") or 0) <= since:
                continue
            story_id = str(hit["objectID"])
            link = clean(hit.get("url"))
            text = clean(hit.get("title"))
            if hit.get("story_text"):
                text += "\n" + strip_html(hit["story_text"])
            items.append(
                FeedItem(
                    id=f"hn:{story_id}",
                    url=link or f"https://news.ycombinator.com/item?id={story_id}",
                    text=text,
                    author=clean(hit.get("author")),
                    published_at=iso_date(hit.get("created_at") or hit.get("created_at_i")),
                    mentions=mentions(text, [link] if link else []),
                    raw=hit,
                )
            )
        return items, str(max(newest, since))
