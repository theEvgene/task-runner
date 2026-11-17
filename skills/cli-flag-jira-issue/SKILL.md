---
name: cli-flag-jira-issue
description: Add or remove the flag on one explicitly named LEL Jira issue with a required comment through the task-runner Jira protocol CLI. Use for flag add/remove only; do not use for other mutations or for reading issue content.
---

# Flag an LEL Jira issue through the CLI

Use the installed `scripts/jira-protocol.mjs` relative to this `SKILL.md` and
read `references/jira-protocol.md` before execution. Supply one unambiguous
`LEL-N` key, one action (`add` or `remove`), and one required comment. The CLI
has no flag-state discovery command: use a supplied or already verified
current state, never a guessed one.

## Plan, confirm, and apply

Run without confirmation first:

```text
node <skill-dir>/scripts/jira-protocol.mjs flag \
  --issue LEL-1234 --action add --comment "Reason"
```

For a long, quoted, Unicode, or multiline comment, write the exact text to a
UTF-8 file and pass `--comment-file <path>` instead of `--comment`. The two
options are mutually exclusive. `--comment-file` is also the bridge-safe form:
the file rides the LAN share while only its Lenovo-side path travels in the
signed command (see `docs/lenovo-hosted-scripts.md`).

The result must be `AwaitingConfirmation`. Show the issue key, action,
comment, and `confirmation`. Only after the user confirms that exact plan may
a later invocation repeat the identical inputs with
`--confirm <confirmation>`. A changed action or comment text is a new
operation and needs a new plan.

## Lenovo bridge route

When the authorized Jira session lives only on Lenovo, run the identical
command on Lenovo through the generic signed queue instead of locally. Follow
`docs/lenovo-hosted-scripts.md` (same two-phase flow, credentials stay in the
Lenovo host environment, same-id recovery). Do not invent a Jira-specific
wrapper; do not move credentials onto the queue.

## Readback and recovery

Report success only when the CLI returns `Completed` and its direct
issue-by-key readback proves both the requested flag state and a new comment
id (`readback.flag`, `readback.commentId`). The confirmed operation refuses
an already matching flag state, so a repeated `Completed` is never produced
by replaying a comment.

`UnknownOutcome` means the flag write and the comment write could not both be
confirmed. Preserve the receipt and stop: verify the flag state and the new
comment by reading the issue before any retry, and never repeat the mutation
blindly. The CLI exposes no safe mutation retry after uncertainty.
`detail-view` is a bounded GreenHopper UI model, not an authoritative flag
reconciliation command. HTTP 401/403 is terminal for this route; do not
switch to raw HTTP, `dp jira`, Nessy, browser automation, or another
credential path.
