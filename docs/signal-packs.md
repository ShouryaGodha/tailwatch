# Signal packs: what the judge looks for

The judge asks one question per check: does this evidence show one of the pack's signals? A
pack is a YAML file and a rules file in plain words. The judge's prompt (`prompts/judge.md`)
and its answer schema (the `signal` and `category` enums) are both built from the pack, so a
new use of tailwatch is a new pack, not a code change.

| Pack | Signals | For |
|---|---|---|
| `career-moves` | 🔴 left · 🟡 looking | recruiters, talent teams, anyone following people's careers |
| `research-output` | 📄 paper · 🚀 release · 📣 announcement | following what researchers and maintainers publish and ship |
| `founder-watch` | 🌱 founding · 💰 raised · 🚀 launched | investors and founder programs |

## Writing your own

`my-pack.yaml`:

```yaml
name: conference-talks
version: "1"
description: >-
  You assess a tracked person's new evidence for someone who books speakers and wants to know when
  people they follow give talks or start speaking about a new topic.
signals:
  - name: talk
    heading: Talks
    mark: "🎤"
    describe: The person announced or gave a talk, keynote or panel, with a date or a venue.
  - name: new_topic
    heading: New topics
    mark: "🧭"
    describe: The person started writing or speaking about a topic absent from their earlier work.
    stale_after_days: 30          # a signal older than this leaves the queue
categories:
  keynote: a keynote or invited talk
  other: anything else
  out_of_scope: activity that is not speaking (judged and recorded, never a card)
priority_categories: [keynote]
gate_labels: []                   # mechanical labels recorded as none with no model call
move_key: item                    # dedupe cards per item (item) or per signal and destination (destination)
rules_file: my-pack.md            # the reading rules, in plain words
```

Then set `signals: ./my-pack.yaml` in `tailwatch.yaml`. A pack's `prompt_version` (the pack
name, its version and a hash of the judge prompt and the rules) is stored on every verdict. The
maintainer therefore knows which rules a verdict was judged under, and never grades an old
verdict against a new rule.

Fields:

- `old_after_days` on a signal: a verdict whose `signal_date` is older is recorded as none with
  the flag `old_move`. This catches an imported roster that was already out of date.
- `stale_after_days` on a signal: the digest stops offering a card this old.
- `gate_labels`: changes made only of these labels never reach the model (a flag alone is
  not a job search).
- `move_key: destination` dedupes on the signal plus the destination organization ("left for
  X" posts once). `move_key: item` dedupes on the check itself.
