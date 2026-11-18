---
name: cli-create-jira-subtask
description: Create one LEL subtask under an explicit LEL parent through the task-runner Jira protocol CLI. Use for a requested LEL subtask only; do not use for Epics, other projects, parent-less issues, comments, assignment, or status changes.
---

# Create an LEL subtask through the CLI

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

Draft one concise Summary and Description from supplied facts. Require one
explicit, unambiguous parent key (`LEL-N`); never invent or guess a parent.
The parent must already exist in project `LEL`/`10848` and must not itself be
a subtask. The protocol has no issue search or JQL command. Compare any
duplicate candidates already supplied by the user, and stop on a likely
duplicate until the user decides whether a separate subtask is intended. When
no candidate list exists, disclose `duplicate search: unavailable in this CLI`
before requesting confirmation.

## Plan, confirm, and apply

Run the command without `--confirm` first:

```text
NODE_USE_SYSTEM_CA=1 node <skill-dir>/scripts/jira-protocol.mjs create-subtask \
  --parent LEL-1234 --summary <summary> --description <description> \
  [--issue-type-id <id>] [--field <server-field-id>=<value>]...
```

For a long, quoted, Unicode, or multiline Summary or Description, write the
exact UTF-8 bytes to a file and pass `--summary-file` / `--description-file`
instead of the inline form. Inline and file forms of one field are mutually
exclusive; the CLI refuses both together and reads the file as UTF-8. The file
forms are the bridge-safe forms over the LAN share: stage the UTF-8 file to
the LAN share first, then pass the Lenovo-side share path through the existing
generic signed-queue mechanism (`docs/lenovo-hosted-scripts.md`; no
Jira-specific wrapper). The file is read as UTF-8 without re-encoding;
`createSubtask` trims outer whitespace from Summary and keeps Description
byte-exact. Do not rely on leading or trailing whitespace in Summary.

The CLI resolves the subtask issue type from live
`/rest/api/2/issue/createmeta` for project `LEL` and validates the parent by
direct issue readback before planning. When live metadata exposes exactly one
subtask type, that type is used. When it exposes more than one, the CLI stays
fail-closed: planning without an explicit `--issue-type-id` is rejected with
the live id list and no default is guessed. When supplied, `--issue-type-id`
must be a positive integer matching a live `subtask: true` entry; unknown,
non-subtask, or malformed ids are rejected before any write. `MissingRequiredFields` names live
required field ids and labels. Ask for or derive only supported values, then
plan again. Never guess custom-field ids or static defaults. Planning and
missing-field discovery perform no write. The confirmation digest binds the
parent key, parent identity, selected subtask type id, every submitted field, and the live
model; a changed default, parent, selection, or model invalidates it.

For `AwaitingConfirmation`, show the parent key, Summary, Description,
supplied fields, live required-field model, selected subtask type id
(`operation.issueTypeId` / `model.subtaskType`), duplicate-search
limitation, and `confirmation`. Only after the user confirms that live plan
may a later invocation repeat the identical inputs with
`--confirm <confirmation>` (including the same `--issue-type-id` when one was
used). A refreshed server model or default changes the
digest and requires a new review; do not reuse the old one.

## Readback and recovery

Report `Completed` only with the returned issue key and the CLI readback. Its
contract proves project `LEL`/`10848`, the selected live subtask issue type id
(`readback.issueTypeId` equals the requested `--issue-type-id`, or the single
live type when no selection was needed), exact
parent linkage (`readback.parentKey`), exact persisted Summary and Description,
and a matching persisted value/id/key/name for every submitted custom field
(`persistedFields`). Show the `persistedFields` receipt; do not call creation
complete if any requested field is absent or mismatched. `detail-view` may
inspect the bounded GreenHopper view, but it remains explicitly incomplete and
does not replace the key-specific creation readback.

`UnknownOutcome` means a write may have happened. The CLI has no search
command that can prove absence or recover a missing returned key, so preserve
the receipt and stop without another create. HTTP 401/403 and other policy
denials are terminal for this route; report them without changing clients,
credentials, or protocols.
