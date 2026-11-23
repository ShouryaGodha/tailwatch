"""Feed watchers: read new items from each configured feed and route them.

An item about a tracked person becomes a check of that person (a paused ``feed:<name>`` source
with one observation and one event), judged in the same run. An item about nobody on the list
becomes a candidate for the enrichment pipeline. The matching is mechanical and never decides
anything: when an item could be about more than one tracked person, each gets the event, the
text says so, and the judge decides whether it fits.

Matching, in order: a link or handle the item carries that belongs to a tracked person; a name
the feed gives explicitly; a tracked person's full name in the text next to their organization
(the whole name or its last word: "DeepMind" for "Google DeepMind").
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from tailwatch import links as link_rules
from tailwatch import util
from tailwatch.connectors.base import FeedItem
from tailwatch.connectors.http import Http, HttpError


def marker_key(feed_id: str) -> str:
    return f"FEED:{feed_id}:MARKER"


class PeopleIndex:
    def __init__(self, people: list[dict[str, Any]], aliases: Mapping[str, list[str]] | None = None) -> None:
        self.people = people
        self.aliases = {k.lower(): [a.lower() for a in v] for k, v in (aliases or {}).items()}

    def org_words(self, org: str) -> list[str]:
        org = org.strip().lower()
        if not org:
            return []
        words = [org, *self.aliases.get(org, [])]
        last = org.split()[-1] if org.split() else ""
        if len(last) > 3:
            words.append(last)
        return words

    def by_name(self, name: str) -> list[dict[str, Any]]:
        name = " ".join(name.lower().split())
        return [p for p in self.people if " ".join(p["name"].lower().split()) == name]

    def in_text(self, text: str) -> list[dict[str, Any]]:
        lowered = " ".join(text.lower().split())
        found = []
        for person in self.people:
            name = " ".join(person["name"].lower().split())
            if len(name.split()) < 2 or not re.search(rf"\b{re.escape(name)}\b", lowered):
                continue
            words = self.org_words(person.get("organization") or "")
            if not words or any(w in lowered for w in words):
                found.append(person)
        return found


def match(store: Any, item: FeedItem, index: PeopleIndex) -> list[dict[str, Any]]:
    found: dict[int, dict[str, Any]] = {}
    for kind, values in (item.mentions or {}).items():
        for value in values:
            person = store.person_by_link(kind, value)
            if person and person["status"] == "tracked":
                found[person["id"]] = person
    if not found:
        for name in item.names or []:
            for person in index.by_name(name):
                found[person["id"]] = person
    if not found:
        for person in index.in_text(item.text):
            found[person["id"]] = person
    return list(found.values())


def record_report(store: Any, feed_id: str, item: FeedItem, person: Mapping[str, Any], *, others: int) -> int:
    kind = f"feed:{feed_id}"
    source_id = store.ensure_source(person["id"], kind, item.url or f"{feed_id}:{item.id}", cadence_hours=0, status="paused")
    text = item.text if not others else f"{item.text}\n\n(This item matched {others + 1} tracked people; judge whether it is about this one.)"
    return store.record_check(store.source(source_id), status="ok",
                              content={"text": item.text, "url": item.url, "author": item.author, "published_at": item.published_at},
                              content_hash=util.sha(f"{feed_id}:{item.id}"),
                              changes=[{"label": f"reported by {feed_id}", "after": text, "content_date": item.published_at}],
                              person_snapshot={"name": person["name"], "organization": person.get("organization"),
                                               "title": person.get("title")})


def watch_feeds(store: Any, config: Any, env: Mapping[str, str], *, http: Http | None = None, judge: bool = True,
                log: Any = print) -> dict[str, Any]:
    from tailwatch.connectors.base import CheckContext
    from tailwatch.connectors.registry import feed_classes

    classes = feed_classes()
    index = PeopleIndex(store.people(status="tracked"), config.priority.aliases)
    summary: dict[str, Any] = {"feeds": [], "reports": 0, "candidates": 0}
    for feed in config.feeds:
        cls = classes.get(feed.kind)
        row = {"feed": feed.id, "items": 0, "reports": 0, "candidates": 0, "error": ""}
        summary["feeds"].append(row)
        if cls is None:
            row["error"] = f"no feed connector named {feed.kind}"
            continue
        missing = [k for k in getattr(cls, "requires", ()) if not env.get(k)]
        if missing:
            row["error"] = f"needs {', '.join(missing)}"
            continue
        ctx = CheckContext(http=http or Http(), env=env, options=feed.options,
                           cache_get=lambda p, k, age: store.cache_get(p, k, max_age_hours=age), cache_put=store.cache_put)
        marker = store.get_setting(marker_key(feed.id))
        try:
            items, new_marker = cls().poll(marker, feed.options, ctx)
        except (HttpError, ValueError) as error:
            row["error"] = str(error)
            continue
        for item in items:
            row["items"] += 1
            people = match(store, item, index)
            for person in people:
                record_report(store, feed.id, item, person, others=len(people) - 1)
                row["reports"] += 1
            if not people and config.switches.candidates:
                _id, created = store.add_candidate(feed.id, item.id, url=item.url, text=item.text,
                                                   subject={"names": item.names, "mentions": item.mentions, "author": item.author,
                                                            "published_at": item.published_at})
                row["candidates"] += int(created)
        if new_marker and new_marker != marker:
            store.set_setting(marker_key(feed.id), new_marker, history=False)
        summary["reports"] += row["reports"]
        summary["candidates"] += row["candidates"]
        if log and (row["items"] or row["error"]):
            log(f"feed {feed.id}: {row['items']} item(s), {row['reports']} about tracked people, {row['candidates']} new candidate(s)"
                + (f" — {row['error']}" if row["error"] else ""))
    if judge and summary["reports"]:
        from tailwatch.pipeline.judge import judge_pending

        summary["judge"] = judge_pending(store, config, env, log=log)
    return summary


def guess_links(values: list[str]) -> dict[str, str]:
    """Sort bare links into kinds (for the candidate pipeline's seed)."""
    out: dict[str, str] = {}
    for value in values:
        kind = link_rules.guess_kind(value)
        out.setdefault(kind, value)
    return out
