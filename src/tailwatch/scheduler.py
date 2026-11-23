"""The routine runner. One ``tick`` every ``schedule.tick_minutes`` (30 by default):

- every tick: the feed watchers, the candidate pipeline, the judge, the editor, the custodian;
- once a day, on the first tick at or after its local time: link enrichment, the source pass,
  the digest, the maintainer and its desk note.

A daily job keeps a receipt per local day (the ``jobs`` table): started, complete or failed. A
failed job is retried on later ticks, three attempts in all; a job that failed three times, and
a provider outage a pass ran into, is said once a day through the notice sinks. Jobs run one
after another in this process: ``tailwatch up`` loops forever, ``tailwatch tick`` runs one tick
(for cron, a GitHub Actions schedule or a Modal cron).
"""

from __future__ import annotations

import importlib
import time
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from tailwatch import util

MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class Job:
    name: str
    target: str  # "module:function", called as fn(store, config, env)
    at: str = ""  # "pass_time" | "digest_time" | "maintenance_time" | "HH:MM"; empty means every tick
    offset_minutes: int = 0


EVERY_TICK = (
    Job("watch", "tailwatch.pipeline.watch:watch_feeds"),
    Job("candidates", "tailwatch.enrich.candidates:run"),
    Job("judge", "tailwatch.pipeline.judge:judge_pending"),
    Job("verify", "tailwatch.desk.verify:verify_cards"),
    Job("custodian", "tailwatch.desk.custodian:run"),
)
DAILY = (
    Job("links_enrich", "tailwatch.enrich.links:enrich_links", at="pass_time", offset_minutes=-20),
    Job("pass", "tailwatch.pipeline.runner:run_pass", at="pass_time"),
    Job("digest", "tailwatch.pipeline.digest:post_morning", at="digest_time"),
    Job("maintenance", "tailwatch.desk.maintainer:run_daily", at="maintenance_time"),
)


def resolve(target: str) -> Callable[..., Any] | None:
    module, _, name = target.partition(":")
    try:
        return getattr(importlib.import_module(module), name)
    except (ImportError, AttributeError):
        return None


def due_at(job: Job, config: Any, day: datetime) -> datetime:
    value = getattr(config.schedule, job.at, None) if job.at.endswith("_time") else job.at
    hour, minute = (int(x) for x in str(value or "00:00").split(":"))
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(minutes=job.offset_minutes)


def daily_due(store: Any, config: Any, *, now: datetime | None = None) -> list[tuple[Job, str]]:
    local = util.local_time(config.schedule.timezone, now)
    day = local.strftime("%Y-%m-%d")
    due = []
    for job in DAILY:
        if local < due_at(job, config, local):
            continue
        receipt = store.job(job.name, day)
        if receipt is None or (receipt["state"] == "failed" and int(receipt["attempts"]) < MAX_ATTEMPTS):
            due.append((job, day))
        elif receipt["state"] == "running" and (receipt["started_at"] or "") < util.ago(hours=12):
            due.append((job, day))  # a crashed process: the receipt never finished
    return due


def notify(store: Any, config: Any, env: Mapping[str, str], topic: str, text: str, *, sinks: list[Any] | None = None) -> bool:
    """Say ``text`` once a day per topic."""
    if not store.notice_once(topic, util.local_day(config.schedule.timezone)):
        return False
    if sinks is None:
        from tailwatch.delivery.slack import sinks_for

        sinks = sinks_for(config, env)
    for sink in sinks:
        try:
            sink.notice(text)
        except Exception:
            pass
    return True


def run_job(store: Any, config: Any, env: Mapping[str, str], job: Job, *, day: str = "", log: Any = print) -> dict[str, Any]:
    fn = resolve(job.target)
    if fn is None:
        return {"job": job.name, "skipped": "not installed"}
    attempt = store.job_start(job.name, day) if day else 0
    try:
        result = fn(store, config, env) or {}
    except Exception as error:
        detail = f"{type(error).__name__}: {error}"
        if day:
            store.job_finish(job.name, day, state="failed", error=detail + "\n" + traceback.format_exc()[-1500:])
            if attempt >= MAX_ATTEMPTS:
                notify(store, config, env, f"job:{job.name}", f"The {job.name} job failed {attempt} times today: {detail}")
        if log:
            log(f"{job.name} failed: {detail}")
        return {"job": job.name, "error": detail}
    if day:
        store.job_finish(job.name, day, state="complete", result=_jsonable(result))
    for outage in (result.get("outages") or []) if isinstance(result, dict) else []:
        notify(store, config, env, f"outage:{outage.split(':')[0]}", f"{outage}. Checks of this kind wait for the next pass.")
    return {"job": job.name, "result": result}


def _jsonable(value: Any) -> Any:
    return util.loads(util.dumps(value), {})


def tick(store: Any, config: Any, env: Mapping[str, str], *, now: datetime | None = None, log: Any = print,
         only: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    from tailwatch.store import LockBusy

    results = []
    try:
        with store.lock("tick", ttl_s=3 * 3600):
            for job in EVERY_TICK:
                if not only or job.name in only:
                    results.append(run_job(store, config, env, job, log=log))
            for job, day in daily_due(store, config, now=now):
                if not only or job.name in only:
                    results.append(run_job(store, config, env, job, day=day, log=log))
    except LockBusy:
        results.append({"job": "tick", "skipped": "another tick is running"})
    return results


def run_forever(store: Any, config: Any, env: Mapping[str, str], *, log: Any = print,
                stop: Callable[[], bool] = lambda: False) -> None:
    step = max(1, int(config.schedule.tick_minutes))
    while not stop():
        started = util.now()
        tick(store, config, env, log=log)
        minutes = step - (started.minute % step)
        wake = started.replace(second=0, microsecond=0) + timedelta(minutes=minutes)
        while not stop() and util.now() < wake:
            time.sleep(min(30.0, max(0.5, (wake - util.now()).total_seconds())))
