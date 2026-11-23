"""Homepage: one personal website, read as readable text and compared sentence by sentence.

The page is fetched as a browser would (or through the Jina reader with ``reader: jina``) and
its text is extracted with the standard library's HTML parser: scripts, styles, menus and
footers dropped, one paragraph per line, copyright years and "last updated" dates masked.

``gone`` is a 404 or 410 and nothing else. Everything else unusable is an ``error`` the runner
retries: DNS failures, timeouts, other 4xx (a 403 is usually a bot wall), 5xx, a body that is
not HTML, a challenge page ("Just a moment...", a captcha) and a domain parking page (a lapsed
domain can come back). The guard catches a page that shrank under 200 characters with no real
sentence in it: a broken deploy, not a person deleting their site. A short page that *does*
say something ("I have moved to ...") is a change.

Options (``connectors: {homepage: {options: {...}}}``):
  ``reader``: ``jina`` to read pages through ``https://r.jina.ai/<url>`` (renders script-built
  pages; ``JINA_API_KEY`` is sent when set). Off by default.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, ClassVar
from urllib.parse import urlsplit

from tailwatch.connectors._text import (
    clean,
    clip,
    has_sentence,
    html_page,
    link_kind,
    mask_volatile,
    sentences,
)
from tailwatch.connectors.base import BaseChecker, Change, CheckContext, Read
from tailwatch.connectors.http import HttpError

TEXT_CAP = 40_000  # characters of page text kept
CHANGE_CAP = 4_000  # characters of removed / added sentences in one change
MIN_TEXT = 200
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 tailwatch",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.8",
}
CHALLENGE = re.compile(
    r"(?i)just a moment\.\.\.|attention required!? \| cloudflare|checking your browser"
    r"|verify you are (?:a )?human|captcha|enable javascript and cookies to continue"
    r"|ddos protection by"
)
PARKED = re.compile(
    r"(?i)domain (?:name )?(?:is|may be) for sale|buy this domain|this domain (?:has expired|is parked)"
    r"|parked (?:free|domain)|domain parking|this domain is available"
)
PARK_HOSTS = re.compile(
    r"(?:^|\.)(?:(?:sedoparking|sedo|hugedomains|dan|afternic|bodis|above|domainmarket)\.com"
    r"|parkingcrew\.net)$"
)
SHELL = re.compile(
    r"(?i)\b(?:enable|requires?|turn on) javascript|application error|something went wrong"
    r"|index of /"
)


def _markdown_text(markdown: str) -> str:
    """The Jina reader's markdown as plain lines: link targets, images and markup dropped."""
    body = markdown.split("Markdown Content:", 1)[-1]
    body = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", body)
    body = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", body)
    body = re.sub(r"(?m)^\s*(?:#{1,6}|[-*+>]|\d+\.)\s+", "", body)
    body = re.sub(r"[*_`]{1,3}", "", body)
    return "\n".join(line for line in (mask_volatile(clean(x)) for x in body.split("\n")) if line)


def _tail(longer: list[str], cut: list[str]) -> set[str]:
    """Sentences of ``longer`` after the last one the cut text ends on: what a cap dropped."""
    if not cut or cut[-1] not in longer:
        return set()
    end = len(longer) - 1 - longer[::-1].index(cut[-1])
    return set(longer[end + 1 :])


class HomepageChecker(BaseChecker):
    kind: ClassVar[str] = "homepage"
    label: ClassVar[str] = "Homepage"
    cadence_hours: ClassVar[float] = 24.0
    paid: ClassVar[bool] = False
    requires: ClassVar[tuple[str, ...]] = ()

    def read(self, url: str, previous: Mapping[str, Any] | None, ctx: CheckContext) -> Read:
        jina = str(ctx.options.get("reader") or "").lower() == "jina"
        try:
            if jina:
                headers = {"Accept": "text/plain"}
                if ctx.env.get("JINA_API_KEY"):
                    headers["Authorization"] = f"Bearer {ctx.env['JINA_API_KEY']}"
                response = ctx.http.get(f"https://r.jina.ai/{url}", headers=headers)
            else:
                response = ctx.http.get(url, headers=BROWSER_HEADERS)
        except HttpError as error:
            return Read.failed(str(error))
        if response.status in (404, 410):
            return Read(status="gone", error=f"HTTP {response.status}")
        if not 200 <= response.status < 300:
            return Read.failed(f"HTTP {response.status}")
        final = response.url or url
        if jina:
            title_match = re.search(r"(?m)^Title:\s*(.+)$", response.text)
            title, text = clean(title_match.group(1) if title_match else ""), _markdown_text(response.text)
            links = re.findall(r"\]\((https?://[^)\s]+)\)", response.text)
        else:
            content_type = str(
                {k.lower(): v for k, v in (response.headers or {}).items()}.get("content-type", "")
            )
            if (
                content_type
                and not re.search(r"(?i)html|xml|text/", content_type)
                and not response.text.lstrip().startswith("<")
            ):
                return Read.failed(f"not an HTML page ({content_type})")
            page = html_page(response.text, final)
            title, text, links = page.title, page.text, page.links
        head = f"{title}\n{text[:3000]}"
        short = len(text) < 3000
        host = (urlsplit(final).hostname or "").lower()
        if PARK_HOSTS.search(host) or (short and PARKED.search(head)):
            return Read.failed(f"a domain parking page at {final}")
        if short and CHALLENGE.search(head):
            return Read.failed("a bot challenge page, not the site")
        truncated = len(text) > TEXT_CAP
        if truncated:  # cut at a line end so no sentence is stored half
            cut = text.rfind("\n", 0, TEXT_CAP)
            text = text[: cut if cut > TEXT_CAP // 2 else TEXT_CAP]
        profiles = sorted(
            {
                f"{kind}:{value}"
                for kind, value in (link_kind(u) for u in links)
                if kind and kind != "homepage"
            }
        )
        content: dict[str, Any] = {"url": url, "title": title, "text": text, "links": profiles}
        if truncated:
            content["truncated"] = True
        prev = dict(previous or {})
        if prev and len(text) < MIN_TEXT:
            # A short page standing in for a full one (maintenance, "back soon") carries the full
            # page's sentences, so the full page coming back does not read as all new.
            content["seen_before"] = (
                list(prev.get("seen_before") or [])
                if len(prev.get("text") or "") < MIN_TEXT
                else list(dict.fromkeys(sentences(prev.get("text") or "")))
            )
        return Read(status="ok", content=content, raw={"status": response.status, "url": final})

    def guard(self, before: Mapping[str, Any], after: Mapping[str, Any]) -> str:
        text, old = str(after.get("text") or ""), str(before.get("text") or "")
        if len(text) < MIN_TEXT <= len(old) and (not has_sentence(text) or SHELL.search(text)):
            return f"page text fell from {len(old)} to {len(text)} characters with no real sentence left"
        return ""

    def diff(self, before: Mapping[str, Any], after: Mapping[str, Any]) -> list[Change]:
        changes: list[Change] = []
        if clean(before.get("title")) != clean(after.get("title")):
            changes.append(Change("title changed", clean(before.get("title")), clean(after.get("title"))))
        if before.get("text") != after.get("text"):
            old, new = sentences(before.get("text") or ""), sentences(after.get("text") or "")
            known = set(old) | set(before.get("seen_before") or [])
            removed = [s for s in dict.fromkeys(old) if s not in set(new)]
            added = [s for s in dict.fromkeys(new) if s not in known]
            # A text cut at the cap loses or gains its tail when something above it moves.
            if after.get("truncated"):
                removed = [s for s in removed if s not in _tail(old, new)]
            if before.get("truncated"):
                added = [s for s in added if s not in _tail(new, old)]
            if removed or added:
                changes.append(
                    Change(
                        "text changed",
                        clip("\n".join(removed), CHANGE_CAP),
                        clip("\n".join(added), CHANGE_CAP),
                    )
                )
        was, now = set(before.get("links") or []), set(after.get("links") or [])
        if was != now:
            changes.append(
                Change("profile links changed", "\n".join(sorted(was - now)), "\n".join(sorted(now - was)))
            )
        return changes

    def describe(self, content: Mapping[str, Any]) -> str:
        head = f"Homepage {content.get('url', '')}" + (
            f" — {content['title']}" if content.get("title") else ""
        )
        links = f"\nProfile links: {', '.join(content['links'])}" if content.get("links") else ""
        return clip(f"{head}{links}\n\n{content.get('text', '')}", 4000)
