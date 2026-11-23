"""Semantic Scholar: an author's profile (name, affiliations, homepage) and newest papers.

Two calls on the Graph API (``https://api.semanticscholar.org/graph/v1``):

- ``/author/<id>?fields=name,affiliations,homepage,paperCount,hIndex``
- ``/author/<id>/papers?fields=title,year,publicationDate,venue,url,externalIds,authors&limit=100``

The papers endpoint has no sort order, so up to ``limit`` papers are read and the newest 20 kept.
``SEMANTIC_SCHOLAR_API_KEY`` is optional and sent as ``x-api-key``; without it the shared
anonymous pool answers 429 often, which is reported as the outage "rate limited".

Options: ``limit`` (papers read per call, default 100, at most 1000).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, ClassVar

from tailwatch.connectors._text import clean, clip, text_change, unseen
from tailwatch.connectors.base import BaseChecker, Change, CheckContext, Read
from tailwatch.connectors.http import HttpError

API = "https://api.semanticscholar.org/graph/v1"
PAPERS = 20


def author_id_of(url: str) -> str:
    found = re.search(r"/author/(?:[^/]+/)?(\d{3,})", url) or re.search(r"(\d{3,})", url)
    return found.group(1) if found else ""


def _paper(item: Mapping[str, Any]) -> dict[str, Any]:
    external = item.get("externalIds") or {}
    url = clean(item.get("url")) or (f"https://arxiv.org/abs/{external['ArXiv']}" if external.get("ArXiv") else "")
    return {"id": clean(item.get("paperId")), "title": clean(item.get("title")),
            "date": clean(item.get("publicationDate") or item.get("year") or ""), "url": url,
            "venue": clean(item.get("venue")),
            "authors": [clean(a.get("name")) for a in item.get("authors") or [] if isinstance(a, dict) and a.get("name")]}


class SemanticScholarChecker(BaseChecker):
    kind: ClassVar[str] = "semantic_scholar"
    label: ClassVar[str] = "Semantic Scholar"
    cadence_hours: ClassVar[float] = 72.0
    paid: ClassVar[bool] = False
    requires: ClassVar[tuple[str, ...]] = ()

    def read(self, url: str, previous: Mapping[str, Any] | None, ctx: CheckContext) -> Read:
        author = author_id_of(url)
        if not author:
            return Read.failed(f"not a Semantic Scholar author: {url!r}")
        headers = {"x-api-key": str(ctx.env["SEMANTIC_SCHOLAR_API_KEY"])} if ctx.env.get("SEMANTIC_SCHOLAR_API_KEY") else {}
        limit = max(1, min(int(ctx.options.get("limit") or 100), 1000))
        try:
            profile = ctx.http.get(f"{API}/author/{author}", headers=headers,
                                   params={"fields": "name,affiliations,homepage,paperCount,hIndex"})
            if failure := self._failure(profile.status, author):
                return failure
            papers = ctx.http.get(f"{API}/author/{author}/papers", headers=headers, params={
                "fields": "title,year,publicationDate,venue,url,externalIds,authors", "limit": limit})
            if failure := self._failure(papers.status, author, papers_call=True):
                return failure
            profile_json, papers_json = profile.json(), papers.json()
        except HttpError as error:
            return Read.failed(str(error))
        except ValueError as error:
            return Read.failed(f"Semantic Scholar answered something that is not JSON: {error}")
        items = [_paper(p) for p in (papers_json or {}).get("data") or [] if isinstance(p, dict) and p.get("paperId")]
        items.sort(key=lambda p: (p["date"], p["id"]), reverse=True)
        content = {"name": clean(profile_json.get("name")),
                   "organization": "; ".join(clean(a) for a in profile_json.get("affiliations") or [] if clean(a)),
                   "website": clean(profile_json.get("homepage")),
                   "stats": {"papers": int(profile_json.get("paperCount") or 0),
                             "h_index": int(profile_json.get("hIndex") or 0)},
                   "papers": items[:PAPERS]}
        return Read(status="ok", content=content, raw={"author": profile_json})

    @staticmethod
    def _failure(status: int, author: str, papers_call: bool = False) -> Read | None:
        if status == 200:
            return None
        if status == 404 and not papers_call:
            return Read(status="gone", error=f"Semantic Scholar has no author {author}")
        if status == 429:
            return Read.failed("Semantic Scholar answered 429", outage="rate limited")
        if status in (401, 403):
            return Read.failed(f"Semantic Scholar refused the key (HTTP {status})", outage="key rejected")
        return Read.failed(f"Semantic Scholar answered HTTP {status}")

    def guard(self, before: Mapping[str, Any], after: Mapping[str, Any]) -> str:
        if before.get("papers") and not after.get("papers") and (after.get("stats") or {}).get("papers"):
            return "the paper list came back empty for an author with papers"
        return ""

    def diff(self, before: Mapping[str, Any], after: Mapping[str, Any]) -> list[Change]:
        changes = text_change("affiliation changed", before.get("organization"), after.get("organization"))
        changes += text_change("website changed", before.get("website"), after.get("website"))
        for paper in unseen(before.get("papers") or [], after.get("papers") or [], cap=PAPERS):
            venue = f" ({paper['venue']})" if paper.get("venue") else ""
            changes.append(Change("new paper", "", f"{paper.get('title')}{venue}\n"
                                  f"{', '.join(paper.get('authors') or [])}\n{paper.get('url')}".strip(),
                                  str(paper.get("date") or "")[:10]))
        return changes

    def describe(self, content: Mapping[str, Any]) -> str:
        stats = content.get("stats") or {}
        lines = [f"Semantic Scholar author {content.get('name', '')}: {stats.get('papers', 0)} papers, "
                 f"h-index {stats.get('h_index', 0)}"]
        if content.get("organization"):
            lines.append(f"Affiliation: {content['organization']}")
        if content.get("website"):
            lines.append(f"Homepage: {content['website']}")
        for paper in content.get("papers") or []:
            lines.append(f"- {paper.get('date')} {paper.get('title')}" + (f" ({paper['venue']})" if paper.get("venue") else ""))
        return clip("\n".join(lines), 4000)
