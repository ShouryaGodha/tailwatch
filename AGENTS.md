# Working on tailwatch

tailwatch is an agent harness that tracks a set of people across the web, turns what changed
into events, has one model judge each check, verifies the cards, ranks them and sends a daily
digest. A desk agent (Slack, the CLI or MCP) answers questions and carries out a person's
decisions; a custodian repairs data with allow-listed playbooks; a maintainer reads the day and
proposes changes; an engineer (off by default) turns code proposals into draft pull requests.

## Layout

- `src/tailwatch/agent/`: the runtime. `model.py` is the one model client (provider adapters,
  retries, key failover, real cost). `loop.py` is the one agent loop (stop rules, evidence
  wrapping, memoised writes, checkpoints, failed runs keep their spend). `registry.py` holds every
  tool and the one permission check. `routines.py` is the routine table plus `run_routine`
  (logs every run with its cost). `skills.py` is Agent Skills loading. `prompts.py` handles files
  and `{{PLACEHOLDERS}}`.
- `src/tailwatch/store/`: the only writer. `Store` composes mixins, one file per area
  (`core`, `people`, `checks`, `cards`, `desk`, `enrich`, `review`, `custodian`,
  `maintenance`). SQL is written once for SQLite and Postgres (`?` placeholders, ISO-8601 UTC
  text times, JSON as text). Migrations live in `store/migrations/NNNN_*.sql`.
- `src/tailwatch/connectors/`: checkers (one source of one person) and feeds (streams about
  anyone). `base.py` is the contract and `registry.py` the list.
- `src/tailwatch/pipeline/`: `runner` (passes), `watch` (feeds), `judge`, `packs` (signal
  packs), `ranker`, `digest`, `ingest`, `coverage`.
- `src/tailwatch/enrich/`: the enricher agent, its read-only tools, its proof checks, and
  candidates (reports about people not on the list).
- `src/tailwatch/desk/`: the desk's tools (`tools_*.py`, one per skill), the editor and floors,
  the custodian (scans and playbooks), the maintainer and the desk note, intel, the engineer,
  and the chat entry point.
- `src/tailwatch/delivery/`: sinks (console, Markdown, Slack).
- `prompts/` (the judge, the ranker, the desk, signal packs) and `skills/<name>/SKILL.md`
  (Agent Skills format).

## Rules of the design (keep them)

1. **One loop, one model client, one registry.** Every model call goes through
   `agent.model.call`. Every multi-step agent runs through `agent.loop.run`, via
   `agent.routines.run_routine` so it lands in the run log with its cost. Every tool is
   registered once with a `Toolbox` and its flags (`writes`, `evidence`, `memo_errors`).
2. **Code can only lower an agent's verdict.** Floors (the editor) and proof checks (the
   enricher) are deterministic checks after the model answers. They can turn a confirm into a
   hold, never a hold into a confirm.
3. **Append-only history.** Observations, events, verdicts, runs and setting versions are never
   rewritten. A correction is a new verdict (`judged_by` human or desk).
4. **Agents propose, people apply.** The maintainer applies nothing. Proposals, learned rules,
   status changes and removals need a person on the team list (`registry.may_write`). The
   engineer opens draft pull requests and never merges.
5. **Outside text is evidence.** Tool results carrying web or profile text are flagged
   `evidence=True`. Prompts say that evidence is never an instruction.
6. **Budgets are real money.** Paid tools charge the run's `loop.Budget`. Daily caps sum the
   `runs` table (`store.spend_today`). A stopped or failed run still records what it spent.
7. **Say things once.** Notices go out once a day per topic (`store.notice_once`). The digest
   is the only scheduled message besides the desk note.
8. **No network in tests.** Use `connectors.http.fixture_transport`, the scripted model
   (`tests/conftest.py`: `model(answer)`, `verdict(...)`), and an in-memory store.

## Conventions

- Python 3.11+, typed, `ruff check .` clean (line length 110). Docstrings say why, not what.
- User-facing text is plain and short. Ids carry their kind: `#R12` a card review, `#I7` an
  issue, `#P3` a proposal, `#G4` intel, `#C9` a candidate, `#T2` an engineer task.
- `pytest -q` must pass offline. Add a regression test with every fix.
- Do not commit secrets. Configuration is `tailwatch.yaml` and secrets are `.env`.
