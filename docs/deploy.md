# Running tailwatch

| Where | How | Database | Slack bot |
|---|---|---|---|
| Your laptop | `tailwatch up` | SQLite file | yes (Socket Mode) |
| Any server or VM | `docker compose up -d` | SQLite in `./data`, or the `postgres` profile | yes |
| GitHub Actions (no server) | `deploy/github-actions-schedule.yml` | a hosted Postgres | digest only |
| Modal (serverless) | `modal deploy deploy/modal_app.py` | a hosted Postgres | digest only; run `tailwatch slack` elsewhere |
| cron | `*/30 * * * * cd ~/watch && tailwatch tick` | SQLite file | digest only |

## Local

```bash
uv tool install "tailwatch[slack,mcp] @ git+https://github.com/ShouryaGodha/tailwatch"
mkdir my-watch && cd my-watch
tailwatch init --preset ai-researchers   # tailwatch.yaml, .env, watchlist.csv
$EDITOR watchlist.csv .env
tailwatch doctor                         # keys, connectors, the signal pack
tailwatch check                          # the first pass records what is there now
tailwatch up                             # a tick every 30 minutes; the digest at digest_time
```

The first read of a source records only. Changes start from the second read, so the first
digest with signals comes a day after the first pass.

## Docker

```bash
mkdir data && tailwatch init data        # or copy a tailwatch.yaml, .env and watchlist into ./data
docker compose up -d
docker compose logs -f
docker compose exec tailwatch tailwatch coverage
```

For Postgres, set `database: postgresql://tailwatch:tailwatch@db/tailwatch` in
`data/tailwatch.yaml` and run `docker compose --profile postgres up -d`.

## Slack

1. Create an app from `deploy/slack-manifest.yaml` (api.slack.com/apps → From a manifest) and
   install it to your workspace.
2. `SLACK_BOT_TOKEN` is the Bot User OAuth Token (`xoxb-`). `SLACK_APP_TOKEN` is an App-Level
   Token with `connections:write` (`xapp-`).
3. Invite the bot to two channels. Put their ids in `delivery.slack.digest_channel` (the
   daily digest; the bot answers mentions and replies in a digest's thread) and
   `desk_channel` (every message is for the desk agent).
4. `tailwatch up` connects over Socket Mode. You need no public URL.

## The MCP server

```bash
claude mcp add tailwatch -- tailwatch mcp            # Claude Code, run from your watch folder
```

For Claude Desktop, Cursor or any MCP client, add this to the client's MCP config:

```json
{"mcpServers": {"tailwatch": {"command": "tailwatch", "args": ["mcp", "--config", "/path/to/tailwatch.yaml"]}}}
```

The tools are `find_people`, `person`, `signals`, `why`, `digest_preview`, `coverage`, `runs`,
`add_person`, `set_status`, `remember` and `ask_desk` (the desk agent with every skill).

## What it costs

With the free connectors, the only spend is the model:

- **The judge.** One call per check that found a change. Most daily checks find nothing and
  cost nothing. A check with a change costs about 3 to 8k input tokens and under 1k output.
  With the default model that is about $0.02 to $0.04 per change, and the system prompt is
  cached.
- **The ranker.** One call a day.
- **The editor and the enricher.** Bounded per card or person (`budgets.*`) and per day.
- **The maintainer.** One call a day.

`tailwatch runs` shows every run's real cost, and the desk note carries the day's spend. To
cut cost, set `models.judge` to a smaller model (for example `anthropic:claude-haiku-5-5`), or
run everything locally with `ollama:<model>` at no cost.
