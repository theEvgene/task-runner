# Jira protocol batch subtasks

This note is the batch companion to `docs/jira-protocol.md`. Session inputs,
TLS rules, exit states, and the portable step ledger in
`docs/jira-protocol-handoff.md` still apply; this file only details
`create-subtasks`.

## Create a batch of subtasks from a file

`create-subtasks` creates a set of subtasks under one parent from a UTF-8
JSON file holding an array of `{ "summary", "description"?, "fields"? }`
entries (multiline and non-ASCII text travel as JSON strings). It reuses the
single-subtask operation, its confirmations, and its readback.

```powershell
$env:NODE_USE_SYSTEM_CA = '1'
node scripts/jira-protocol.mjs create-subtasks --parent LEL-4122 --file subtasks.json
```

The read-only plan shows every entry with its live match against the
parent's existing children (`new`, `exists`, `ambiguous`) and creates only
unmatched entries after confirmation. Matching normalizes summaries with
NFC plus collapsing every Unicode whitespace run to one space, then trimming;
matching stays case-sensitive, and normalized-equal entries need an explicit
repeatable `--allow-duplicate '<summary>'`. Identical file entries (the same
normalized summary twice in the batch file) stay `ambiguous` even when
allowed and are never written, so one batch cannot silently create twins. A
repeat run skips already-created entries, so an interrupted batch resumes
without duplicates. A confirmed write requires `--journal <path>`: a durable
in-flight marker is written atomically before each POST, every confirmed
per-item result replaces that marker before the next item starts, the journal
is bound to the batch identity (parent, file snapshot, requested type,
allow-duplicate set) plus the live resolved subtask type, and a rerun skips
journaled successes with no new write. A journaled in-flight or
`UnknownOutcome` entry blocks any rerun of that index until it is explicitly
resolved after operator verification with repeatable
`--resolve-unknown <index>=retry|<LEL-key>`; there is no blind repeat. Only
failures proven to precede dispatch clear the marker as `Failed`; any
uncertain failure keeps a blocking marker. Journal IO failures carry stage
`batch_journal`. HTTP 401/403 aborts the batch and preserves partial results
in the error details and in the journal. The result lists every entry with
its link and state. Single `create-subtask` is unchanged.

The portable step ledger, sanitized handoff evidence, transfer rules, and
fixture/live boundary for these flows are defined in
`docs/jira-protocol-handoff.md`.
