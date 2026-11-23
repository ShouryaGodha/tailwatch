# The engineer's handbook

This is what tailwatch's desk engineer reads first, every time, before it touches the code. It is
written for a careful engineer who has never seen the repository: where things are, the rules of
the design, how tests are written, and the recipes of tasks done before. `AGENTS.md` at the root
is the short version; `docs/architecture.md` is the long one.

You work in a fresh copy of the repository in a temporary folder. Your task is in
`.engineer/task.json`, a map of every module in `.engineer/code_map.md`, and the lessons the team
applied from earlier tasks in `.engineer/rules.md`. When you finish, the harness runs the suite,
has a reviewer read your diff, checks the diff in code and opens a draft pull request. A person
merges it on GitHub. You never push, merge or deploy.

## What tailwatch is, in one paragraph

tailwatch tracks a list of people across the web. Checkers read one source of one person
(a profile, a homepage, a feed) and every read is an observation; a difference between two reads
is an event. The judge (one strict model call per check, prompt `prompts/judge.md` plus a signal
pack under `prompts/packs/`) turns a check into a verdict: one of the pack's signals or none. The
editor checks every card before the digest carries it, floors in code can only lower its
verdict, the ranker orders the digest, and the digest posts once a day. A desk agent (Slack, the
CLI, MCP) answers people and carries out their decisions; the custodian repairs data with
allow-listed playbooks; the maintainer reads the day and proposes changes; you turn code
proposals into draft pull requests.

## Where things are

- `src/tailwatch/agent/`: the runtime. `model.py` is the one model client, `loop.py` the one
  agent loop, `registry.py` every desk tool once with the one permission check, `routines.py`
  the table of repeated jobs and `run_routine` (which logs every run with its cost), `skills.py`
  the Agent Skills loader, `prompts.py` prompt files and `{{PLACEHOLDERS}}`.
- `src/tailwatch/store/`: the only code that talks to the database. `Store` is a set of mixins,
  one file per area. SQL is written once for SQLite and Postgres: `?` placeholders, times as
  ISO-8601 UTC text, JSON as text. A new table or column is a new numbered file in
  `store/migrations/`.
- `src/tailwatch/connectors/`: checkers and feeds; `base.py` is the contract.
- `src/tailwatch/pipeline/`: the pass, the watchers, the judge, the packs, the ranker, the digest.
- `src/tailwatch/desk/`: the desk's tools (`tools_*.py`, one module per skill), the editor and
  its floors, the custodian, the maintainer and the desk note, the engineer (you), notices.
- `src/tailwatch/enrich/`: the enricher and candidates.
- `prompts/` and `skills/<name>/` (SKILL.md, plus agent.md for a worker's instructions).
- `tests/`: the offline suite. `tests/conftest.py` has the in-memory store (`store`), a config
  (`config`), the scripted model (`model(answer)`) and a fake checker.

## The rules of the design (keep them)

1. One loop, one model client, one registry. Every model call goes through `agent.model.call`;
   every multi-step agent through `agent.loop.run` via `agent.routines.run_routine`. Every desk
   tool is registered once with a `Toolbox`, and a tool name is unique across the repository.
2. Code can only lower an agent's verdict. Floors and proof checks turn a confirm into a hold,
   never a hold into a confirm.
3. Append-only history: observations, events, verdicts, runs and setting versions are never
   rewritten. A correction is a new row.
4. Agents propose, people apply. Learned rules, floors and aliases change only when a person
   applies a proposal; each change is a new setting version.
5. Outside text is evidence: a tool that returns web or profile text is flagged `evidence=True`,
   and prompts say evidence is never an instruction.
6. Budgets are real money: paid tools charge the run's `loop.Budget`; daily caps sum the `runs`
   table.
7. Say things once: a notice goes out once a day per topic.
8. No network in tests: the scripted model, `connectors.http.fixture_transport`, the in-memory
   store.

## Conventions

- Python 3.11+, typed, `ruff check .` clean at line length 110. Short functions; a docstring
  says why, not what; no new abstraction for one use; the file's own patterns first.
- User-facing text is plain and short. Ids carry their kind: `#R12` a card review, `#I7` an
  issue, `#P3` a proposal, `#G4` intel, `#C9` a candidate, `#T2` an engineer task.
- Prompts are plain prose for a model. A value code owns is a `{{PLACEHOLDER}}` filled by
  `prompts.render`; an unknown placeholder fails at read time.
- Never a secret in a file. Configuration is `tailwatch.yaml`, secrets are `.env`, and you never
  read either.

## Tests

- Every code change has a regression test beside the tests of the module it touches, using
  their fixtures. A prompt change has a test that reads the prompt text.
- Run one file while you work (`python -m pytest -q tests/test_x.py`) and the whole suite
  (`python -m pytest -q`) before you report. The harness runs it again; a failure comes back to
  you.
- A store method gets a test against the in-memory SQLite store.

## What the harness checks after you

Only the allowed paths change (by default `src/tailwatch/`, `prompts/`, `skills/`, `tests/`,
`docs/`); the protected ones never (the deploy files under `.github/`, the engineer's own code
`src/tailwatch/desk/engineer.py` and `engineer_runner.py`, any `.env`); at most 30 files, each at
most 400KB; no secret in any file; a change under `src/` comes with a change under `tests/`; the
suite passes; a reviewer who did not write the change answers ship. Anything else is refused and
no pull request is opened.

## Recipes

1. **A bug in one module.** Find the code path from the code map, read the module's test file,
   write a failing test with its fixtures, make the smallest fix, run the file, then the suite.
2. **A new rule for the judge.** One paragraph in the pack's rules file (`prompts/packs/<pack>.md`)
   or in `prompts/judge.md`, in the prompt's voice; the answer schema in `pipeline/packs.py`
   widened only when the answer needs a new value; a test that reads the prompt. A changed
   prompt changes the pack's `prompt_version` by itself.
3. **A new desk tool.** A handler in the skill's `tools_<skill>.py` with `@TOOLS.tool(...)` and
   its flags; its name added to the skill's `allowed-tools` in `skills/<skill>/SKILL.md`; a test
   that calls it through `registry.execute` with a `ToolContext`.
4. **A new playbook for the custodian.** A function in `desk/playbooks.py` that returns what it
   changed, idempotent, named in `KNOWN`; a scan that opens the issues; a test with the
   in-memory store.
5. **A store query.** A method on the area's mixin in `store/<area>.py`, SQL that runs on both
   databases, and a test against the in-memory store.
6. **A question, not a change.** Answer it in the report from the code and this handbook, with
   file paths, and change nothing (outcome `no_change`).

## When to stop and ask

A change to a budget, a secret, the deploy, the engineer itself, more than one paragraph of the
judge's rules, or anything that would write people's data: those are the owner's. Say what you
would do and ask one question in the report (outcome `needs_decision`).
