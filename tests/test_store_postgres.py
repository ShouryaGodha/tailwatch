"""The same store on Postgres. Runs only when TAILWATCH_TEST_DSN points at a local, empty
database (CI starts one); each test gets its own schema."""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest

from tailwatch.store import Database, Store

DSN = os.environ.get("TAILWATCH_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN or not any(h in DSN for h in ("localhost", "127.0.0.1")),
                                reason="set TAILWATCH_TEST_DSN to a local Postgres to run")


@pytest.fixture
def pg_store(config: Any) -> Any:
    schema = f"t_{uuid.uuid4().hex[:10]}"
    admin = Database(DSN)
    admin.execute(f"CREATE SCHEMA {schema}")
    separator = "&" if "?" in DSN else "?"
    db = Database(f"{DSN}{separator}options=-csearch_path%3D{schema}")
    db.migrate()
    yield Store(db, config)
    db.close()
    admin.execute(f"DROP SCHEMA {schema} CASCADE")
    admin.close()


def test_the_core_flow_on_postgres(pg_store: Any) -> None:
    store = pg_store
    person_id, created = store.add_person("Ada Lovelace", organization="Analytical Engines", links={"github": "https://github.com/ada"})
    assert created and store.add_person("Ada", links={"github": "github.com/ADA"}) == (person_id, False)
    source_id = store.ensure_source(person_id, "github", "https://github.com/ada", cadence_hours=24)
    assert store.due_sources("github", before="9999", limit=5)[0]["id"] == source_id
    observation = store.record_check(store.source(source_id), status="ok", content={"bio": "b"},
                                     changes=[{"label": "bio changed", "before": "a", "after": "b"}])
    assert store.pending_checks(5)[0]["events"][0]["label"] == "bio changed"
    verdict_id = store.save_verdict(observation, person_id, {"signal": "left", "summary": "s", "now": {"organization": "X"}})
    assert store.is_current(verdict_id)
    assert len(store.waiting_cards(since="0000", channel="digest")) == 1
    store.add_post(verdict_id, person_id, channel="digest", digest_id=None, move_key="left|x")
    assert store.posted_moves(person_id, since="0000")[0]["move_key"] == "left|x"
    assert store.set_setting("K", [1]) == 1 and store.set_setting("K", [2]) == 2
    assert store.revert_setting("K", 2) == 3 and store.get_setting("K") == [1]
    with store.lock("x"):
        pass
    run_id = store.open_run("judge", subject="s")
    store.finish_run(run_id, None, state="done")
    assert store.runs(limit=1)[0]["id"] == run_id
    assert store.read_only_query("SELECT COUNT(*) AS n FROM people")[0]["n"] == 1
    issue_id, opened = store.open_issue("k", "s", playbook="fill_links")
    assert opened and store.open_issue("k", "s") == (issue_id, False)
