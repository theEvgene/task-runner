---
name: cli-read-selected-fields
description: Read explicitly selected LEL Jira fields through the task-runner REST protocol CLI with no comment pages and no parent extraction unless requested. Use when an agent needs only title, status, assignee, flag, parent, or child list without a full extract.
---

# Read selected Jira fields through the CLI

Use `scripts/jira-protocol.mjs` relative to this `SKILL.md`. Read
`references/jira-protocol.md` and `references/jira-protocol-read-fields.md`
before execution, require one unambiguous
`LEL-N` key and an explicit field list, and run it on the host that holds
the authorized Jira session: locally when the session is local, or on
Lenovo through the generic signed queue when the session lives only there
(see `docs/lenovo-hosted-scripts.md`; same command, no Jira-specific
wrapper). Launch every invocation with `NODE_USE_SYSTEM_CA=1` in the
environment (PowerShell: `$env:NODE_USE_SYSTEM_CA='1'`). TLS verification
stays enabled; add no error handling around it.
For the run itself:

```text
NODE_USE_SYSTEM_CA=1 node <skill-dir>/scripts/jira-protocol.mjs read-fields --issue LEL-1234 --fields summary,status,assignee
```

Supported names are `summary`, `status`, `assignee`, `flagged`, `parent`,
and `subtasks`. Unknown names are rejected with the supported list. The
command requests only the mapped REST fields
(`flagged` reads `customfield_10641`) from one issue resource, never pages
comments, and never extracts the parent issue. `commentsFetched` is always
false; `parentFetched` is true only when `parent` was requested. For a
queue-limited transport add `--max-bytes N`; exceeding it emits a truncated
skeleton with `completeness.truncated: true` instead of a cut payload
claimed as complete.

## Read the receipt

Check `requested`, `fields`, and `completeness.comprehensive`. Each
requested field carries `state: value`, `empty`, `unobserved`, or
`truncated`: `null` assignee proves unassigned and `null` parent proves
absence, while a missing REST field is `unobserved` with
`field_unobserved_permission_or_configuration` and makes the receipt
incomplete with that field in `visibilityLimits` and `gaps`. `flagged`
`null` proves unflagged (`false`); a non-empty array proves flagged
(`true`). Parent and subtask entries carry key plus caller-visible
summary/status only, without comments, attachments, or issue links.

If any category is incomplete, preserve the returned evidence, name every
entry in `completeness.gaps`, and avoid presenting the declared compact
scope as complete. HTTP 401/403 is terminal; report the denial without
switching to raw HTTP, `dp jira`, Nessy, browser automation, an Atlassian
connector, or another credential path. Never merge these short entries into
a full extraction: use `extract` when comments, attachments, links, or the
immediate parent extraction are required.
