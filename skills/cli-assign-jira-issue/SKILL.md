---
name: cli-assign-jira-issue
description: Assign one explicitly named LEL Jira issue to an exact Jira login through the task-runner Jira protocol CLI. Do not use for status changes, issue creation, or resolving an unknown current-user login.
---

# Assign an LEL Jira issue through the CLI

Use the installed `scripts/jira-protocol.mjs` relative to this `SKILL.md` and
read `references/jira-protocol.md` before execution. Supply an exact `LEL-N`
key and exact Jira login. Run it on the host that holds the authorized Jira
session: locally when the session is local, or on Lenovo through the generic
signed queue when the session lives only there (see
`docs/lenovo-hosted-scripts.md`; same two-phase flow, no Jira-specific
wrapper). The CLI has no current-user lookup: “assign to me” is
incomplete until the login is known from user-provided or already verified
context. Never infer it from an email, display name, or operating-system user.

## Plan, confirm, and apply

Place the existing Jira session cookie in `JIRA_SESSION_COOKIE` and the
same-session `atl_token` in `JIRA_ATL_TOKEN`; keep both out of arguments,
prompts, files, receipts, and logs. Launch every invocation with
`NODE_USE_SYSTEM_CA=1` in the environment (PowerShell:
`$env:NODE_USE_SYSTEM_CA='1'`). TLS verification stays enabled; add no error
handling around it. Run without confirmation first:

```text
NODE_USE_SYSTEM_CA=1 node <skill-dir>/scripts/jira-protocol.mjs assign \
  --issue LEL-1234 --assignee exact.login
```

The result must be `AwaitingConfirmation`. Show the issue key, assignee login,
and `confirmation`. Only after the user confirms that exact plan may a later
invocation repeat the same inputs with `--confirm <confirmation>`. A changed
key or login is a new operation and needs a new plan.

## Readback and recovery

Report success only when the CLI returns `Completed` and its direct
issue-by-key readback reports the exact assignee login. A response from
`AssignIssue.jspa` alone is not success.

`UnknownOutcome` means the write may have happened but the authoritative
readback did not prove it. Preserve the receipt and stop: `detail-view` is a
bounded GreenHopper UI model, not an authoritative assignment reconciliation
command, and the CLI exposes no safe mutation retry after uncertainty. HTTP
401/403 is a terminal denial; do not switch to raw HTTP, `dp jira`, Nessy,
browser automation, or another credential path.
