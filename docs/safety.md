# Safety, privacy and responsible use

tailwatch reads public pages about people. Use it the way you would want it used about you.

- **Public sources only.** The built-in connectors read public profiles, public feeds and
  public APIs. The paid LinkedIn and X connectors go through third-party providers. Check that
  your use complies with each platform's terms and your local law (for example the GDPR's
  rules on processing personal data).
- **Nothing is sent to the people you track.** tailwatch never contacts anyone. Its only
  outputs are your digest, your desk channel and your files.
- **Data stays with you.** The database is a local SQLite file or your own Postgres. The model
  provider you configure sees the evidence for each check. If that is a concern, run
  `ollama:<model>` locally.
- **Removal is remembered.** `ignored` people and removed links are never re-added by an
  import or a feed report.

## How the harness guards itself

- **Prompt injection.** Every tool result that carries outside text (a web page, a profile, a
  post) is wrapped as `<evidence>`, and any closing tag inside it is defanged. Every prompt
  says that evidence is data and never instructions. Agents that read the web have read-only
  tools. The desk's writing tools need a person on the team list.
- **Agents cannot raise their own verdicts.** Floors and proof checks in code can only lower
  what an agent concluded.
- **Agents propose, people apply.** The maintainer applies nothing. Learned rules are scanned
  (no URLs, no ids, no orders about posting), versioned, and applied by a person. The
  custodian runs only allow-listed, idempotent playbooks. It never removes people or changes a
  status.
- **The engineer is off by default.** When on, it works in a temporary checkout with a
  scrubbed environment (no secrets), touches only allow-listed paths, must add a test with any
  code change, passes a secret scan and a fresh-session review, and opens a **draft** pull
  request. It never merges or deploys.
- **Budgets are hard caps.** Per run and per day, on real cost, including failed runs.
- **The desk's SQL is read-only.** `query` accepts one `SELECT`, refuses write keywords, and
  is capped in rows.
