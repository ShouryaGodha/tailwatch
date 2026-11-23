---
name: intel
description: What the team heard. Someone says something about a person, an organization or everyone ("people in the interpretability group are leaving", "Ada is unhappy since the reorg"); you keep it, dated and under their name; the judge and the editor read it when they decide about a matching person; after a month it is asked about again. A correction of where someone works is the data skill's correct_person, not intel.
allowed-tools: remember intel confirm_intel
metadata:
  writes: remember confirm_intel
  requires: ""
  rules_key: INTEL:RULES
  default: "true"
  version: "1"
---

# What this skill is for

People hear things the profiles do not show yet. Kept as intel, dated and attributed, such a
thing tells the judge where to look and how much a real signal matters. It is never evidence
that anything changed, and never a reason to call a vague line a move.

# Tools

- `remember(text, scope_kind, scope_value, heard_where)`: one entry in the person's own words,
  trimmed to the fact. scope_kind everyone for a market-wide thing, organization for a company
  (scope_value: its name), person for one person (scope_value: the id from find_person, or the
  name when it is unambiguous). heard_where: where they heard it, when they said.
- `intel(state)`: active (default), retired, all, with #G ids, who said it and when.
- `confirm_intel(intel_id, keep)`: a reply to "still true?": keep true gives another 30 days;
  keep false retires the entry.

# How to work

- "I heard that ...", "someone told me ...", "FYI ..." from a person on the team list is intel:
  keep it, then repeat it back in one line with its #G id and when it will be asked about. Ask
  one question only when the scope is unclear (which company? which person?).
- A correction of the data ("Ada is not at X, she is at a stealth startup") is not intel: load
  the data skill and use `correct_person`.
- Asked what we know about a company or a person, read `intel` and answer with the #G ids and
  who said what, when.
- Never invent intel, never widen what a person said, never put a link, an id or a mention in
  the text: the fact only.

# Floors (code, not yours to change)

The text is under 500 characters and carries no link, no id and no Slack mention; an
organization scope needs the organization's name, a person scope one person; only a person on
the team list adds or confirms an entry.

# Answer

Ids as #G4.
