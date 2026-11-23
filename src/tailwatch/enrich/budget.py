"""What one enrichment run may spend, tool by tool.

Prices are nominal USD per live call, checked before the call against the run's
``loop.Budget``; the toolkit charges what the provider reports when it reports a cost (Exa's
``costDollars``, an X page by the posts it held), else the nominal price. A cached answer costs
nothing and does not count towards a tool's cap. Caps stop one tool from eating the budget: a
run that searched eight times has learned what searching will tell it.
"""

from __future__ import annotations

PRICES: dict[str, float] = {
    "web_search": 0.007,
    "web_read": 0.001,          # Exa contents; the plain-fetch fallback is free
    "github_user": 0.0,
    "github_search": 0.0,
    "arxiv_search": 0.0,
    "scholar_search": 0.0,
    "bluesky_profile": 0.0,
    "x_profile": 0.00018,       # twitterapi.io: 18 credits a profile, 100,000 credits a dollar
    "x_posts": 0.003,           # at most a page of 20 posts at 15 credits each
    "linkedin_profile": 0.005,  # one Apify actor run; the connector's usd_per_profile wins
    "list_lookup": 0.0,
}
CAPS: dict[str, int] = {
    "web_search": 8,
    "web_read": 8,
    "github_user": 6,
    "github_search": 2,
    "arxiv_search": 2,
    "scholar_search": 2,
    "bluesky_profile": 3,
    "x_profile": 3,
    "x_posts": 2,
    "linkedin_profile": 2,
    "list_lookup": 6,
}
X_CREDITS_PER_USD = 100_000
X_PROFILE_CREDITS = 18
X_POST_CREDITS = 15


def x_posts_usd(posts: int) -> float:
    """twitterapi.io bills 15 credits a post returned, at least one post's worth a call."""
    return max(1, int(posts)) * X_POST_CREDITS / X_CREDITS_PER_USD
