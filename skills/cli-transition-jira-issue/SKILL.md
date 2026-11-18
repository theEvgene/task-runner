---
name: cli-transition-jira-issue
description: Apply one currently available workflow transition to an explicitly named LEL Jira issue through the task-runner Jira protocol CLI. Do not use for automatic multi-step status paths, workflow forms, assignment, or issue creation.
---

# Transition an LEL Jira issue through the CLI

Use the installed `scripts/jira-protocol.mjs` relative to this `SKILL.md` and
read `references/jira-protocol.md` before execution. This skill applies one
available transition; it does not synthesize a path to an arbitrary status.
Run it on the host that holds the authorized Jira session: locally when the
session is local, or on Lenovo through the generic signed queue when the
session lives only there (see `docs/lenovo-hosted-scripts.md`; same
two-phase flow, no Jira-specific wrapper). Launch every invocation with
`NODE_USE_SYSTEM_CA=1` in the environment (PowerShell:
`$env:NODE_USE_SYSTEM_CA='1'`). TLS verification stays enabled; add no error
handling around it.

## Discover and select

Discover the issue-specific transitions read-only:

```text
NODE_USE_SYSTEM_CA=1 node <skill-dir>/scripts/jira-protocol.mjs transitions --issue LEL-1234
```

Select only a transition actually returned for this issue. Keep the action
label, transition id, and target status id distinct. The CLI also requires the
current board column id whose `statusIds` contains that target status. Its
public command set does not expose board-column discovery, so use a column id
only when it is supplied by trusted current context. Stop and report the gap
when it is missing; never guess one, reuse the documentation example, probe ids,
or invent a discovery command.

Transitions with `hasTransitionView=true` require a workflow form and are
unsupported. Report that boundary rather than submitting an empty form.

## Plan, confirm, and apply

Run without confirmation first:

```text
NODE_USE_SYSTEM_CA=1 node <skill-dir>/scripts/jira-protocol.mjs transition \
  --issue LEL-1234 --transition-id <id> --target-column <column-id>
```

The planner re-discovers the transition and current board columns, then checks
that the target column contains the target status. Show the issue, transition
name/id, target status id, column name/id, and `confirmation`. Only after the
user confirms that exact plan may a later invocation repeat identical inputs
with `--confirm <confirmation>`. The digest binds both displayed names as well
as their ids, so a renamed transition or column requires a new confirmation.

## Readback and recovery

Report success only for `Completed` with a direct issue-by-key readback whose
status id equals the planned target status id. Board placement or the action
label alone is not proof.

`UnknownOutcome` means the transition may have happened but readback did not
prove it. Preserve the receipt and stop; the CLI has no standalone status
reconciliation command and `detail-view` is not full issue extraction. Never
repeat the mutation blindly. HTTP 401/403 is terminal for this route; do not
bypass it with another Jira client, raw HTTP, or browser automation.
