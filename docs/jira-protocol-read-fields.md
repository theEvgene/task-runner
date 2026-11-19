# Compact selected Jira field reading

```powershell
$env:NODE_USE_SYSTEM_CA = '1'
node scripts/jira-protocol.mjs read-fields --issue LEL-4122 --fields summary,status,assignee
```

The read-only `read-fields` command returns only explicitly selected
standard fields (`summary`, `status`, `assignee`, `flagged`, `parent`,
`subtasks`) from one issue resource
(`GET /rest/api/2/issue/{key}?fields=...`). `flagged` reads
`customfield_10641` (`null` proves unflagged, a non-empty array proves
flagged). The command never pages comments and never extracts the parent
issue: the receipt carries `commentsFetched: false` always and
`parentFetched: true` only when `parent` was requested, with parent and
subtask entries kept to key plus caller-visible summary/status. Unknown
field names are rejected with the supported list.

Each requested field carries `state: value`, `empty`, or `unobserved`. A
missing REST field is `state: unobserved` with reason
`field_unobserved_permission_or_configuration`; an explicit `null`
assignee proves unassigned, an explicit `null` parent proves absence, and
only observed values are `state: value`.
`completeness.comprehensive` is false with `visibilityLimits` and `gaps`
whenever any requested field is unobserved. HTTP 401/403 is terminal like
`extract`; other transport failures keep their `httpStatus` in the error
receipt.

Optional `--max-bytes N` keeps the receipt queue-safe: exceeding it emits a
skeleton with field values dropped, `completeness.truncated: true`,
`comprehensive: false`, and a stderr warning instead of presenting a cut
payload as complete. Receipts are redacted at the output boundary and never
carry session material.

This command is not a substitute for `extract`: use it for
title/status/assignee/flag/parent/child checks, and `extract` when
comments, attachments, links, or the immediate parent extraction are
required. Exit `0` means complete, `6` means evidence with an incomplete
scope, `2` means invalid input or failure.
