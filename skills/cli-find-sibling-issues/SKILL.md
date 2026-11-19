---
name: cli-find-sibling-issues
description: Find same-parent LEL subtasks assigned to one exact Jira login through the task-runner REST protocol CLI, returning only key, summary, status, and link with an explicit completeness ledger. Use when an agent needs the sibling issues of one source issue for a single assignee without reading every child manually.
---

# Find sibling issues by assignee through the CLI

Use `scripts/jira-protocol.mjs` relative to this `SKILL.md`. Read
`references/jira-protocol.md` before execution, require one unambiguous
`LEL-N` source key and one exact Jira login, and run it on the host that
holds the authorized Jira session: locally when the session is local, or on
Lenovo through the generic signed queue when the session lives only there
(see `docs/lenovo-hosted-scripts.md`; same command, no Jira-specific
wrapper). Launch every invocation with `NODE_USE_SYSTEM_CA=1` in the
environment (PowerShell: `$env:NODE_USE_SYSTEM_CA='1'`). TLS verification
stays enabled; add no error handling around it. For the run itself:

```text
NODE_USE_SYSTEM_CA=1 node <skill-dir>/scripts/jira-protocol.mjs siblings --issue LEL-1234 --assignee example.user
```

## Read the receipt

The command resolves the immediate `fields.parent` of the source issue,
reads the parent's listed subtasks, and reads each listed child once by key
for its caller-visible assignee. It returns only the children assigned to
the exact login, each as key, summary, status, and browse link. The source
issue itself is never listed as its own sibling.

Check `completeness.comprehensive` and every entry in
`completeness.categories` (`parent`, `childList`, `childReads`). A complete
empty `matches` array proves no visible sibling carries that login; an
incomplete receipt never stands in for that proof. Unassigned children are
listed under `unassigned` by key and never match. Unreadable children are
listed under `unreadable` with their key and reason
(`not_found_or_not_visible`, `access_denied`, or `retrieval_unknown`) and do
not hide the readable rest. A missing `parent` or `subtasks` field is
unobserved due to permission or configuration, not an empty value. An
explicit `parent: null` proves the source has no parent and therefore no
siblings. A parent 404 is `not_found_or_not_visible`, not proof that no
parent exists.

Both supported assignee identities are honored: a child matches when the
requested login equals either its REST `assignee.name` or its legacy
`assignee.key`, compared exactly. There is no current-user lookup, no fuzzy
match, and no JQL: only the listed subtasks of the immediate parent are
examined, and only caller-visible children can match.

## Denial boundary

A 401/403 on the source read is terminal: report the denial without
switching to raw HTTP, `dp jira`, Nessy, browser automation, an Atlassian
connector, or another credential path. A 401/403 on a parent or child read
is reported as an incomplete receipt with reason `access_denied`, never as
an empty or a full result. Never present an incomplete receipt as proof that
no sibling exists, and never merge these short entries into a full
extraction: the sibling receipt carries key, summary, status, and link only,
without comments, attachments, or issue links.
