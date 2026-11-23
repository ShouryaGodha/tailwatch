"""Feeds: streams of posts about anyone (a news search, a blog, an announcer account).

Each feed's ``poll(marker, options, ctx)`` returns the items after ``marker``, oldest first, and
the new marker. The first poll (``marker`` None) returns no items, only the marker of the newest
one, so a new feed never replays its history. A failed poll raises ``FeedError``; the marker the
caller holds stays where it was."""

from __future__ import annotations

from tailwatch.connectors.http import HttpError


class FeedError(HttpError):
    """A poll that could not read its feed. An ``HttpError``, so the watch loop records it
    against the feed and moves on. ``outage`` names a provider-wide refusal ("out of credits",
    "key rejected", "rate limited", "no credential") the way ``Read.outage`` does."""

    def __init__(self, message: str, *, outage: str = "", status: int = 0) -> None:
        super().__init__(message, status=status)
        self.outage = outage
