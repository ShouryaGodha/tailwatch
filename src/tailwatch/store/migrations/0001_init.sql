-- tailwatch schema. Written once for SQLite and Postgres: the adapter turns
-- "INTEGER PRIMARY KEY AUTOINCREMENT" into "BIGSERIAL PRIMARY KEY" for Postgres.
-- Times are ISO-8601 UTC text (sortable everywhere); JSON is text.
-- Append-only: observations, events, verdicts, runs, settings_history.

CREATE TABLE people (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  organization TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  since TEXT NOT NULL DEFAULT '',
  headline TEXT NOT NULL DEFAULT '',
  location TEXT NOT NULL DEFAULT '',
  links TEXT NOT NULL DEFAULT '{}',
  tags TEXT NOT NULL DEFAULT '[]',
  notes TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'tracked',          -- tracked | left | ignored
  status_reason TEXT NOT NULL DEFAULT '',
  status_at TEXT,
  status_by TEXT NOT NULL DEFAULT '',
  added_because TEXT NOT NULL DEFAULT '',
  links_enriched_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX people_status ON people(status);

-- One spelling of every link, so nobody is listed twice.
CREATE TABLE person_links (
  person_id INTEGER NOT NULL REFERENCES people(id),
  kind TEXT NOT NULL,
  link_key TEXT NOT NULL,
  url TEXT NOT NULL,
  PRIMARY KEY (kind, link_key)
);
CREATE INDEX person_links_person ON person_links(person_id);

-- Removed people are remembered so an import never adds them back.
CREATE TABLE removals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  person_id INTEGER,
  name TEXT NOT NULL DEFAULT '',
  link_keys TEXT NOT NULL DEFAULT '[]',
  reason TEXT NOT NULL DEFAULT '',
  by_whom TEXT NOT NULL DEFAULT '',
  at TEXT NOT NULL
);

CREATE TABLE sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  person_id INTEGER NOT NULL REFERENCES people(id),
  kind TEXT NOT NULL,
  url TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',           -- active | broken | private | wrong_person | paused
  status_note TEXT NOT NULL DEFAULT '',
  cadence_hours REAL NOT NULL DEFAULT 24,
  last_checked_at TEXT,
  next_due_at TEXT,
  last_observation_id INTEGER,
  fail_streak INTEGER NOT NULL DEFAULT 0,
  gone_streak INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE (person_id, kind, url)
);
CREATE INDEX sources_due ON sources(status, kind, next_due_at);

-- One row per check of one source. A check that finds nothing new still writes a row:
-- that is what "checked" means. judge_state is the only bookkeeping that changes.
CREATE TABLE observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id INTEGER NOT NULL REFERENCES sources(id),
  person_id INTEGER NOT NULL REFERENCES people(id),
  observed_at TEXT NOT NULL,
  fetch_status TEXT NOT NULL,                      -- ok | error | gone | private
  content_hash TEXT NOT NULL DEFAULT '',
  content TEXT NOT NULL DEFAULT '{}',
  person_snapshot TEXT NOT NULL DEFAULT '{}',
  error TEXT NOT NULL DEFAULT '',
  credits REAL NOT NULL DEFAULT 0,
  first_read INTEGER NOT NULL DEFAULT 0,
  judge_state TEXT NOT NULL DEFAULT 'none',        -- none | pending | judged | parked
  judge_attempts INTEGER NOT NULL DEFAULT 0,
  judge_error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX observations_source ON observations(source_id, observed_at);
CREATE INDEX observations_judge ON observations(judge_state, observed_at);
CREATE INDEX observations_person ON observations(person_id, observed_at);

-- One row per difference between two reads of one source. Never changes.
CREATE TABLE events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  person_id INTEGER NOT NULL REFERENCES people(id),
  source_id INTEGER NOT NULL REFERENCES sources(id),
  observation_id INTEGER NOT NULL REFERENCES observations(id),
  previous_observation_id INTEGER,
  label TEXT NOT NULL,                             -- mechanical: bio changed, role ended, new post ...
  before_text TEXT NOT NULL DEFAULT '',
  after_text TEXT NOT NULL DEFAULT '',
  content_date TEXT NOT NULL DEFAULT '',
  observed_at TEXT NOT NULL
);
CREATE INDEX events_observation ON events(observation_id);
CREATE INDEX events_person ON events(person_id, observed_at);

-- One judgement of one check (an observation and its events). Append-only: the newest row
-- for an observation is its current verdict; a person's correction supersedes the model's.
CREATE TABLE verdicts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  observation_id INTEGER NOT NULL REFERENCES observations(id),
  person_id INTEGER NOT NULL REFERENCES people(id),
  signal TEXT NOT NULL,                            -- a signal from the pack, or none
  confidence REAL NOT NULL DEFAULT 0,
  summary TEXT NOT NULL DEFAULT '',
  context TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '',
  now TEXT NOT NULL DEFAULT '{}',
  category TEXT NOT NULL DEFAULT '',
  priority INTEGER NOT NULL DEFAULT 0,
  eligible INTEGER NOT NULL DEFAULT 1,
  flags TEXT NOT NULL DEFAULT '[]',
  same_as INTEGER NOT NULL DEFAULT 0,
  supersedes INTEGER NOT NULL DEFAULT 0,
  signal_date TEXT NOT NULL DEFAULT '',
  wrong_person INTEGER NOT NULL DEFAULT 0,
  judged_by TEXT NOT NULL DEFAULT 'model',         -- model | gate | human | desk | report
  model TEXT NOT NULL DEFAULT '',
  prompt_version TEXT NOT NULL DEFAULT '',
  guidance_hash TEXT NOT NULL DEFAULT '',
  by_whom TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX verdicts_observation ON verdicts(observation_id, id);
CREATE INDEX verdicts_person ON verdicts(person_id, created_at);
CREATE INDEX verdicts_signal ON verdicts(signal, created_at);

-- The editor's record of one card: a person and the check that speaks for them.
CREATE TABLE card_reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  person_id INTEGER NOT NULL REFERENCES people(id),
  verdict_id INTEGER NOT NULL REFERENCES verdicts(id),
  observation_id INTEGER NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending',           -- pending | verified | fixing | held | dropped | superseded
  answer TEXT NOT NULL DEFAULT '{}',
  floors TEXT NOT NULL DEFAULT '{}',
  decision TEXT NOT NULL DEFAULT '{}',
  rounds INTEGER NOT NULL DEFAULT 0,
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (verdict_id)
);
CREATE INDEX card_reviews_state ON card_reviews(state, updated_at);

CREATE TABLE card_labels (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  verdict_id INTEGER NOT NULL REFERENCES verdicts(id),
  label TEXT NOT NULL,                             -- right | wrong | unsure
  holdout INTEGER NOT NULL DEFAULT 0,
  note TEXT NOT NULL DEFAULT '',
  by_whom TEXT NOT NULL DEFAULT '',
  at TEXT NOT NULL
);

-- Digest deliveries: prepared -> sending -> sent, with a stable message key so an uncertain
-- send is reconciled before anything is sent again.
CREATE TABLE digests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel TEXT NOT NULL,
  day TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'prepared',
  message_key TEXT NOT NULL,
  ts TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL DEFAULT '',
  payload TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (channel, day)
);

-- One row per card a digest carried: the receipts behind "one card per move".
CREATE TABLE posts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  verdict_id INTEGER NOT NULL REFERENCES verdicts(id),
  person_id INTEGER NOT NULL,
  digest_id INTEGER,
  channel TEXT NOT NULL,
  move_key TEXT NOT NULL DEFAULT '',
  by_whom TEXT NOT NULL DEFAULT 'digest',
  at TEXT NOT NULL,
  UNIQUE (verdict_id, channel)
);
CREATE INDEX posts_person ON posts(person_id, at);

-- Reports about people who are not on the list.
CREATE TABLE candidates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  feed TEXT NOT NULL,
  item_id TEXT NOT NULL,
  url TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'new',               -- new | identified | out_of_scope | enriching | needs_review | added | dismissed
  subject TEXT NOT NULL DEFAULT '{}',
  enrichment TEXT NOT NULL DEFAULT '{}',
  reason TEXT NOT NULL DEFAULT '',
  person_id INTEGER,
  attempts INTEGER NOT NULL DEFAULT 0,
  digested_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (feed, item_id)
);
CREATE INDEX candidates_state ON candidates(state, updated_at);

-- What the team heard: where to look, never evidence of a change.
CREATE TABLE intel (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  text TEXT NOT NULL,
  scope_kind TEXT NOT NULL DEFAULT 'everyone',     -- everyone | organization | person
  scope_value TEXT NOT NULL DEFAULT '',
  said_by TEXT NOT NULL DEFAULT '',
  heard_where TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'active',            -- active | retired
  asks INTEGER NOT NULL DEFAULT 0,
  review_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- Data problems for the custodian: one per kind and subject.
CREATE TABLE issues (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  subject_key TEXT NOT NULL,
  playbook TEXT NOT NULL DEFAULT '',
  args TEXT NOT NULL DEFAULT '{}',
  state TEXT NOT NULL DEFAULT 'open',              -- open | running | fixed | failed | wontfix
  attempts INTEGER NOT NULL DEFAULT 0,
  found_by TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  result TEXT NOT NULL DEFAULT '{}',
  review_id INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  closed_at TEXT,
  UNIQUE (kind, subject_key)
);
CREATE INDEX issues_state ON issues(state, updated_at);

-- What the maintainer proposes and a person applies. Engineer tasks are proposals of setting CODE.
CREATE TABLE proposals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  skill TEXT NOT NULL DEFAULT '',
  setting TEXT NOT NULL,                           -- RULES | ALIASES | FLOORS | PLAYBOOK | OTHER | CODE | REVERT
  op TEXT NOT NULL DEFAULT 'add',
  value TEXT NOT NULL DEFAULT '{}',
  rationale TEXT NOT NULL DEFAULT '',
  evidence TEXT NOT NULL DEFAULT '[]',
  state TEXT NOT NULL DEFAULT 'proposed',          -- proposed | applied | rejected | expired | running | failed
  urgent INTEGER NOT NULL DEFAULT 0,
  by_whom TEXT NOT NULL DEFAULT 'maintainer',
  decided_by TEXT NOT NULL DEFAULT '',
  decided_at TEXT,
  note TEXT NOT NULL DEFAULT '',
  pr_url TEXT NOT NULL DEFAULT '',
  thread TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE INDEX proposals_state ON proposals(state, created_at);

-- The run log: one row per routine run or chat message, with its real cost. The single spend
-- ledger the daily caps sum (model cost plus tool cost).
CREATE TABLE runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  routine TEXT NOT NULL,
  run_key TEXT,
  subject TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'running',           -- running | done | capped | failed | silent
  model TEXT NOT NULL DEFAULT '',
  model_calls INTEGER NOT NULL DEFAULT 0,
  tokens_in INTEGER NOT NULL DEFAULT 0,
  tokens_cached INTEGER NOT NULL DEFAULT 0,
  tokens_out INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL NOT NULL DEFAULT 0,
  tool_usd REAL NOT NULL DEFAULT 0,
  priced INTEGER NOT NULL DEFAULT 1,
  stopped TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT '',
  checkpoint TEXT NOT NULL DEFAULT '{}',
  answer TEXT NOT NULL DEFAULT '',
  trace TEXT NOT NULL DEFAULT '[]',
  started_at TEXT NOT NULL,
  finished_at TEXT,
  UNIQUE (run_key)
);
CREATE INDEX runs_routine ON runs(routine, started_at);

-- Job receipts for the routine runner: one row per job and day.
CREATE TABLE jobs (
  job TEXT NOT NULL,
  day TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'running',           -- running | complete | failed
  attempts INTEGER NOT NULL DEFAULT 0,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  result TEXT NOT NULL DEFAULT '{}',
  error TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (job, day)
);

-- Versioned key/value: switches, learned rules, floors, aliases, watcher markers.
CREATE TABLE settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  updated_by TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL
);
CREATE TABLE settings_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT NOT NULL,
  value TEXT NOT NULL,
  version INTEGER NOT NULL,
  by_whom TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  at TEXT NOT NULL
);
CREATE INDEX settings_history_key ON settings_history(key, version);

-- Every paid answer is cached, so a replay is free.
CREATE TABLE source_cache (
  provider TEXT NOT NULL,
  cache_key TEXT NOT NULL,
  response TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  PRIMARY KEY (provider, cache_key)
);

-- Cooperative locks that work on both databases.
CREATE TABLE locks (
  name TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  expires_at TEXT NOT NULL
);

-- Chat thread memory and notices said once a day.
CREATE TABLE threads (
  thread_key TEXT PRIMARY KEY,
  lines TEXT NOT NULL DEFAULT '[]',
  updated_at TEXT NOT NULL
);
CREATE TABLE notices (
  topic TEXT NOT NULL,
  day TEXT NOT NULL,
  at TEXT NOT NULL,
  PRIMARY KEY (topic, day)
);

CREATE TABLE schema_migrations (
  version TEXT PRIMARY KEY,
  applied_at TEXT NOT NULL
);
