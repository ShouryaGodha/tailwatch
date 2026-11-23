"""Helpers the connectors share: whitespace and HTML cleaning, readable page text, sentences,
RSS/Atom parsing, dates, handle and link extraction, and the two small diff idioms (a text
field that changed, list items not seen before).

Everything here is a pure function of its input, standard library only.
"""

from __future__ import annotations

import html
import re
import unicodedata
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

from tailwatch.connectors.base import Change

# --------------------------------------------------------------------------- plain text

_ZERO_WIDTH = re.compile("[​-‏⁠﻿­]")


def clean(value: Any) -> str:
    """One line of text: HTML entities decoded, zero-width characters dropped, whitespace
    collapsed. ``None`` is ``""``."""
    if value is None:
        return ""
    text = _ZERO_WIDTH.sub("", html.unescape(str(value)))
    return " ".join(text.split())


def clip(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` characters, marked with an ellipsis when cut."""
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def round2(value: Any) -> int:
    """A count to two significant figures, so a star or two more does not change the content."""
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        return 0
    if n < 100:
        return n
    scale = 10 ** (len(str(n)) - 2)
    return round(n / scale) * scale


# --------------------------------------------------------------------------- HTML

_BLOCK = frozenset({
    "address", "article", "aside", "blockquote", "body", "br", "caption", "dd", "details", "div",
    "dl", "dt", "figcaption", "figure", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr",
    "li", "main", "ol", "p", "pre", "section", "summary", "table", "tr", "ul"})
_INLINE_GAP = frozenset({"a", "span", "time", "label", "small", "cite", "abbr", "button", "td", "th"})
# Never readable content: code, styling, menus, and the footer (copyright lines, counters).
_SKIP = frozenset({"script", "style", "noscript", "template", "svg", "nav", "footer", "head", "iframe"})
_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
                   "param", "source", "track", "wbr"})


class _Readable(HTMLParser):
    """Collects visible text (blocks become line breaks), the <title> and every <a href>."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[str] = []
        self.title_parts: list[str] = []
        self._skip: list[str] = []  # the stack of open skipped elements
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {k: v or "" for k, v in attrs}
        if tag == "title":
            self._in_title = True
        if tag == "a" and values.get("href"):
            self.links.append(values["href"])
        if self._skip:
            if tag == self._skip[-1] and tag not in _VOID:
                self._skip.append(tag)
            return
        if tag in _SKIP or values.get("role") in ("navigation", "contentinfo") or "hidden" in values:
            if tag not in _VOID:
                self._skip.append(tag)
            return
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if self._skip:
            if tag == self._skip[-1]:
                self._skip.pop()
            return
        if tag in _BLOCK:
            self.parts.append("\n")
        elif tag in _INLINE_GAP:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        if not self._skip:
            self.parts.append(re.sub(r"\s+", " ", data))  # source newlines are not breaks


@dataclass
class Page:
    title: str
    text: str  # one paragraph per line
    links: list[str] = field(default_factory=list)  # absolute http(s) URLs, in page order


def html_page(markup: str, base_url: str = "") -> Page:
    """Readable text of an HTML document: scripts, styles, menus and footers dropped, one block
    per line, whitespace collapsed, volatile tokens (copyright years, "updated on" dates, visit
    counters, "3 days ago") masked so they never read as a change."""
    parser = _Readable()
    try:
        parser.feed(markup or "")
        parser.close()
    except Exception:  # HTMLParser is tolerant; this is a last-resort guard
        pass
    lines = [mask_volatile(clean(line)) for line in "".join(parser.parts).split("\n")]
    text = "\n".join(line for line in lines if line)
    links: list[str] = []
    for href in parser.links:
        absolute = urljoin(base_url, href.strip()) if base_url else href.strip()
        if urlsplit(absolute).scheme in ("http", "https") and absolute not in links:
            links.append(absolute)
    return Page(title=clean("".join(parser.title_parts)), text=text, links=links)


def strip_html(fragment: Any) -> str:
    """A fragment of HTML (a feed summary, a post body) as one line of text."""
    return clean(html_page(str(fragment or "")).text.replace("\n", " "))


_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_DATE = (r"(?:\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}"
         rf"|{_MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}|\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH},?\s+\d{{4}}"
         rf"|{_MONTH},?\s+\d{{4}}|\d{{4}})")
_MASKS = [(re.compile(pattern), replacement) for pattern, replacement in [
    (r"(?i)((?:©|\(c\)|copyright)\s*©?\s*)\d{4}(?:\s*[-–—]\s*\d{4})?", r"\1<YEAR>"),
    (r"(?i)((?:last\s+)?(?:updated|modified|revised|built|generated)(?:\s+on|\s+at)?\s*[:\-–]?\s*)" + _DATE,
     r"\1<DATE>"),
    (r"(?i)((?:visitors?|visits|hits|page\s*views?)\s*[:#]?\s*)\d[\d,.]*k?\b", r"\1<N>"),
    (r"(?i)\b\d+\s+(?:second|minute|hour|day|week|month|year)s?\s+ago\b", "<AGO>"),
]]


def mask_volatile(line: str) -> str:
    line = unicodedata.normalize("NFKC", line)
    for pattern, replacement in _MASKS:
        line = pattern.sub(replacement, line)
    return line


_ABBREVIATIONS = re.compile(r"(?i)\b(?:Ph\.\s?D|Dr|Prof|Mr|Ms|Mrs|Jr|Sr|St|Inc|Corp|Univ|Dept|e\.g|i\.e|vs|etc|U\.S)\.")


def sentences(text: str) -> list[str]:
    """Sentences of a text, one paragraph per line, abbreviations kept whole."""
    out: list[str] = []
    for line in (text or "").split("\n"):
        guarded = _ABBREVIATIONS.sub(lambda m: m.group(0).replace(".", "\0"), line)
        out += [s.replace("\0", ".").strip() for s in re.split(r"(?<=[.!?])\s+", guarded)]
    return [s for s in out if re.search(r"\w", s)]


def has_sentence(text: str, words: int = 4) -> bool:
    """Whether a text holds one real sentence: ``words`` or more words of two letters or more."""
    return any(len(re.findall(r"[^\W\d_]{2,}", s)) >= words for s in sentences(text))


# --------------------------------------------------------------------------- dates


def iso_date(value: Any) -> str:
    """A date in any of the shapes sources use (ISO 8601, RFC 822, X's ``Fri Oct 02 06:35:51
    +0000 2026``, epoch seconds) as ``YYYY-MM-DDTHH:MM:SSZ`` in UTC; a bare date or year stays
    as it is; ``""`` when unreadable. One format, so dates compare as strings."""
    if value is None or value == "":
        return ""
    if isinstance(value, int | float) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    text = str(value).strip()
    if re.fullmatch(r"\d{4}(-\d{2}(-\d{2})?)?", text):
        return text
    moment: datetime | None = None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for parse in (parsedate_to_datetime, lambda t: datetime.strptime(t, "%a %b %d %H:%M:%S %z %Y")):
            try:
                moment = parse(text)
                break
            except (TypeError, ValueError, IndexError):
                continue
    if moment is None:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- XML and feeds


def local(tag: str) -> str:
    """An element's tag without its namespace: ``{http://www.w3.org/2005/Atom}entry`` → ``entry``."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def parse_xml(text: str) -> ET.Element:
    """The root element; ``ValueError`` when the text is not XML. Documents that declare
    entities are refused (the expansion attacks a feed parser should never run)."""
    if re.search(r"<!ENTITY", text or "", re.IGNORECASE):
        raise ValueError("XML with entity declarations is refused")
    try:
        return ET.fromstring((text or "").strip().encode("utf-8"))
    except ET.ParseError as error:
        raise ValueError(f"not XML: {error}") from error


def child(element: ET.Element, name: str) -> ET.Element | None:
    for item in element:
        if local(item.tag) == name:
            return item
    return None


def children(element: ET.Element, name: str) -> list[ET.Element]:
    return [item for item in element if local(item.tag) == name]


def child_text(element: ET.Element, *names: str) -> str:
    """The text of the first named child that has any, cleaned."""
    for name in names:
        found = child(element, name)
        if found is not None and (found.text or "").strip():
            return clean(found.text)
    return ""


@dataclass
class FeedEntry:
    id: str
    title: str
    url: str
    date: str
    summary: str
    authors: list[str] = field(default_factory=list)
    element: ET.Element | None = None  # the entry itself, for source-specific fields


@dataclass
class ParsedFeed:
    title: str
    url: str
    entries: list[FeedEntry]


def _atom_link(entry: ET.Element) -> str:
    links = children(entry, "link")
    for link in links:
        if link.get("rel", "alternate") == "alternate" and link.get("href"):
            return link.get("href", "")
    return links[0].get("href", "") if links else ""


def parse_feed(text: str) -> ParsedFeed:
    """An RSS 2.0, RSS 1.0 (RDF) or Atom document. ``ValueError`` when it is none of them.
    Entries come back newest first (by date, then in document order)."""
    root = parse_xml(text)
    kind = local(root.tag)
    entries: list[FeedEntry] = []
    if kind == "feed":  # Atom
        title, url = child_text(root, "title"), _atom_link(root)
        for entry in children(root, "entry"):
            link = _atom_link(entry)
            entries.append(FeedEntry(
                id=child_text(entry, "id") or link, title=child_text(entry, "title"), url=link,
                date=iso_date(child_text(entry, "published", "updated")),
                summary=strip_html(child_text(entry, "summary", "content")),
                authors=[child_text(a, "name") for a in children(entry, "author") if child_text(a, "name")],
                element=entry))
    elif kind in ("rss", "RDF"):
        channel = child(root, "channel")
        if channel is None:
            raise ValueError("an RSS document without a channel")
        title, url = child_text(channel, "title"), child_text(channel, "link")
        items = children(channel, "item") or children(root, "item")  # RSS 1.0 puts items at the root
        for item in items:
            link = child_text(item, "link")
            entries.append(FeedEntry(
                id=child_text(item, "guid") or link or child_text(item, "title"),
                title=child_text(item, "title"), url=link,
                date=iso_date(child_text(item, "pubDate", "date", "published", "updated")),
                summary=strip_html(child_text(item, "description", "encoded")),
                authors=[a for a in (child_text(item, "author", "creator"),) if a],
                element=item))
    else:
        raise ValueError(f"not an RSS or Atom feed (root element <{kind}>)")
    order = {id(e): i for i, e in enumerate(entries)}
    entries.sort(key=lambda e: (e.date, -order[id(e)]), reverse=True)
    return ParsedFeed(title=title, url=url, entries=[e for e in entries if e.id])


# --------------------------------------------------------------------------- handles and links

_X_RESERVED = {"i", "intent", "home", "search", "share", "hashtag", "explore", "settings", "messages",
               "notifications", "login", "signup", "compose", "status"}
_GITHUB_RESERVED = {"orgs", "settings", "about", "features", "topics", "explore", "marketplace",
                    "sponsors", "login", "join", "search", "notifications", "pulls", "issues"}
_URL = re.compile(r"https?://[^\s<>\"'()\[\]]+", re.IGNORECASE)
_AT = re.compile(r"(?<![\w@./])@([A-Za-z0-9_]{1,15})(?![\w@])")
_SKIP_HOSTS = ("t.co", "bit.ly", "pic.twitter.com", "news.ycombinator.com", "youtu.be", "youtube.com")


def link_kind(url: str) -> tuple[str, str]:
    """``(kind, handle_or_url)`` for a profile link: ``("x", "ada")``, ``("github", "ada")``,
    ``("linkedin", "https://www.linkedin.com/in/ada")``, ``("bluesky", "ada.bsky.social")``,
    ``("homepage", url)``; ``("", "")`` for a link that names nobody (a tweet, a repo issue)."""
    parts = urlsplit(url.strip().rstrip(".,;:!?"))
    host = (parts.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    segments = [s for s in parts.path.split("/") if s]
    if host in ("x.com", "twitter.com"):
        if len(segments) == 1 and re.fullmatch(r"[A-Za-z0-9_]{1,15}", segments[0]) \
                and segments[0].lower() not in _X_RESERVED:
            return "x", segments[0].lower()
        return "", ""
    if host == "github.com":
        if 1 <= len(segments) <= 2 and segments[0].lower() not in _GITHUB_RESERVED \
                and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", segments[0]):
            return "github", segments[0].lower()
        return "", ""
    if host.endswith("linkedin.com"):
        if len(segments) >= 2 and segments[0] == "in":
            return "linkedin", f"https://www.linkedin.com/in/{segments[1].lower()}"
        return "", ""
    if host == "bsky.app":
        if len(segments) == 2 and segments[0] == "profile":
            return "bluesky", segments[1].lower()
        return "", ""
    if not host or host.endswith(_SKIP_HOSTS) or host in _SKIP_HOSTS:
        return "", ""
    return "homepage", f"{parts.scheme}://{host}{parts.path.rstrip('/')}"


def urls_in(text: str) -> list[str]:
    return list(dict.fromkeys(u.rstrip(".,;:!?") for u in _URL.findall(text or "")))


def at_handles(text: str) -> list[str]:
    """``@handle`` mentions in a text (X style), lowercased, in order, without repeats."""
    return list(dict.fromkeys(m.lower() for m in _AT.findall(text or "")))


def mentions(text: str = "", urls: Iterable[str] = (), *, x_handles: Iterable[str] = (),
             bluesky_handles: Iterable[str] = (), exclude: Mapping[str, Iterable[str]] | None = None
             ) -> dict[str, list[str]]:
    """``kind -> handles/urls`` an item points at: profile links in the text and in ``urls``,
    plus handles a source already parsed. ``exclude`` drops the announcer's own handles.
    Kinds with nothing are left out; values keep first-seen order."""
    found: dict[str, list[str]] = {}

    def add(kind: str, value: str) -> None:
        if kind and value and value not in found.setdefault(kind, []):
            found[kind].append(value)

    for handle in x_handles:
        add("x", handle.lower().lstrip("@"))
    for handle in bluesky_handles:
        add("bluesky", handle.lower().lstrip("@"))
    for url in [*urls, *urls_in(text)]:
        add(*link_kind(url))
    for name, values in (exclude or {}).items():
        drop = {v.lower() for v in values}
        if name in found:
            found[name] = [v for v in found[name] if v.lower() not in drop]
    return {k: v for k, v in found.items() if v}


# --------------------------------------------------------------------------- diff idioms


def text_change(label: str, before: Any, after: Any) -> list[Change]:
    """One change when two texts differ once whitespace is collapsed, none when they do not."""
    old, new = clean(before), clean(after)
    return [Change(label, old, new)] if old != new else []


def flag_change(label: str, before: Any, after: Any) -> list[Change]:
    """A boolean flag that flipped. A flag one side does not carry (``None``) is not a change:
    an answer that leaves the field out says nothing about it."""
    if before is None or after is None or bool(before) == bool(after):
        return []
    return [Change(label, "yes" if before else "no", "yes" if after else "no")]


def unseen(before: Sequence[Mapping[str, Any]], after: Sequence[Mapping[str, Any]], *, cap: int,
           key: str = "id", date: str = "date") -> list[Mapping[str, Any]]:
    """Items of ``after`` whose ``key`` ``before`` did not have, oldest first.

    When ``before`` was a full window (``cap`` items), an unseen item older than the oldest one
    it held slid into the window because something newer was deleted; it is not new."""
    seen = {str(item.get(key)) for item in before}
    floor = ""
    if len(before) >= cap:
        dates = [str(item.get(date) or "") for item in before]
        floor = min(dates) if all(dates) else ""
    fresh = [item for item in after if str(item.get(key)) not in seen
             and (not floor or str(item.get(date) or "") >= floor)]
    return sorted(fresh, key=lambda item: str(item.get(date) or ""))
