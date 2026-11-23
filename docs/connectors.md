# Connectors

tailwatch reads the web through two kinds of connector:

- a **checker** reads one source of one person (a GitHub profile, a homepage, an arXiv author)
  on a cadence and reports *mechanical* changes ("bio changed", "new paper", "role ended");
- a **feed** polls a stream about anyone (a Hacker News search, a lab's news feed, an
  announcer account) and hands each new item to the matcher, which ties it to tracked people
  or keeps it as a candidate.

Neither ever judges. What a change means is the judge's call. The contract lives in
[`src/tailwatch/connectors/base.py`](../src/tailwatch/connectors/base.py); writing your own is
in [writing-a-connector.md](writing-a-connector.md).

## Checkers

| kind | source | cost | key | cadence | changes it reports |
|---|---|---|---|---|---|
| `github` | GitHub REST API | free | optional `GITHUB_TOKEN` (comma-separated tokens rotate on rate limits) | 24 h | name, bio, company, location, website, x handle changed; hireable flag changed; new repository; new release (with a token) |
| `homepage` | the page itself (or the Jina reader) | free | none (`JINA_API_KEY` optional with `reader: jina`) | 24 h | title changed; text changed (only the removed and added sentences); profile links changed |
| `arxiv` | arXiv API / author Atom feed | free | none | 24 h | new paper; affiliation changed |
| `semantic_scholar` | Semantic Scholar Graph API | free | optional `SEMANTIC_SCHOLAR_API_KEY` | 72 h | affiliation changed; website changed; new paper |
| `bluesky` | Bluesky public AppView | free | none | 24 h | name changed; bio changed; new post (own posts, not reposts) |
| `rss` | the person's blog feed (RSS or Atom) | free | none | 24 h | new post; feed title changed |
| `x` | twitterapi.io | **paid**: 18 credits a profile, +15 a post when posts are fetched (100,000 credits = $1): about $0.0002 on a quiet day, up to $0.003 | `TWITTERAPI_IO_KEY` | 24 h | name, bio, location, website changed; protected flag changed; new post |
| `linkedin` | an Apify actor | **paid**: about $0.005 a profile (actor pricing; set `usd_per_profile`) | `APIFY_API_KEY` | 168 h | role ended; role added; title changed; headline changed; location changed; about changed; open to work flag changed |

Free checkers are on unless you turn them off; paid ones are off until you turn them on *and*
their key is set. `tailwatch doctor` lists every connector as on, off, or missing a key.

### Turning connectors on and off

```yaml
# tailwatch.yaml
connectors:
  github: {enabled: true, cadence_hours: 12}
  homepage: {enabled: true, options: {reader: jina}}
  semantic_scholar: {enabled: false}
  x: {enabled: true}                       # needs TWITTERAPI_IO_KEY in .env
  linkedin:
    enabled: true                          # needs APIFY_API_KEY in .env
    cadence_hours: 168
    options:
      actor: apimaestro~linkedin-profile-detail
      usd_per_profile: 0.005
```

`cadence_hours` overrides how often a source of that kind is due; `options` is handed to the
connector as `ctx.options`.

### Connector options

- **github**: `release_repos` (how many of the most starred recently pushed repositories to
  read releases of; default 3 with a token, 0 anonymous, since anonymous calls get 60 an hour).
- **homepage**: `reader: jina` reads pages through `https://r.jina.ai/<url>`, which renders
  pages built by JavaScript. Off by default.
- **semantic_scholar**: `limit` (papers read per call, default 100; the API has no sort order,
  so the newest 20 are kept client-side).
- **x**: `include_replies` (default false). Replies cost credits like any post.
- **linkedin**: `actor` (or env `APIFY_LINKEDIN_ACTOR`; default
  `apimaestro~linkedin-profile-detail`, a public actor from the Apify store), `input` (the
  actor input as a dict, `{url}` replaced by the profile URL; default
  `{"username": "{url}", "includeEmail": false}`), `usd_per_profile`, `timeout` (seconds).

### What each checker stores

Every read's `content` is JSON with stable keys, so its hash only changes when something did:

| kind | content keys |
|---|---|
| `github` | `login, name, bio, organization, location, website, x_handle, flags.hireable, public_repos, repos[name, description, created, url, stars], releases[id, repo, tag, name, date, url]` |
| `homepage` | `url, title, text, links` (`kind:handle` of profile links on the page), `truncated`, `seen_before` |
| `arxiv` | `query, name, organization, papers[id, title, date, url, venue, authors]` |
| `semantic_scholar` | `name, organization, website, stats{papers, h_index}, papers[id, title, date, url, venue, authors]` |
| `bluesky` | `handle, did, name, bio, posts[id, text, date, url]` |
| `rss` | `name, website, posts[id, title, text, date, url]` |
| `x` | `handle, user_id, name, bio, location, website, flags.protected, statuses, posts[id, text, date, url], posts_fetched_at` |
| `linkedin` | `name, headline, bio, location, organization, title, flags.open_to_work, positions[title, company, start, end, current]` |

Stars and counts are rounded or left out (they churn on their own); lists are sorted and capped.

### The rules every checker keeps

- **gone vs error.** `gone` only when the provider says the account or page does not exist
  (404/410, "user not found", "no profile found"). Timeouts, rate limits, 5xx, bot walls and
  parked domains are errors, retried sooner. Two gone reads in a row retire a source.
- **guards.** A read that looks like a change but is a failed read is recorded as an error and
  never compared: a homepage that shrank to an empty shell, an X timeline or blog feed that came
  back empty, a LinkedIn answer with no experience when the last read had positions.
- **outages.** A provider-wide refusal sets `Read.outage` (`out of credits`, `key rejected`,
  `rate limited`, `no credential`). When the first checks of a pass all report the same outage,
  the pass stops and leaves the rest due.
- **spend.** Paid checkers report `credits` and `usd` on every read, failed ones included.
- **X saves credits.** The posts call (the expensive one) is made only when the profile's post
  count moved since the last read or the posts were fetched 14 or more days ago.
- **LinkedIn reuses answers.** Every actor answer is cached (`linkedin`, profile URL) before it
  is interpreted; a cached answer younger than the cadence is used instead of a new paid run.

## Feeds

| kind | source | cost | key | options | marker |
|---|---|---|---|---|---|
| `hackernews` | HN Algolia search (stories) | free | none | `query` | newest `created_at_i` |
| `rss_feed` | any RSS or Atom feed | free | none | `url` | newest entry id |
| `bluesky_account` | an announcer's Bluesky posts | free | none | `handle` | newest `indexedAt` |
| `x_account` | an announcer's X posts via twitterapi.io | **paid**: 15 credits a post returned, 15 at least (about $0.003 a poll of 20) | `TWITTERAPI_IO_KEY` | `handle` | newest post id |

The first poll of a feed only marks where it is; history is never replayed. Each item carries
`mentions` (kind → handles or URLs it links: `x`, `github`, `linkedin`, `homepage`, `bluesky`;
the announcer itself is left out) and, when the source gives them, `names` (X mentions carry
display names). A failed poll raises `FeedError` and the marker stays where it was.

```yaml
# tailwatch.yaml
feeds:
  - kind: hackernews
    name: hn-ai-moves
    options: {query: "joins OpenAI"}
  - kind: rss_feed
    name: lab-news
    options: {url: "https://lab.example/news.xml"}
  - kind: bluesky_account
    name: moves-on-bluesky
    options: {handle: "moves.example.com"}
  - kind: x_account                        # paid, needs TWITTERAPI_IO_KEY
    name: moves-on-x
    options: {handle: "some_announcer"}
```
