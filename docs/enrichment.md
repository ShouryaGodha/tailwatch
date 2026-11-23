# Enrichment

Two jobs share one agent, the **enricher**: it finds a person's public profiles and proves
each one with a verbatim quote from text a tool fetched. Code then checks the proof and can
only lower it (rule 2 of the design: a proof check turns a confirm into a hold, never the
other way).

- **Candidates** (`enrich/candidates.py`, every tick): reports about people who are not on the
  list become people on the list, or wait for a person's decision.
- **Link enrichment** (`enrich/links.py`, daily, 20 minutes before the pass): tracked people
  with missing links get them filled.

## Candidates

A feed item that matches nobody on the list is kept whole as a candidate (`#C12`). The
`candidates` job takes each waiting row through three steps.

1. **Identify.** One tool-less model call (`identify_mention`, prompt
   `prompts/mention_subject.md`) says who the post is about: the name, the handles in the post
   that are theirs (never the announcer's), the organization left and joined, the role. A
   round-up of several people becomes one candidate per person on the same item (item id
   `<item>#n`), already identified, so the model is asked once per post. A post about nobody
   (a launch, a joke) is dismissed.
2. **Filter** (free, before anything is paid for):
   - only reports that touch an organization in `priority.tiers` (either end of the move, any
     tier; aliases from `priority.aliases` and `ENRICHMENT:ALIASES`) go on; the rest are
     `out_of_scope` and still show in the digest's "seen elsewhere" line. With no tiers
     configured, everything passes;
   - a role naming one of `ENRICHMENT:OUT_OF_SCOPE_ROLES` (empty by default) is dismissed;
   - someone already on the list (one of the handles, or the name with the organization) gets
     the report as an event instead, and nobody is added; a removed person is never added back;
   - a second report about an open candidate's person and move is dismissed as a duplicate.
3. **Enrich and decide.** The enricher runs in `report` mode. A verified profile link of a kind
   an enabled checker reads (LinkedIn first when it is on) adds the person:
   `added_because = "report: <feed> <item>"`, organization the one left (so the judge reads the
   report as the move it is), and the report recorded as their first event, judged in the same
   tick. Otherwise the candidate waits as `needs_review` with the reason. A link another person
   on the list holds sends the candidate to review.

A failed run counts an attempt; after three the row waits for review. A provider outage does
not burn attempts: three in a row stop the job until the next tick.

### A person's decisions

`candidates.decide(store, config, env, candidate_id, action, links=..., note=..., by=...)`:

- `add`: with the links the person confirmed, or the ones the enricher verified;
- `dismiss`: needs a reason, kept in their words;
- `retry`: back to the enricher on the next tick, with a fresh budget.

On the desk these are the enrichment skill's `add_mention`, `dismiss_mention` and
`enrich_again` (and the core `decide` with kind `C`). They write, so when a team list is set
only a person on it may use them. Every decision is kept on the candidate.

## Link enrichment

Daily: yesterday's newcomers (whatever brought them), then up to `ENRICHMENT:LINKS_PER_DAY`
(default 25) tracked people with only one link, oldest first. The enricher runs in
`fill_links` mode: the organization the list holds is the one to find, and the links already
on the row are proven anchors (a GitHub whose blog is the person's known homepage is
cross-linked). Verified links fill empty kinds only; a link another person holds is skipped;
`people.links_enriched_at` marks everyone looked at, so nobody is paid for twice. The
custodian's `fill_links` playbook runs the same thing for one person
(`agent.enrich_person(..., person_id=..., mode="fill_links")`).

## The enricher

`enrich_person(store, config, env, seed, toolkit=None)` runs the `enrich_person` routine in
the one loop, with the worker prompt `skills/enrichment/agent.md` plus the learned rules
(`ENRICHMENT:RULES`). Its answer, per link: `kind, url, verdict (verified | unverified |
rejected), confidence, quote, quote_url, facts, reason`, plus `name, organization, title,
summary, concerns`.

### Tools (read-only, `enrich/tools.py`)

| tool | arguments | provider | key | cost |
|---|---|---|---|---|
| `web_search` | `query`, `kind` (people, personal_site, github, news, papers, web), `limit` | Exa `/search` | `EXA_API_KEY` (without it: an error result, the run goes on) | ~$0.007 |
| `web_read` | `url` | Exa `/contents`, else a plain fetch | `EXA_API_KEY` optional | ~$0.001 / free |
| `github_user` | `login` | GitHub API | `GITHUB_TOKEN` optional | free |
| `github_search` | `query` | GitHub API | `GITHUB_TOKEN` optional | free |
| `arxiv_search` | `name` | arXiv API | none | free |
| `scholar_search` | `name` | Semantic Scholar | `SEMANTIC_SCHOLAR_API_KEY` optional | free |
| `bluesky_profile` | `handle` | Bluesky AppView | none | free |
| `x_profile` | `handle` | twitterapi.io | `TWITTERAPI_IO_KEY` | ~$0.0002 |
| `x_posts` | `handle` | twitterapi.io | `TWITTERAPI_IO_KEY` | up to ~$0.003 |
| `linkedin_profile` | `url` | Apify (the LinkedIn connector) | `APIFY_API_KEY` | ~$0.005, at most 2 a run |
| `list_lookup` | `urls`, `name` | the list | none | free |

Every answer is cached in `source_cache` (30 days; LinkedIn pulls share the connector's cache),
so a replay costs nothing and a cache hit does not count against a tool's cap. Per-tool caps
and nominal prices are in `enrich/budget.py`. A provider that refuses the key or rate-limits
stops the run (`NEED_KEY`, `RATE_LIMITED`) and the model answers with what it has; so does an
empty budget (`BUDGET`). Every tool but `list_lookup` returns outside text, wrapped as evidence.

### Proof checks (`enrich/verify.py`)

Each can only lower a verdict:

1. the URL must be one a tool returned (or the source gave), else rejected;
2. the quote must be in the text a tool returned for `quote_url` (at most 40 words);
3. the confidence must reach `confidence_min`;
4. the name the profile shows must be compatible (`enrich/names.py`: initials, a dropped
   middle name, nicknames, swapped order, a surname cut to an initial) at `name_level_min`; a
   LinkedIn with a different name is rejected;
5. the profile must show an expected organization, or be cross-linked with a proven profile;
   a LinkedIn still showing the old organization as current is fine until `stale_days` after
   the report;
6. a page linking a different LinkedIn than the proven one is rejected;
7. a link another person on the list holds is rejected and the candidate goes to review.

## Tunables

Versioned settings with limits (`enrich/settings.py`); every change is a new version and a
revert is one more:

| key | default | limits |
|---|---|---|
| `ENRICHMENT:FLOORS` | `confidence_min 0.8`, `stale_days 180`, `name_level_min initials` | 0.6–0.95, 30–365, initials/variant/exact |
| `ENRICHMENT:RULES` | none | at most 15 sentences of 300 characters; no URLs, Slack ids or posting orders |
| `ENRICHMENT:ALIASES` | none | spelling → organization, at most 200 |
| `ENRICHMENT:OUT_OF_SCOPE_ROLES` | none | at most 40 words |
| `ENRICHMENT:LINKS_PER_DAY` | 25 | 0–200 |

On the desk: `settings_show`, `settings_change(key, value, note)`, `settings_revert(key, version)`.
The maintainer may propose changes; a person applies them.

## Costs

- per person: `budgets.enrich_per_person` (default $0.20), tools and model together;
- per day: `budgets.enrich_daily` (default $15), summed from the run log over
  `enrich_person` and `identify_mention`. When it is spent, both jobs stop and the desk is told
  once that day.

A typical report costs one small identify call, one or two searches and a few free profile
reads: a few cents with a mid-sized model. Every run is in the run log with its model and tool
spend, also when it failed.
