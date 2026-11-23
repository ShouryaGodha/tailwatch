"""arXiv: an author's newest papers.

A source is either an author query (``arxiv:au:Lovelace, Ada``), read through the arXiv API
(``export.arxiv.org/api/query``, Atom), or an arXiv author page (``https://arxiv.org/a/<id>``),
read from its Atom feed ``https://arxiv.org/a/<id>.atom2``. When the author feed cannot be read
(anything but a 404), the checker falls back to an API query built from the author id
(``lovelace_a_1`` → ``au:lovelace_a``).

Changes: ``new paper`` for each paper id not seen before, and ``affiliation changed`` when the
affiliation arXiv lists for the author (sparse; carried over from the last read when a read
has none) names a different place.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, ClassVar

from tailwatch.connectors._text import (
    child,
    child_text,
    children,
    clean,
    clip,
    iso_date,
    local,
    parse_xml,
    unseen,
)
from tailwatch.connectors.base import BaseChecker, Change, CheckContext, Read
from tailwatch.connectors.http import HttpError

API = "http://export.arxiv.org/api/query"
PAPERS = 20


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z]+", text.lower()) if len(t) > 1}


def _paper_id(raw_id: str) -> str:
    """``http://arxiv.org/abs/2401.01234v2`` → ``2401.01234`` (the version is not a new paper)."""
    tail = raw_id.rsplit("/abs/", 1)[-1]
    return re.sub(r"v\d+$", "", tail.strip())


def parse_papers(xml_text: str) -> list[dict[str, Any]]:
    """Papers from an arXiv Atom document, newest first. Each keeps every author's affiliation
    under ``_affiliations`` for the reader to pick the tracked author's (dropped from content)."""
    root = parse_xml(xml_text)
    if local(root.tag) != "feed":
        raise ValueError(f"not an Atom feed (root <{local(root.tag)}>)")
    papers = []
    for entry in children(root, "entry"):
        raw_id = child_text(entry, "id")
        if not raw_id or "/abs/" not in raw_id:  # the API puts an error entry in an empty answer
            continue
        authors, affiliations = [], {}
        for author in children(entry, "author"):
            name = child_text(author, "name")
            if name:
                authors.append(name)
                if affiliation := child_text(author, "affiliation"):
                    affiliations[name] = affiliation
        link = next(
            (
                a.get("href", "")
                for a in children(entry, "link")
                if a.get("rel") == "alternate" or a.get("type") == "text/html"
            ),
            "",
        )
        category = child(entry, "primary_category")
        venue = child_text(entry, "journal_ref") or (category.get("term", "") if category is not None else "")
        papers.append(
            {
                "id": _paper_id(raw_id),
                "title": clean(child_text(entry, "title")),
                "date": iso_date(child_text(entry, "published")),
                "url": link or raw_id,
                "venue": clean(venue),
                "authors": authors,
                "_affiliations": affiliations,
            }
        )
    papers.sort(key=lambda p: (p["date"], p["id"]), reverse=True)
    return papers


def _affiliation(papers: list[dict[str, Any]], who: str) -> tuple[str, str]:
    """``(author name, affiliation)`` of the tracked author on the newest paper that lists one;
    the author is the one whose surname is in the query."""
    wanted = _tokens(who)
    for paper in papers:
        for name in paper["authors"]:
            parts = name.split()
            if parts and parts[-1].lower() in wanted and paper["_affiliations"].get(name):
                return name, paper["_affiliations"][name]
    return "", ""


def _author_name(papers: list[dict[str, Any]], who: str) -> str:
    """The tracked author's name as their newest paper spells it."""
    wanted = _tokens(who)
    for paper in papers:
        for name in paper["authors"]:
            parts = name.split()
            if parts and parts[-1].lower() in wanted:
                return name
    return ""


class ArxivChecker(BaseChecker):
    kind: ClassVar[str] = "arxiv"
    label: ClassVar[str] = "arXiv"
    cadence_hours: ClassVar[float] = 24.0
    paid: ClassVar[bool] = False
    requires: ClassVar[tuple[str, ...]] = ()

    def _query(self, ctx: CheckContext, query: str) -> tuple[str, str]:
        """``(xml, error)`` for an API author query."""
        params = {
            "search_query": f'au:"{query}"' if " " in query or "," in query else f"au:{query}",
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "max_results": PAPERS,
        }
        response = ctx.http.get(API, params=params)
        if response.status != 200:
            return "", f"arXiv API answered HTTP {response.status}"
        return response.text, ""

    def read(self, url: str, previous: Mapping[str, Any] | None, ctx: CheckContext) -> Read:
        try:
            if url.startswith("arxiv:au:"):
                who = url.removeprefix("arxiv:au:").strip()
                xml_text, error = self._query(ctx, who)
            elif "arxiv.org/a/" in url:
                author_id = url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".atom2").removesuffix(".html")
                who = author_id.replace("_", " ")
                response = ctx.http.get(f"https://arxiv.org/a/{author_id}.atom2")
                if response.status in (404, 410):
                    return Read(status="gone", error=f"arXiv has no author page {author_id}")
                xml_text, error = response.text, ""
                if response.status != 200 or "<feed" not in response.text:
                    # the author feed is down or odd: ask the API for the same author instead
                    xml_text, error = self._query(ctx, re.sub(r"_\d+$", "", author_id))
            else:
                return Read.failed(f"not an arXiv author source: {url!r}")
        except HttpError as err:
            return Read.failed(str(err))
        if error:
            return Read.failed(error)
        try:
            papers = parse_papers(xml_text)
        except ValueError as err:
            return Read.failed(f"arXiv answered something that is not an Atom feed: {err}")
        name, affiliation = _affiliation(papers, who)
        if not affiliation:  # arXiv rarely lists one; keep what an earlier paper said
            affiliation = str((previous or {}).get("organization") or "")
        content = {
            "query": who,
            "name": name or _author_name(papers, who),
            "organization": affiliation,
            "papers": [{k: v for k, v in p.items() if not k.startswith("_")} for p in papers[:PAPERS]],
        }
        return Read(status="ok", content=content, raw=xml_text[:200_000])

    def guard(self, before: Mapping[str, Any], after: Mapping[str, Any]) -> str:
        if before.get("papers") and not after.get("papers"):
            return "arXiv returned no papers for an author who had some (the API does this when overloaded)"
        return ""

    def diff(self, before: Mapping[str, Any], after: Mapping[str, Any]) -> list[Change]:
        changes: list[Change] = []
        old, new = clean(before.get("organization")), clean(after.get("organization"))
        if old and new and old.lower() != new.lower():
            changes.append(Change("affiliation changed", old, new))
        for paper in unseen(before.get("papers") or [], after.get("papers") or [], cap=PAPERS):
            authors = ", ".join(paper.get("authors") or [])
            changes.append(
                Change(
                    "new paper",
                    "",
                    f"{paper.get('title')}\n{authors}\n{paper.get('url')}",
                    str(paper.get("date") or "")[:10],
                )
            )
        return changes

    def describe(self, content: Mapping[str, Any]) -> str:
        lines = [f"arXiv author {content.get('name') or content.get('query')}"]
        if content.get("organization"):
            lines.append(f"Affiliation: {content['organization']}")
        for paper in content.get("papers") or []:
            authors = ", ".join((paper.get("authors") or [])[:8])
            lines.append(
                f"- {str(paper.get('date'))[:10]} {paper.get('title')} ({authors}) {paper.get('url')}"
            )
        return clip("\n".join(lines), 4000)
