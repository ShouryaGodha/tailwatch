# Writing a connector

A connector is a Python class. Built-ins live in `src/tailwatch/connectors/`; your own can live
in a separate package that registers itself through the `tailwatch.connectors` entry point
group, so you never fork tailwatch to add a source. This page builds one, a Mastodon checker;
the finished package is [`examples/custom-connector/`](../examples/custom-connector/).

## 1. The contract in one minute

A **checker** reads one source of one person and returns a `Read`:

```python
from tailwatch.connectors.base import BaseChecker, Change, CheckContext, Read

class MastodonChecker(BaseChecker):
    kind = "mastodon"          # the link kind people carry ("mastodon": "https://mastodon.social/@ada")
    label = "Mastodon"         # the human name in `tailwatch doctor`
    cadence_hours = 24.0       # how often a source is due (tailwatch.yaml can override)
    paid = False
    requires = ()              # env vars that must be set, e.g. ("MASTODON_TOKEN",)

    def read(self, url, previous, ctx) -> Read: ...
    def diff(self, before, after) -> list[Change]: ...
    def guard(self, before, after) -> str: ...      # optional
    def describe(self, content) -> str: ...         # optional
```

The runner does the rest: it calls `read`, checks the result with `guard`, compares it with the
last successful read through `diff` (only when the content's hash moved), records the changes
as events and hands them to the judge.

- `read(url, previous, ctx)` fetches and normalises. `url` is the canonical link
  (see `tailwatch/links.py`; a kind it does not know is normalised as a plain URL). `previous`
  is the last successful `content`, or `None` on the first read. `ctx` gives you `ctx.http`
  (always use it, never `httpx` directly: it is the seam tests replace), `ctx.env`,
  `ctx.options` (your section of `tailwatch.yaml`) and a response cache
  (`ctx.cache_get(provider, key, max_age_hours)` / `ctx.cache_put(provider, key, value)`).
- Return `Read(status="ok", content={...}, raw=answer)`. Keep `content` JSON, with stable keys
  and sorted, capped lists, and leave out what churns on its own (follower counts, signed image
  URLs, "fetched at" times): its hash decides whether `diff` runs at all. Use the shared keys
  where they fit: `name, headline, bio, organization, title, location, website, flags,
  positions, posts, papers, repos, text`.
- `diff(before, after)` returns mechanical `Change(label, before, after, content_date)`s:
  `"bio changed"`, `"new post"`. Never a judgement ("probably left their job"); that is the
  judge's work. The diff of identical content must be `[]`.
- `guard(before, after)` returns a reason when a read *looks* like a change but is really a
  failed read (an empty timeline when the account had posts). That read is recorded as an
  error and never compared.
- `describe(content)` is the compact text the judge reads (4,000 characters at most).

Three rules keep tailwatch honest:

1. **gone only on a definitive signal.** `Read(status="gone")` when the provider says the
   account does not exist (404/410, "user not found"). Timeouts, 429, 5xx and bot walls are
   `Read.failed(...)`: retried, never retired on a guess.
2. **Outages are named.** A refusal that will hit every source of the kind (no credit, a
   rejected key, a rate limit) sets `outage`: `Read.failed("HTTP 402", outage="out of credits")`.
   The pass stops early instead of failing every check.
3. **Spend is reported.** A paid source sets `credits` and/or `usd` on every read, failed
   ones included.

## 2. Write the checker

The core of the example (`examples/custom-connector/src/tailwatch_mastodon/checker.py`):

```python
def read(self, url, previous, ctx):
    instance, user = account_of(url)                      # https://mastodon.social/@ada
    try:
        account = ctx.http.get(f"https://{instance}/api/v1/accounts/lookup", params={"acct": user})
        if account.status in (404, 410):
            return Read(status="gone", error=f"{instance} has no account {user}")
        if account.status != 200:
            return Read.failed(f"{instance} answered HTTP {account.status}",
                               outage="rate limited" if account.status == 429 else "")
        profile = account.json()
        statuses = ctx.http.get(f"https://{instance}/api/v1/accounts/{profile['id']}/statuses",
                                params={"limit": 20, "exclude_reblogs": "true"})
        ...
    except HttpError as error:                            # timeouts and connection errors
        return Read.failed(str(error))
    content = {"handle": ..., "name": ..., "bio": ..., "posts": posts[:20]}
    return Read(status="ok", content=content, raw=profile)

def diff(self, before, after):
    changes = []
    for field, label in (("name", "name changed"), ("bio", "bio changed")):
        if (before.get(field) or "") != (after.get(field) or ""):
            changes.append(Change(label, before.get(field) or "", after.get(field) or ""))
    seen = {p["id"] for p in before.get("posts") or []}
    oldest = min((p["date"] for p in before.get("posts") or []), default="")
    for post in sorted(after.get("posts") or [], key=lambda p: p["date"]):
        if post["id"] not in seen and post["date"] >= oldest:   # older ones slid in after a deletion
            changes.append(Change("new post", "", post["text"], post["date"][:10]))
    return changes
```

## 3. Register it

In your package's `pyproject.toml`:

```toml
[project.entry-points."tailwatch.connectors"]
mastodon = "tailwatch_mastodon:MastodonChecker"
```

Install it into tailwatch's environment (`pip install -e path/to/package`) and
`tailwatch doctor` lists it. A plugin is off until the config turns it on:

```yaml
connectors:
  mastodon: {enabled: true}
```

A **feed** registers the same way: a class with `kind`, `requires` and
`poll(marker, options, ctx) -> (items, new_marker)` returning `FeedItem`s oldest first. Its
first poll (`marker` None) returns `([], marker_of_newest)` so history is not replayed, and a
failed poll raises `tailwatch.connectors.feeds.FeedError`. Fill `FeedItem.mentions` with the
handles and profile URLs an item links (`{"x": ["ada"], "github": ["ada"]}`): that is how the
matcher ties an item to a tracked person.

## 4. Test it without the network

`fixture_transport` answers requests from a dict of URL prefixes, and raises on anything else,
so a test can never reach the network:

```python
from tailwatch.connectors.base import CheckContext
from tailwatch.connectors.http import Http, fixture_transport

def ctx(account, posts):
    return CheckContext(http=Http(transport=fixture_transport({
        "https://mastodon.social/api/v1/accounts/1/statuses": posts,        # dict/list → 200 JSON
        "https://mastodon.social/api/v1/accounts/lookup": account,          # (404, body) → that status
    })))

def test_unknown_account_is_gone():
    assert MastodonChecker().read(URL, None, ctx((404, {"error": "Record not found"}), [])).status == "gone"
```

A route can also be a function of the `Call` (to check the params and headers you send, or to
raise `HttpError` for a timeout). Cover at least: the first read, an unchanged read (`diff` is
`[]`), each change label, 404 → gone, 429/5xx/timeout → error, and each guard.
