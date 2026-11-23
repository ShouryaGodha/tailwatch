"""An X announcer account: its new posts, with the accounts and links each one mentions,
through twitterapi.io (paid: ``TWITTERAPI_IO_KEY``).

  GET https://api.twitterapi.io/twitter/user/last_tweets?userName=<handle>   (X-API-Key)

One call a poll: 15 credits a post returned, 15 at least (about $0.003 for a page of 20).
Options: ``handle`` (required). ``@mentions`` (excluding the announcer itself) go to
``mentions["x"]`` with the display names X gives in ``names``; linked profiles (GitHub,
LinkedIn, homepages) go to their kinds. Reposts are left out. The marker is the newest post id.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from tailwatch.connectors._text import at_handles, clean, iso_date, mentions
from tailwatch.connectors.base import CheckContext, FeedItem
from tailwatch.connectors.feeds import FeedError
from tailwatch.connectors.http import HttpError
from tailwatch.connectors.x import API, KEY_ENV, URL_ENV, _expand, _json, _message, _tweets, handle_of


class XAccountFeed:
    kind: ClassVar[str] = "x_account"
    label: ClassVar[str] = "X announcer account"
    paid: ClassVar[bool] = True
    requires: ClassVar[tuple[str, ...]] = (KEY_ENV,)

    def poll(
        self, marker: str | None, options: Mapping[str, Any], ctx: CheckContext
    ) -> tuple[list[FeedItem], str]:
        raw_handle = str(options.get("handle") or "")
        handle = handle_of(raw_handle) or clean(raw_handle).lstrip("@").lower()
        if not handle:
            raise FeedError("the x_account feed needs options.handle")
        key = str(ctx.env.get(KEY_ENV) or "").strip()
        if not key:
            raise FeedError(f"{KEY_ENV} is not set", outage="no credential")
        base = str(ctx.env.get(URL_ENV) or API).rstrip("/")
        try:
            response = ctx.http.get(
                f"{base}/twitter/user/last_tweets", params={"userName": handle}, headers={"X-API-Key": key}
            )
        except HttpError as error:
            raise FeedError(str(error)) from error
        body = _json(response)
        outage = {402: "out of credits", 401: "key rejected", 403: "key rejected", 429: "rate limited"}.get(
            response.status, ""
        )
        if response.status != 200 or (isinstance(body, dict) and body.get("status") == "error"):
            raise FeedError(
                f"twitterapi.io answered HTTP {response.status} {_message(body)}".strip(), outage=outage
            )
        tweets = [t for t in _tweets(body) if not isinstance(t.get("retweeted_tweet"), dict)]
        newest = max((int(t["id"]) for t in tweets), default=0)
        if marker is None:
            return [], str(newest) if newest else ""
        since = int(marker) if str(marker).isdigit() else 0
        items = []
        for tweet in sorted(tweets, key=lambda t: int(t["id"])):
            if int(tweet["id"]) <= since:
                continue
            entities = tweet.get("entities") or {}
            text = _expand(clean(tweet.get("text")), entities.get("urls"))
            tagged = [m for m in entities.get("user_mentions") or [] if isinstance(m, dict)]
            handles = [str(m.get("screen_name") or "").lower() for m in tagged if m.get("screen_name")]
            handles += at_handles(text)
            names = list(
                dict.fromkeys(
                    clean(m.get("name"))
                    for m in tagged
                    if m.get("name") and str(m.get("screen_name") or "").lower() != handle
                )
            )
            urls = [
                str(u.get("expanded_url"))
                for u in entities.get("urls") or []
                if isinstance(u, dict) and u.get("expanded_url")
            ]
            items.append(
                FeedItem(
                    id=f"x:{tweet['id']}",
                    url=clean(tweet.get("url")) or f"https://x.com/{handle}/status/{tweet['id']}",
                    text=text,
                    author=handle,
                    published_at=iso_date(tweet.get("createdAt")),
                    mentions=mentions(text, urls, x_handles=handles, exclude={"x": [handle]}),
                    names=names,
                    raw=tweet,
                )
            )
        return items, str(max(newest, since)) if (newest or since) else (marker or "")
