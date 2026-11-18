---
name: cli-create-jira-subtasks
description: Create a set of LEL subtasks under one explicit LEL parent from a UTF-8 JSON batch file through the task-runner Jira protocol CLI. Use for a requested subtask batch only; do not use for Epics, other projects, parent-less issues, comments, assignment, or status changes.
---

# Create a batch of LEL subtasks through the CLI

Use the installed `scripts/jira-protocol.mjs` relative to this `SKILL.md`.
Read `references/jira-protocol.md` and
`references/jira-protocol-handoff.md` before execution. The runtime is
installed with this skill; never substitute a similarly named global
command. Run it on the host that holds the authorized Jira session:
locally when the session is local, or on Lenovo through the generic signed
queue when the session lives only there (see
`docs/lenovo-hosted-scripts.md` in the pinned task-runner checkout; same
two-phase flow, no Jira-specific wrapper). Launch every invocation with
`NODE_USE_SYSTEM_CA=1` in the environment (PowerShell:
`$env:NODE_USE_SYSTEM_CA='1'`). TLS verification stays enabled; add no
error handling around it.

## Prepare the batch file

Write one UTF-8 JSON file holding an array of entries:

```json
[
  { "summary": "Fix validation edge case", "description": "Context\nand steps" },
  { "summary": "Добавить проверку", "fields": { "customfield_10481": "SERVICE" } }
]
```

Each entry needs a non-empty `summary`; `description` is optional multiline
text; `fields` is an optional object of server field ids to string values.
Require one explicit, unambiguous parent key (`LEL-N`); never invent or
guess a parent. The parent must already exist in project `LEL`/`10848` and
must not itself be a subtask. The protocol has no issue search or JQL
command: matching runs only against the live children of that parent, and
any duplicate candidate outside the parent is out of scope. Stop on a
likely duplicate outside the parent until the user decides.

For a Lenovo run, stage the exact UTF-8 bytes to the LAN share first, then
pass the Lenovo-side share path as `--file` through the existing generic
signed-queue mechanism (`docs/lenovo-hosted-scripts.md`; no Jira-specific
wrapper). The file is read as UTF-8 without re-encoding; `summary` is
trimmed of outer whitespace and `description` is kept byte-exact.

## Plan, confirm, and apply

Run the command without `--confirm` first:

```text
NODE_USE_SYSTEM_CA=1 node <skill-dir>/scripts/jira-protocol.mjs create-subtasks \
  --parent LEL-1234 --file <path-to-subtasks.json> \
  [--issue-type-id <id>] [--allow-duplicate '<summary>']...
```

The plan shows every entry with its live match: `new` (will be created),
`exists` (exactly one same-summary child; skipped, never rewritten), or
`ambiguous` (several matching children, or several identical file entries).
When live metadata exposes more than one subtask type, pass the explicit
`--issue-type-id`; otherwise the single live type is used.
`MissingRequiredFields` names live required field ids and labels; fix the
file, then plan again. The confirmation digest binds the parent key, the
selected subtask type id, every entry, the live match list, and the
`--allow-duplicate` set; any change requires a new review. The receipt
carries `operation.parentKey`, the per-entry matches, `results[].key`, and
`results[].link`.

For `AwaitingConfirmation`, show the parent key, the entry list with
matches, the selected subtask type id, and `confirmation`. Only after the
user confirms that live plan may a later invocation repeat the identical
inputs with `--confirm <confirmation>`. An ambiguous match is never
created without its exact summary passed as a repeatable
`--allow-duplicate`; that decision is bound into the digest.

## Readback and recovery

Report `Completed` only with the per-entry result list: each entry carries
its link and state (`Created` with the returned key and exact parent
linkage, or `AlreadyExists`). Every created entry proves project
`LEL`/`10848`, the selected live subtask type id, exact parent linkage,
and persisted fields, exactly like a single `create-subtask`. A repeat run
after success or interruption skips `AlreadyExists` entries and creates
only the missing ones, so a batch resumes without a second confirmed
instance. `detail-view` may inspect the bounded GreenHopper view, but it
remains explicitly incomplete and does not replace the key-specific
creation readback.

`UnknownOutcome` stops only the affected entry: its key (when returned) and
verification are recorded, no automatic retry follows, and the remaining
entries still complete. Preserve the receipt and investigate that entry
from its current state before any new run. HTTP 401/403 aborts the batch
and is terminal for this route; report it without changing clients,
credentials, or protocols.
