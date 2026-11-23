# Configuration

Two files sit next to each other: `tailwatch.yaml` (what to watch, how and where to report)
and `.env` (secrets). `tailwatch init` writes both from a preset. tailwatch looks for
`tailwatch.yaml` in the current folder and its parents, or reads `TAILWATCH_CONFIG`.
`TAILWATCH_DATABASE` overrides the database URL (useful in CI and the cloud).

```yaml
name: AI researchers                 # the digest's title
database: sqlite:///tailwatch.db     # or postgresql://user:pass@host/db (pip install 'tailwatch[postgres]')
signals: career-moves                # a built-in pack, or a path to your own pack's YAML

models:                              # provider:model for each role; empty roles use default
  default: anthropic:claude-opus-5-5
  judge: ""                          # one strict call per check (the highest-volume role)
  editor: ""                         # verifies cards with tools
  enricher: ""                       # finds people's profiles with tools
  identifier: ""                     # who a post is about (one small call)
  ranker: ""
  desk: ""                           # the chat agent
  maintainer: ""
  engineer: ""

priority:
  tiers:                             # most important first; the judge and the ranker read these
    - {name: frontier labs, orgs: [OpenAI, Anthropic, Google DeepMind]}
    - {name: notable, orgs: [Mistral, Cohere]}
  aliases: {Google DeepMind: [DeepMind, GDM]}
  trusted_feeds: [my-announcer]      # feed names whose reports are taken as true

watchlist:
  imports: [watchlist.csv]           # CSV or YAML; see below
  people:
    - name: Ada Lovelace
      organization: Analytical Engines
      title: Research Scientist
      links: {github: https://github.com/ada, homepage: https://ada.example, arxiv: "Lovelace, Ada"}
      tags: [compilers]

connectors:                          # free ones default on, paid ones default off
  github: {enabled: true, cadence_hours: 24}
  linkedin: {enabled: false, cadence_hours: 168, options: {actor: "<apify actor id>"}}

feeds:
  - {kind: hackernews, name: hn-moves, options: {query: "joins OpenAI"}}
  - {kind: rss_feed, name: lab-blog, options: {url: "https://example.org/feed.xml"}}
  - {kind: bluesky_account, name: announcer, options: {handle: someone.bsky.social}}

delivery:
  console: true                      # print the digest in the terminal
  markdown_dir: digests              # write digests/YYYY-MM-DD.md (and the desk log)
  site_url: ""                       # a link printed under the digest, if you host a page
  slack: {digest_channel: C0123, desk_channel: C0456, shadow_channel: ""}

schedule: {timezone: Europe/London, pass_time: "02:00", digest_time: "09:00", maintenance_time: "09:30", tick_minutes: 30}
digest: {cap: 10, window_hours: 26, repost_days: 30, stale_signal_days: 14, old_move_days: 180}
switches:
  verify: shadow                     # off | shadow (the editor runs and records; the digest ignores it) | on (verified cards only)
  rank: true                         # the ranking agent (false: deterministic order)
  engineer: false                    # the engineer opens draft pull requests only when true
  candidates: true                   # turn feed reports about unlisted people into candidates
budgets:                             # USD; the run log is the ledger
  editor_per_card: 0.15
  editor_daily: 10
  enrich_per_person: 0.20
  enrich_daily: 15
  engineer_per_task: 6
  engineer_daily: 20
  maintainer_daily: 2
  desk_per_message: 0.5
team: [U012ABC]                      # Slack user ids that may change things; empty = everyone
require_team_list: false
pricing:                             # add prices for models the table does not know, USD per million tokens
  "openai:gpt-5": [1.25, 0.125, 10.0]
engineer: {repo: you/your-fork, base: main, backend: auto}
```

## The watchlist file

CSV columns: `name, organization, title, tags`, plus one column per link kind (`github`,
`homepage`, `x`, `linkedin`, `arxiv`, `semantic_scholar`, `bluesky`, `rss`, `scholar`), or a
`links` column of space-separated URLs whose kinds are guessed. YAML is a list of the same
fields, with `links` as a mapping.

An arXiv link is either an author query (`"Lovelace, Ada"`) or an `https://arxiv.org/a/<id>`
author page. A Semantic Scholar link is the author page URL or its numeric id.

## Secrets (`.env`)

| Variable | For |
|---|---|
| `ANTHROPIC_API_KEY` (or `OPENAI_API_KEY`, `AZURE_OPENAI_API_KEY` + `AZURE_OPENAI_ENDPOINT`, `OPENROUTER_API_KEY`, `OLLAMA_BASE_URL`) | the model; comma-separate several keys for failover |
| `GITHUB_TOKEN` | optional: a higher GitHub rate limit (it is also the engineer's token) |
| `SEMANTIC_SCHOLAR_API_KEY` | optional: a higher Semantic Scholar rate limit |
| `EXA_API_KEY` | optional, paid: web search for the enricher and the editor |
| `TWITTERAPI_IO_KEY` | optional, paid: the X connector and the X feed |
| `APIFY_API_KEY`, `APIFY_LINKEDIN_ACTOR` | optional, paid: the LinkedIn connector |
| `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` | optional: Slack delivery and the bot (Socket Mode) |
| `TAILWATCH_REFUSAL_FALLBACK=off` | turn off Claude's server-side refusal fallback |
