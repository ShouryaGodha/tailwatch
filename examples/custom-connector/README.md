# tailwatch-mastodon: an example connector package

A working third-party checker for [tailwatch](../../README.md): it reads a Mastodon profile
through the instance's public API (no key) and reports `name changed`, `bio changed` and
`new post`. It is small on purpose, a template to copy for your own source.

## Install and turn it on

```sh
pip install -e examples/custom-connector      # from the tailwatch checkout, into tailwatch's environment
tailwatch doctor                              # lists "mastodon" among the connectors
```

Plugins are off until the config turns them on:

```yaml
# tailwatch.yaml
connectors:
  mastodon: {enabled: true, cadence_hours: 24}
```

Then give people a `mastodon` link, the profile URL: `https://mastodon.social/@ada`.

## How it plugs in

`pyproject.toml` declares one entry point in the `tailwatch.connectors` group:

```toml
[project.entry-points."tailwatch.connectors"]
mastodon = "tailwatch_mastodon:MastodonChecker"
```

tailwatch's registry loads every entry point in that group next to the built-ins. The class
implements the checker contract from `tailwatch.connectors.base`: `read`, `diff`, `guard` and
`describe`, plus the class attributes `kind`, `label`, `cadence_hours`, `paid` and `requires`.

## Tests

```sh
cd examples/custom-connector && pytest
```

The tests answer every request from fixtures through `tailwatch.connectors.http.fixture_transport`,
so they never touch the network. See [writing a connector](../../docs/writing-a-connector.md)
for the walkthrough.
