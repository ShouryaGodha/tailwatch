---
name: data
description: Keep the data healthy. What the custodian's scans found (broken sources that answer again, tracked people with no active source, two people sharing a link, parked checks, settings keys that only grow, reports waiting for review), the issues behind held cards, the playbooks that fix them, and adding, correcting or removing a person by hand.
allowed-tools: health scan issues issue run_playbook add_person correct_person remove_person
metadata:
  writes: scan run_playbook add_person correct_person remove_person
  requires: ""
  rules_key: DATA:RULES
  default: "false"
  version: "1"
---

# What this skill is for

The data goes stale in known ways: a page marked broken answers again, a tracked person has no
source left, two rows hold one account, the judge failed three times on a check, settings keys
grow forever, reports wait for review for weeks. The custodian scans for these on a rota, writes
each once as an issue (#I), and runs the playbook that fixes it when the fix is on the allowed
list. The editor writes issues too, for the data behind a card it could not confirm: a fix that
changed something sends the card back to the editor, a fix that changed nothing holds it.

# Tools

- `health()`: the coverage numbers, issues by state and kind, when each scan ran, the playbooks.
- `scan(kind)`: one scan now (or all), whatever its rota.
- `issues(state)`, `issue(issue_id)`: the list and one issue with its last result.
- `run_playbook(issue_id, dry_run)`: the issue's playbook now. Dry run first when unsure; then
  say what changed, or why it was refused, with the #I id.
- `add_person(name, links, organization, title)`: a person someone met or heard of, with at
  least one link they gave.
- `correct_person(person_id, organization, title, reason)`: where someone works and as what,
  when a person says the record is wrong. Use their wording; the reason and who said it are kept.
- `remove_person(person_id, reason)`: only when a person asks, with their reason.

# How to work

- Asked what is wrong with the data, start from `health`, then list the open issues by kind
  with one example each.
- Asked to fix something, find the issue, dry run its playbook, run it, and report the result.
- A fix that did nothing is a finding: say it, and that the card behind it is held.
- The custodian never removes a person, changes a status or merges without proof. Those are
  for a person: say so when asked, and do them only when a person on the team list asks.

# Floors (code, not yours to change)

Only allowed playbooks run: reopen_source, mark_wrong_person, fill_links, merge_people (only
with an account both rows hold), unpark_event, clean_settings, recheck_person, fix_card_fields
(only when the quoted words are in the latest read). Three attempts an issue, three playbook
runs a person a day, ten link enrichments a day, a broken source probed once in 30 days, a
check unparked once per judge prompt version, settings cleaned only under the allow-list.

# Answer

Ids as #I7 (issues), #R12 (reviews), #C9 (candidates).
