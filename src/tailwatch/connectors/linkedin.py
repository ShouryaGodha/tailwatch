"""LinkedIn: a person's headline, open-to-work flag and positions, through an Apify actor (paid).

LinkedIn has no public API for profiles, so this checker runs an Apify actor synchronously:

  POST https://api.apify.com/v2/acts/<actor>/run-sync-get-dataset-items   (Authorization: Bearer <key>)

and reads the first dataset item. Credential: ``APIFY_API_KEY`` (several keys may be given
separated by commas; the next one is tried only when a key is refused or out of credit, never
after a run that cost money).

The actor is ``ctx.options["actor"]``, else the env ``APIFY_LINKEDIN_ACTOR``, else
``DEFAULT_ACTOR`` (a public profile-detail actor from the Apify store whose input is
``{"username": <profile url>, "includeEmail": false}``). An actor with another input shape is
configured with ``options.input``, a dict whose string values may hold ``{url}``. The reader
understands both common answer shapes: ``basic_info`` + ``experience[title, company,
start_date, end_date, is_current]`` and ``firstName`` / ``headline`` +
``experience[position, companyName, startDate, endDate]``.

Every answer is put in the response cache (``ctx.cache_put("linkedin", url, raw)``) before it
is interpreted, and a cached answer younger than the cadence is reused instead of paying for a
new run, so a pass that is retried, or another part of tailwatch that already pulled the
profile, costs nothing.

Changes: ``role ended`` (a current position no longer current, or gone), ``role added`` (a new
current position), ``title changed`` (both at once at one company), ``headline changed``,
``location changed``, ``about changed``, ``open to work flag changed``. Guard: an answer with no
experience when the last read had positions is a partial scrape, not someone deleting every job.

Options: ``actor``, ``input``, ``usd_per_profile`` (default 0.005), ``timeout`` (seconds, 120).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, ClassVar

from tailwatch.connectors._text import clean, clip, flag_change, text_change
from tailwatch.connectors.base import BaseChecker, Change, CheckContext, Read
from tailwatch.connectors.http import HttpError

API = "https://api.apify.com/v2"
DEFAULT_ACTOR = "apimaestro~linkedin-profile-detail"
KEY_ENV, ACTOR_ENV = "APIFY_API_KEY", "APIFY_LINKEDIN_ACTOR"
USD_PER_PROFILE = 0.005
CACHE = "linkedin"
MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1
    )
}
GONE_MESSAGES = ("no profile found", "profile not found", "profile does not exist")


def profile_url(value: str) -> str:
    match = re.match(
        r"^https?://(?:[a-z]{2,3}\.|www\.)?linkedin\.com/(?:in|pub)/([^/?#\s]+)",
        str(value or "").strip(),
        re.I,
    )
    return f"https://www.linkedin.com/in/{match.group(1).lower()}" if match else ""


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _date(value: Any) -> str:
    """``{year, month}``, ``"Jun 2024"`` or ``"2024-06"`` as ``2024-06`` (or ``2024``); ``""``."""
    if isinstance(value, dict):
        year, month = value.get("year"), value.get("month")
    else:
        found = re.fullmatch(r"(?:([a-z]{3})[a-z]*\.? )?(\d{4})(?:-(\d{1,2}))?", clean(value).lower())
        if not found:
            return ""
        year, month = found.group(2), found.group(1) or found.group(3)
    if not str(year or "").isdigit():
        return ""
    text = clean(month).lower()
    number = int(text) if text.isdigit() else MONTHS.get(text[:3], 0)
    return f"{int(year)}-{number:02d}" if 1 <= number <= 12 else str(int(year))


def _is_present(value: Any) -> bool:
    text = clean(_dict(value).get("text") if isinstance(value, dict) else value).lower()
    return text in ("present", "now", "current")


def positions(profile: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every position, newest start first. An explicit current flag or a "Present" end wins; a
    position with no end at all is current."""
    out = []
    for entry in map(_dict, _list(profile.get("experience"))):
        start = _date(entry.get("start_date") or entry.get("startDate"))
        raw_end = entry.get("end_date") or entry.get("endDate")
        end = _date(raw_end)
        flag = entry.get("is_current", entry.get("isCurrent"))
        current = bool(flag) if flag is not None else (_is_present(raw_end) or not raw_end)
        out.append(
            {
                "title": clean(entry.get("title") or entry.get("position")),
                "company": clean(entry.get("company") or entry.get("companyName")),
                "start": start,
                "end": "" if current else end,
                "current": current,
            }
        )
    out = [p for p in out if p["title"] or p["company"]]
    order = {id(p): i for i, p in enumerate(out)}
    return sorted(out, key=lambda p: (p["start"], -order[id(p)]), reverse=True)


def is_profile(item: Mapping[str, Any]) -> bool:
    profile = _dict(item.get("linkedin_data")) or dict(item)
    return bool(
        _dict(profile.get("basic_info"))
        or profile.get("publicIdentifier")
        or profile.get("firstName")
        or profile.get("fullName")
        or profile.get("experience")
    )


def normalise(item: Mapping[str, Any]) -> dict[str, Any]:
    """The stored content: no counts, pictures or durations, which change on their own."""
    profile = _dict(item.get("linkedin_data")) or dict(item)
    basic = _dict(profile.get("basic_info"))
    location = profile.get("location") or basic.get("location")
    place = (
        location
        if not isinstance(location, dict)
        else (
            location.get("full")
            or location.get("linkedinText")
            or location.get("default")
            or location.get("city")
        )
    )
    jobs = positions(profile)
    current = next((p for p in jobs if p["current"]), None)
    content: dict[str, Any] = {
        "name": clean(
            basic.get("fullname")
            or basic.get("fullName")
            or profile.get("fullName")
            or " ".join(filter(None, [profile.get("firstName"), profile.get("lastName")]))
        ),
        "headline": clean(basic.get("headline") or profile.get("headline")),
        "bio": clean(basic.get("about") or profile.get("about") or profile.get("summary")),
        "location": clean(place),
        "organization": current["company"] if current else "",
        "title": current["title"] if current else "",
        "flags": {},
        "positions": jobs,
    }
    flag = basic.get("open_to_work", profile.get("openToWork"))
    if flag is not None:
        content["flags"]["open_to_work"] = bool(flag)
    return content


def _fill(value: Any, url: str) -> Any:
    """``{url}`` in every string of an input template replaced by the profile URL."""
    if isinstance(value, str):
        return value.replace("{url}", url)
    if isinstance(value, list):
        return [_fill(v, url) for v in value]
    if isinstance(value, dict):
        return {k: _fill(v, url) for k, v in value.items()}
    return value


def _same_job(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    return (
        clean(a.get("company")).lower() == clean(b.get("company")).lower()
        and clean(a.get("title")).lower() == clean(b.get("title")).lower()
    )


def _at(p: Mapping[str, Any]) -> str:
    return (
        f"{p.get('title')} at {p.get('company')}"
        if p.get("title") and p.get("company")
        else str(p.get("title") or p.get("company") or "")
    )


class LinkedInChecker(BaseChecker):
    kind: ClassVar[str] = "linkedin"
    label: ClassVar[str] = "LinkedIn"
    cadence_hours: ClassVar[float] = 168.0
    paid: ClassVar[bool] = True
    requires: ClassVar[tuple[str, ...]] = (KEY_ENV,)

    def _input(self, ctx: CheckContext, url: str) -> dict[str, Any]:
        template = ctx.options.get("input")
        if not isinstance(template, Mapping):
            return {"username": url, "includeEmail": False}
        return _fill(dict(template), url)

    def read(self, url: str, previous: Mapping[str, Any] | None, ctx: CheckContext) -> Read:
        url = profile_url(url)
        if not url:
            # our data error (a company page, a search URL), never "gone"
            return Read.failed("not a LinkedIn profile URL (linkedin.com/in/...)")
        max_age = float(getattr(self, "cadence_hours", 168.0))
        cached = ctx.cache_get(CACHE, url, max_age)
        usd = 0.0
        # A cached answer stands in only when it is a usable profile: a cached partial scrape
        # must not fail every retry until it ages out.
        if (
            isinstance(cached, dict)
            and is_profile(cached)
            and not self.guard(previous or {}, normalise(cached))
        ):
            item: Any = cached
        else:
            item, failure = self._run(ctx, url)
            if failure is not None:
                return failure
            usd = float(ctx.options.get("usd_per_profile", USD_PER_PROFILE))
            ctx.cache_put(CACHE, url, item)  # kept before it is interpreted, whatever it says
        if not isinstance(item, dict):
            return Read(status="error", error="the actor returned no profile", usd=usd)
        if not is_profile(item):
            message = clean(item.get("message") or item.get("error") or "")
            if any(m in message.lower() for m in GONE_MESSAGES):
                return Read(status="gone", error=message, raw=item, usd=usd)
            return Read(
                status="error",
                error=f"unrecognised actor answer: {message or sorted(item)[:6]}",
                raw=item,
                usd=usd,
            )
        return Read(status="ok", content=normalise(item), raw=item, usd=usd)

    def _run(self, ctx: CheckContext, url: str) -> tuple[Any, Read | None]:
        """``(first dataset item or None, failure)`` of one synchronous actor run."""
        keys = [k.strip() for k in str(ctx.env.get(KEY_ENV) or "").split(",") if k.strip()]
        if not keys:
            return None, Read.failed(f"{KEY_ENV} is not set", outage="no credential")
        actor = str(ctx.options.get("actor") or ctx.env.get(ACTOR_ENV) or DEFAULT_ACTOR).replace("/", "~")
        timeout = float(ctx.options.get("timeout") or 120)
        refused: list[tuple[int, str]] = []
        for key in keys:
            try:
                response = ctx.http.post(
                    f"{API}/acts/{actor}/run-sync-get-dataset-items",
                    params={"timeout": int(timeout)},
                    json=self._input(ctx, url),
                    headers={"Authorization": f"Bearer {key}"},
                    timeout=timeout + 15,
                )
            except HttpError as error:
                return None, Read.failed(str(error))
            text = response.text[:300]
            for secret in keys:
                text = text.replace(secret, "[key]")
            if response.status in (401, 402, 403):  # this key cannot pay: try the next one
                refused.append((response.status, text))
                continue
            if response.status == 429:
                return None, Read.failed("Apify answered 429", outage="rate limited")
            if response.status not in (200, 201):
                return None, Read.failed(f"Apify actor run answered HTTP {response.status}: {text}")
            try:
                body = response.json()
            except ValueError:
                return None, Read.failed("Apify answered something that is not JSON")
            items = body if isinstance(body, list) else [body]
            return (items[0] if items else None), None
        status = refused[-1][0]
        outage = "out of credits" if status == 402 or "limit" in refused[-1][1].lower() else "key rejected"
        return None, Read.failed(
            f"every {KEY_ENV} was refused (last: HTTP {status} {refused[-1][1]})".strip(), outage=outage
        )

    def guard(self, before: Mapping[str, Any], after: Mapping[str, Any]) -> str:
        if before.get("positions") and not after.get("positions"):
            return "the profile answer carried no experience when the last read had positions"
        return ""

    def diff(self, before: Mapping[str, Any], after: Mapping[str, Any]) -> list[Change]:
        changes = text_change("headline changed", before.get("headline"), after.get("headline"))
        changes += text_change("location changed", before.get("location"), after.get("location"))
        changes += text_change("about changed", before.get("bio"), after.get("bio"))
        changes += flag_change(
            "open to work flag changed",
            (before.get("flags") or {}).get("open_to_work"),
            (after.get("flags") or {}).get("open_to_work"),
        )
        old, new = list(before.get("positions") or []), list(after.get("positions") or [])
        ended: list[tuple[dict[str, Any], Change]] = []
        for job in (p for p in old if p.get("current")):
            match = next((p for p in new if _same_job(p, job)), None)
            if match is None:
                ended.append((job, Change("role ended", _at(job), "")))
            elif not match.get("current"):
                ended.append(
                    (
                        job,
                        Change(
                            "role ended",
                            _at(job),
                            f"{_at(job)}, ended {match.get('end') or 'recently'}",
                            str(match.get("end") or ""),
                        ),
                    )
                )
        added = [p for p in new if p.get("current") and not any(_same_job(p, o) for o in old)]
        # A role ended and a role added at the same company is one promotion or retitling.
        for job in list(added):
            pair = next(
                (
                    (j, c)
                    for j, c in ended
                    if clean(j.get("company")).lower() == clean(job.get("company")).lower()
                ),
                None,
            )
            if pair is not None:
                ended.remove(pair)
                added.remove(job)
                changes.append(Change("title changed", _at(pair[0]), _at(job), str(job.get("start") or "")))
        changes += [change for _job, change in ended]
        changes += [
            Change(
                "role added",
                "",
                _at(job) + (f", since {job['start']}" if job.get("start") else ""),
                str(job.get("start") or ""),
            )
            for job in added
        ]
        return changes

    def describe(self, content: Mapping[str, Any]) -> str:
        lines = [f"LinkedIn {content.get('name', '')}: {content.get('headline', '')}"]
        if content.get("location"):
            lines.append(f"Location: {content['location']}")
        if (content.get("flags") or {}).get("open_to_work"):
            lines.append("Open to work: yes")
        if content.get("bio"):
            lines.append(f"About: {clip(str(content['bio']), 800)}")
        for job in content.get("positions") or []:
            span = (
                f"{job.get('start') or '?'} to {'present' if job.get('current') else job.get('end') or '?'}"
            )
            lines.append(f"- {_at(job)} ({span})")
        return clip("\n".join(lines), 4000)
