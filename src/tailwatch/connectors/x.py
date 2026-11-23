"""X: a person's profile and newest own posts, through twitterapi.io (paid).

Credential: ``TWITTERAPI_IO_KEY``, sent as ``X-API-Key``; ``TWITTERAPI_IO_URL`` overrides the
base URL. twitterapi.io bills in credits, 100,000 credits to the dollar:

  GET /twitter/user/info?userName=<handle>         one profile: 18 credits
  GET /twitter/user/last_tweets?userName=<handle>  up to 20 posts: 15 credits a post returned,
                                                   15 at least

The posts call is the expensive one, and on most days there is nothing new, so it is made only
when the profile's post count (``statusesCount``) moved since the last read, or the posts were
last fetched ``POSTS_REFRESH_DAYS`` or more ago (a post deleted and another published leave the
count where it was). Otherwise the last read's posts stand, and the read costs 18 credits.

Status mapping: 402 is the outage "out of credits", 401/403 "key rejected", 429 "rate limited";
the provider saying the user does not exist (or is suspended) is ``gone``; anything else is an
error. A protected account is read without posts. The guard rejects a read with no posts when
the account had posts before: a failed read of the timeline, not a deleted one.

Options: ``include_replies`` (default false): replies count as posts (more credits per read).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, ClassVar

from tailwatch import util
from tailwatch.connectors._text import clean, clip, flag_change, iso_date, link_kind, text_change
from tailwatch.connectors.base import BaseChecker, Change, CheckContext, Read
from tailwatch.connectors.http import HttpError, Response

API = "https://api.twitterapi.io"
KEY_ENV, URL_ENV = "TWITTERAPI_IO_KEY", "TWITTERAPI_IO_URL"
POSTS = 20
POSTS_REFRESH_DAYS = 14
PROFILE_CREDITS, POST_CREDITS, MIN_CALL_CREDITS = 18, 15, 15
CREDITS_PER_USD = 100_000
PROFILE_FIELDS = (
    ("name", "name changed"),
    ("bio", "bio changed"),
    ("location", "location changed"),
    ("website", "website changed"),
)
_GONE = re.compile(r"not found|not exist|suspend|no user|deactivat", re.IGNORECASE)


def handle_of(url: str) -> str:
    text = re.sub(r"^[a-z]+://", "", str(url or "").strip(), flags=re.IGNORECASE)
    host, _, path = text.partition("/")
    if host.lower().removeprefix("www.") in ("x.com", "twitter.com"):
        first = re.split(r"[/?#]", path, maxsplit=1)[0].lstrip("@")
    elif host.startswith("@") and not path:
        first = host[1:]
    else:
        return ""
    return first.lower() if re.fullmatch(r"[A-Za-z0-9_]{1,15}", first) else ""


def _expand(text: str, urls: Any) -> str:
    """t.co links replaced by where they point."""
    for link in urls or []:
        if isinstance(link, dict) and link.get("url") and link.get("expanded_url"):
            text = text.replace(str(link["url"]), str(link["expanded_url"]))
    return text


def _entity_urls(user: Mapping[str, Any], part: str) -> list[dict[str, Any]]:
    """The bio's (``description``) or website's (``url``) t.co links, from either place the
    provider puts them (top-level ``entities`` or ``profile_bio.entities``)."""
    out: list[dict[str, Any]] = []
    for entities in (user.get("entities"), (user.get("profile_bio") or {}).get("entities")):
        section = entities.get(part) if isinstance(entities, dict) else None
        out += [u for u in (section or {}).get("urls") or [] if isinstance(u, dict)]
    return out


def _user(body: Any) -> dict[str, Any]:
    body = body if isinstance(body, dict) else {}
    return body["data"] if isinstance(body.get("data"), dict) else body


def _tweets(body: Any) -> list[dict[str, Any]]:
    body = body if isinstance(body, dict) else {}
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    tweets = body.get("tweets") or data.get("tweets") or []
    return [t for t in tweets if isinstance(t, dict) and str(t.get("id") or "").isdigit()]


def _count(value: Any) -> int | None:
    return int(value) if str(value if value is not None else "").isdigit() else None


def _message(body: Any) -> str:
    body = body if isinstance(body, dict) else {}
    return " ".join(str(body[k]) for k in ("msg", "message", "error") if body.get(k))[:300]


def _json(response: Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {"error": response.text[:300]}


def own_posts(body: Any, handle: str) -> list[dict[str, Any]]:
    """Authored posts in a last_tweets answer (reposts left out), newest first."""
    posts = []
    for tweet in _tweets(body):
        if isinstance(tweet.get("retweeted_tweet"), dict):
            continue
        text = _expand(clean(tweet.get("text")), (tweet.get("entities") or {}).get("urls"))
        posts.append(
            {
                "id": str(tweet["id"]),
                "text": text,
                "date": iso_date(tweet.get("createdAt")),
                "url": clean(tweet.get("url")) or f"https://x.com/{handle}/status/{tweet['id']}",
            }
        )
    posts.sort(key=lambda p: int(p["id"]), reverse=True)
    return posts


def page_credits(body: Any) -> int:
    """What one posts page cost: 15 credits a post returned (reposts included), 15 at least."""
    return max(MIN_CALL_CREDITS, POST_CREDITS * len(_tweets(body)))


def _status_failure(status: int, body: Any, what: str, credits: float) -> Read | None:
    """The Read for a refused call, ``None`` for a usable 200."""

    def fail(error: str, outage: str = "") -> Read:
        return Read(
            status="error", error=error, outage=outage, credits=credits, usd=credits / CREDITS_PER_USD
        )

    if status == 402:
        return fail(f"{what}: HTTP 402 {_message(body)}".strip(), "out of credits")
    if status in (401, 403):
        return fail(f"{what}: HTTP {status} {_message(body)}".strip(), "key rejected")
    if status == 429:
        return fail(f"{what}: HTTP 429", "rate limited")
    if status != 200:
        return fail(f"{what}: HTTP {status} {_message(body)}".strip())
    if isinstance(body, dict) and body.get("status") == "error":
        return fail(f"{what}: provider error {_message(body)}".strip())
    return None


class XChecker(BaseChecker):
    kind: ClassVar[str] = "x"
    label: ClassVar[str] = "X"
    cadence_hours: ClassVar[float] = 24.0
    paid: ClassVar[bool] = True
    requires: ClassVar[tuple[str, ...]] = (KEY_ENV,)

    def read(self, url: str, previous: Mapping[str, Any] | None, ctx: CheckContext) -> Read:
        handle, key = handle_of(url), str(ctx.env.get(KEY_ENV) or "").strip()
        if not handle:
            return Read.failed(f"no X handle in {url!r}")
        if not key:
            return Read.failed(f"{KEY_ENV} is not set", outage="no credential")
        base = str(ctx.env.get(URL_ENV) or API).rstrip("/")
        headers = {"X-API-Key": key}
        before = dict(previous or {})
        try:
            response = ctx.http.get(f"{base}/twitter/user/info", params={"userName": handle}, headers=headers)
        except HttpError as error:
            return Read.failed(str(error))
        body = _json(response)
        credits = float(PROFILE_CREDITS if response.status == 200 else 0)  # a refused call is not billed
        user = _user(body)
        problem = _message(body)
        if (
            response.status in (200, 400, 404)
            and ((isinstance(body, dict) and body.get("status") == "error") or response.status != 200)
            and _GONE.search(problem)
        ):
            return Read(status="gone", error=f"X: {problem}", credits=credits, usd=credits / CREDITS_PER_USD)
        if failure := _status_failure(response.status, body, "profile", credits):
            return failure
        reason = f"{user.get('unavailableReason') or ''} {user.get('message') or ''}".strip()
        protected = bool(user.get("protected") or user.get("isProtected") or "protect" in reason.lower())
        if user.get("unavailable") and not protected:
            return Read(
                status="gone",
                error=f"X account unavailable: {reason or 'no reason given'}",
                credits=credits,
                usd=credits / CREDITS_PER_USD,
            )
        if not user.get("id") and not protected:
            return Read(
                status="error",
                error="no profile in the provider answer",
                credits=credits,
                usd=credits / CREDITS_PER_USD,
            )

        content = self._profile(user, handle, protected)
        posts_body: Any = None
        skipped = not protected and self._posts_still_stand(before, content)
        if skipped:
            content["posts"] = list(before.get("posts") or [])
            content["posts_fetched_at"] = before.get("posts_fetched_at", "")
        elif not protected:
            params = {"userName": handle}
            if ctx.options.get("include_replies"):
                params["includeReplies"] = "true"
            try:
                posts_response = ctx.http.get(
                    f"{base}/twitter/user/last_tweets", params=params, headers=headers
                )
            except HttpError as error:
                return Read(
                    status="error", error=f"posts: {error}", credits=credits, usd=credits / CREDITS_PER_USD
                )
            posts_body = _json(posts_response)
            if failure := _status_failure(posts_response.status, posts_body, "posts", credits):
                return failure
            credits += page_credits(posts_body)
            content["posts"] = own_posts(posts_body, handle)[:POSTS]
            content["posts_fetched_at"] = util.iso()
        else:
            content["posts"], content["posts_fetched_at"] = [], ""
        raw = {"profile": body, "posts": posts_body, "posts_skipped": skipped}
        return Read(status="ok", content=content, raw=raw, credits=credits, usd=credits / CREDITS_PER_USD)

    @staticmethod
    def _profile(user: Mapping[str, Any], handle: str, protected: bool) -> dict[str, Any]:
        bio = (user.get("profile_bio") or {}).get("description") or user.get("description")
        site = _entity_urls(user, "url")
        website = clean(site[0].get("expanded_url") or site[0].get("url")) if site else clean(user.get("url"))
        if link_kind(website)[0] == "x" or website.startswith("https://t.co/"):
            website = ""  # the profile linking to itself, or an unexpanded t.co link
        return {
            "handle": clean(user.get("userName")).lower() or handle,
            "user_id": str(user.get("id") or ""),
            "name": clean(user.get("name")),
            "bio": clean(_expand(str(bio or ""), _entity_urls(user, "description"))),
            "location": clean(user.get("location")),
            "website": website,
            "flags": {"protected": protected},
            "statuses": _count(user.get("statusesCount")),
        }

    @staticmethod
    def _posts_still_stand(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
        """Whether the last read's posts can stand in for a new posts call: the same account,
        the same post count, and posts fetched under ``POSTS_REFRESH_DAYS`` ago."""
        if not before or "posts" not in before or (before.get("flags") or {}).get("protected"):
            return False
        if (
            before.get("statuses") is None
            or after.get("statuses") is None
            or before["statuses"] != after["statuses"]
        ):
            return False
        if str(before.get("user_id") or "") != str(after.get("user_id") or ""):
            return False
        fetched = util.parse(str(before.get("posts_fetched_at") or ""))
        return fetched is not None and (util.now() - fetched).total_seconds() < POSTS_REFRESH_DAYS * 86400

    def guard(self, before: Mapping[str, Any], after: Mapping[str, Any]) -> str:
        if (
            before.get("posts")
            and not after.get("posts")
            and not (after.get("flags") or {}).get("protected")
            and str(before.get("user_id") or "") == str(after.get("user_id") or "")
        ):
            return "the timeline came back with no posts when the account had posts before"
        return ""

    def diff(self, before: Mapping[str, Any], after: Mapping[str, Any]) -> list[Change]:
        if before.get("user_id") and after.get("user_id") and before["user_id"] != after["user_id"]:
            return []  # another account behind the same handle: a new baseline, not a change
        changes: list[Change] = []
        for field, label in PROFILE_FIELDS:
            changes += text_change(label, before.get(field), after.get(field))
        was_protected = (before.get("flags") or {}).get("protected")
        changes += flag_change(
            "protected flag changed", was_protected, (after.get("flags") or {}).get("protected")
        )
        if was_protected:
            return changes  # posts that reappear when an account opens again are not new
        # Post ids grow with time, so an unseen post older than the oldest one seen slid into the
        # window after a deletion (or a repost dropped out): never new.
        old = before.get("posts") or []
        seen = {str(p.get("id")) for p in old}
        floor = min((int(p["id"]) for p in old if str(p.get("id", "")).isdigit()), default=0)
        fresh = [
            p
            for p in after.get("posts") or []
            if str(p.get("id")) not in seen and str(p.get("id", "")).isdigit() and int(p["id"]) > floor
        ]
        for post in sorted(fresh, key=lambda p: int(p["id"])):
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
        lines = [f"X @{content.get('handle', '')}: {content.get('name', '')}"]
        for key, title in (("bio", "Bio"), ("location", "Location"), ("website", "Website")):
            if content.get(key):
                lines.append(f"{title}: {content[key]}")
        if (content.get("flags") or {}).get("protected"):
            lines.append("The account is protected; posts are not visible.")
        for post in content.get("posts") or []:
            lines.append(f"- {str(post.get('date'))[:10]} {post.get('text')}")
        return clip("\n".join(lines), 4000)
