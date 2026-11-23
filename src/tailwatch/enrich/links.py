"""The daily link enrichment: the enricher in ``fill_links`` mode for people already listed.

Who is looked at, in order: yesterday's newcomers (whatever brought them: a report, an import,
the config), then up to ``ENRICHMENT:LINKS_PER_DAY`` (default 25) tracked people with only one
link, oldest first. Verified links fill empty kinds only (``store.add_links``; a link another
person holds is skipped there), and ``people.links_enriched_at`` marks everyone looked at, so
nobody is paid for twice. A failed run leaves the person unmarked for tomorrow. The daily cap
is the candidates' cap: both jobs spend the same ``enrich_person`` budget.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from tailwatch import util
from tailwatch.agent import loop
from tailwatch.connectors.http import Http
from tailwatch.enrich import agent, candidates
from tailwatch.enrich import settings as tunables
from tailwatch.enrich.tools import Toolkit

FAILURE_STOP = 3


def people_to_look_at(store: Any, config: Any) -> list[dict[str, Any]]:
    start = util.local_time(config.schedule.timezone).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    newcomers = store.newcomers_to_enrich(since=util.iso(start), limit=200)
    backfill = store.thin_people(limit=tunables.links_per_day(store), exclude=tuple(p["id"] for p in newcomers))
    return newcomers + backfill


def enrich_links(store: Any, config: Any, env: Mapping[str, str], *, http: Http | None = None,
                 log: Any = print) -> dict[str, Any]:
    counts: dict[str, Any] = {"people": 0, "links_added": 0, "filled": [], "failed": 0, "usd": 0.0, "stopped": ""}
    failures = 0
    chosen = people_to_look_at(store, config)
    for index, person in enumerate(chosen):
        stop = candidates._cap_reached(store, config, env, waiting=len(chosen) - index)
        if stop:
            counts["stopped"] = stop
            break
        counts["people"] += 1
        toolkit = Toolkit(store, config, env, budget=loop.Budget(float(config.budgets.enrich_per_person)), http=http)
        try:
            found = agent.enrich_person(store, config, env, agent.seed_for_person(person), toolkit=toolkit)
        except Exception as error:
            counts["failed"] += 1
            counts["usd"] = round(counts["usd"] + float(getattr(error, "usd", 0.0) or 0.0), 6)
            failures += 1
            if log:
                log(f"link enrichment for #{person['id']} failed: {type(error).__name__}: {util.clip(str(error), 200)}")
            if failures >= FAILURE_STOP:
                counts["stopped"] = f"{failures} failures in a row"
                break
            continue
        failures = 0
        counts["usd"] = round(counts["usd"] + found["usd"], 6)
        empty = {kind: url for kind, url in found["verified"].items() if not (person.get("links") or {}).get(kind)}
        added = store.add_links(person["id"], empty) if empty else {}
        store.mark_links_enriched(person["id"])
        if added:
            counts["links_added"] += len(added)
            counts["filled"].append({"person_id": person["id"], "links": added})
            if log:
                log(f"links for #{person['id']} {person['name']}: added {', '.join(sorted(added))}")
    return counts
