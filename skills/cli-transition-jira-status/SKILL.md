---
name: cli-transition-jira-status
description: Drive one explicitly named LEL Jira issue to an explicitly named target status id or unambiguous live status name through the task-runner Jira protocol CLI, executing only live-discovered safe steps. Do not use for single transitions with a chosen transition id, workflow forms, assignment, or issue creation.
---

# Drive an LEL Jira issue to a target status through the CLI

Use the installed `scripts/jira-protocol.mjs` relative to this `SKILL.md`.
Read `references/jira-protocol.md` and
`references/jira-protocol-handoff.md` before execution. This skill drives
one issue to one explicit target status id or unambiguous live status name
along live-discovered edges; it
never synthesizes a path from a static map. Run it on the host that holds
the authorized Jira session: locally when the session is local, or on
Lenovo through the generic signed queue when the session lives only there
(see `docs/lenovo-hosted-scripts.md`; same two-phase flow, no Jira-specific
wrapper). Launch every invocation with `NODE_USE_SYSTEM_CA=1` in the
environment (PowerShell: `$env:NODE_USE_SYSTEM_CA='1'`). TLS verification
stays enabled; add no error handling around it.

## Prepare

Require one explicit, unambiguous issue key (`LEL-N`) and one explicit
target: either a numeric status id (for example `10217`) or an unambiguous
live status name (for example `Done`). A name is resolved only against the
live status catalog (`GET /rest/api/2/status`) filtered to rapid view
`298290`: trimming, collapsing whitespace, and case-folding must yield
exactly one board status; otherwise the plan stops with no write and never
guesses. Status ids are live board
facts: confirm the target appears on rapid view `298290` (the read-only
plan reports the board check) and never guess an id from documentation
examples. The current status id is reported by the plan; board placement or
the action label alone is not proof of anything.

## Plan, confirm, and apply

Run the command without `--confirm` first:

```text
NODE_USE_SYSTEM_CA=1 node <skill-dir>/scripts/jira-protocol.mjs transition-to-status \
  --issue LEL-1234 --status 10217
```

A name may be passed in the same `--status` slot
(`--status Done`). The planner re-discovers the issue-specific transitions
and the live board
columns. Show the issue, current status id, target status id, first step
(transition name/id, target column name/id), the available transitions, and
`confirmation`. Only after the user confirms that exact plan may a later
invocation repeat identical inputs with `--confirm <confirmation>`.
That single user approval covers the named goal and the execution of the
necessary intermediate transitions: do not ask again on each step.

After confirmation the executor re-discovers before every step and verifies
each step by direct issue-by-key readback whose status id must advance as
planned. An already-reached target writes nothing. A stale plan (changed
transitions, columns, first step, current status, or resolved target) returns
a fresh `AwaitingConfirmation`
instead of executing.

## Readback and recovery

Report success only for `Completed` whose final issue-by-key readback
reports the target status id. `detail-view` is not a status reconciliation
command.

`UnknownOutcome` means executed steps may have happened but the outcome was
not proven. Preserve the receipt and stop; never repeat the mutation
blindly. Re-plan read-only from the current status before any new run; a
rerun resumes from wherever the issue actually stands. HTTP 401/403 is
terminal for this route; do not bypass it with another Jira client, raw
HTTP, or browser automation.

Unavailability (`status ... is not on board`, `no board status named`, `no transition is currently
available`), ambiguity (`ambiguous status name`, `ambiguous next step`, several direct transitions),
workflow-form-only paths, and suspected loops all stop with their reason
and no write. For an ambiguous direct choice, fall back to the single-step
`cli-transition-jira-issue` skill with an explicit transition id. For an
ambiguous name, ask the user to pick an explicit status id instead of
guessing.
