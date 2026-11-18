---
name: cli-create-lel-service-backlog-issue
description: Create one Epic on the fixed LEL Service board through the task-runner Jira protocol CLI. Use for an explicitly requested LEL board Epic; do not use for other projects, issue types, subtasks, comments, assignment, or status changes.
---

# Create an LEL Service board Epic through the CLI

Use the installed `scripts/jira-protocol.mjs` relative to this `SKILL.md`.
Read `references/jira-protocol.md` before execution. The runtime is installed
with this skill; never substitute a similarly named global command. Run it on
the host that holds the authorized Jira session: locally when the session is
local, or on Lenovo through the generic signed queue when the session lives
only there (see `docs/lenovo-hosted-scripts.md` in the pinned task-runner
checkout; same two-phase flow, no Jira-specific wrapper). Launch every
invocation with `NODE_USE_SYSTEM_CA=1` in the environment (PowerShell:
`$env:NODE_USE_SYSTEM_CA='1'`). TLS verification stays enabled; add no error
handling around it.

## Prepare

Draft one concise Summary and Description from supplied facts. Preserve the
user's primary problem and target result ahead of secondary benefits. Keep
unresolved facts as questions instead of inventing scope or acceptance checks.
When exact commands or flags belong in the Jira wiki-markup description, wrap
them in a closed `{noformat}` or `{code}` block and compare the literals before
planning.

The protocol has no issue search or JQL command. Compare any duplicate
candidates already supplied by the user, and stop on a likely duplicate until
the user decides whether a separate Epic is intended. When no candidate list
exists, disclose `duplicate search: unavailable in this CLI` before requesting
confirmation. That disclosure is a known limitation, not evidence of absence.

## Plan, confirm, and apply

Run the command without `--confirm` first:

```text
NODE_USE_SYSTEM_CA=1 node <skill-dir>/scripts/jira-protocol.mjs create-epic \
  --summary <summary> --description <description> \
  [--field <server-field-id>=<value>]...
```

For a long, quoted, Unicode, or multiline Summary or Description, write the
exact UTF-8 bytes to a file and pass `--summary-file` / `--description-file`
instead of the inline form. Inline and file forms of one field are mutually
exclusive; the CLI refuses both together and reads the file as UTF-8. The file
forms are the bridge-safe forms over the LAN share: stage the UTF-8 file to
the LAN share first, then pass the Lenovo-side share path through the existing
generic signed-queue mechanism (`docs/lenovo-hosted-scripts.md`; no
Jira-specific wrapper). The file is read as UTF-8 without re-encoding;
`createEpic` then trims outer whitespace from Summary (pre-existing behavior,
also applied to the inline form) and keeps Description byte-exact. Do not rely
on leading or trailing whitespace in Summary.

`MissingRequiredFields` names safe server-declared field ids and labels. Ask
for or derive only supported values, then plan again. Never guess custom-field
ids, static defaults, Team values, or Initiative classification. Planning and
missing-field discovery perform no write.

For `AwaitingConfirmation`, show the exact Summary, Description, supplied
fields, required-field model, duplicate-search limitation, and `confirmation`.
Only after the user confirms that live plan may a later invocation repeat the
identical inputs with `--confirm <confirmation>`. A refreshed server model or
default changes the digest and requires a new review; do not reuse the old one.

## Readback and recovery

Report `Completed` only with the returned issue key and the CLI readback. Its
contract proves project `LEL`/`10848`, issue type `Epic`/`10000`, and placement
on the board of rapid view `298290`. It also proves exact persisted Summary and
Description and a matching persisted value/id/key/name for every submitted
custom field. Show the `persistedFields` receipt; do not call creation complete
if any requested field is absent or mismatched. `detail-view --issue <KEY>` may
inspect the bounded GreenHopper view, but it remains explicitly incomplete and
does not replace the key-specific creation readback.

`UnknownOutcome` means a write may have happened. The CLI has no search command
that can prove absence or recover a missing returned key, so preserve the
receipt and stop without another create. HTTP 401/403 and other policy denials
are terminal for this route; report them without changing clients, credentials,
or protocols.
