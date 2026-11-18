---
name: cli-extract-jira-content
description: Extract one explicitly named LEL Jira issue and its standard immediate parent through the task-runner REST protocol CLI, including caller-visible fields, paged comments, attachment metadata, Jira issue links, and an explicit completeness ledger. Use for Jira issue reading when every unavailable or unproven content category must remain visible.
---

# Extract one LEL Jira issue through the CLI

Use `scripts/jira-protocol.mjs` relative to this `SKILL.md`. Read
`references/jira-protocol.md` before execution, require one unambiguous
`LEL-N` key, and run it on the host that holds the authorized Jira session:
locally when the session is local, or on Lenovo through the generic signed
queue when the session lives only there (see
`docs/lenovo-hosted-scripts.md`; same command, no Jira-specific wrapper).
Launch every invocation with `NODE_USE_SYSTEM_CA=1` in the environment
(PowerShell: `$env:NODE_USE_SYSTEM_CA='1'`). TLS verification stays enabled;
add no error handling around it.
For the run itself:

```text
NODE_USE_SYSTEM_CA=1 node <skill-dir>/scripts/jira-protocol.mjs extract --issue LEL-1234
```

For an unusually large issue, `--page-size N` changes the requested comment
page size and `--max-pages N` changes the fail-closed safety bound. The server
may cap page size; the extractor advances by returned rows and proves complete
pagination from every page's `startAt`, `total`, unique comment ids, and final
count.

## Read the receipt

Treat `dataContract.scope` as the exact guarantee. The command reads:

- every caller-visible field from the standard Jira issue resource;
- every dedicated REST comment within the declared safety bounds;
- visible attachment metadata and sanitized content/thumbnail URL inventory;
- visible Jira issue links without following them;
- `fields.parent` and, when observed, the immediate parent through the same
  extraction contract exactly once.

Check `completeness.comprehensive` and every entry in
`completeness.categories`. A complete category may still mean
caller-visible-only. A missing `attachment`, `issuelinks`, or `parent` field is
unobserved due to permission or configuration, not an empty value. An explicit
`parent: null` proves absence; a missing parent field is
`parent.presence: unobserved`. A parent 404 is `not_found_or_not_visible`, not
proof that no parent exists.

If any category is incomplete, preserve the returned evidence, name every
entry in `completeness.gaps`, and avoid presenting the declared REST scope as
complete. HTTP 401/403 is terminal; report the denial without switching to raw
HTTP, `dp jira`, Nessy, browser automation, an Atlassian connector, or another
credential path.

## Full-content boundary

`dataContract.fullContentProven` is always false. The extraction does not read
server-rendered GreenHopper sections, Wiki pages, remote-link endpoints,
attachment bodies, or attachment text. These are separate entries under
`completeness.extendedContent` with `unproven_not_retrieved` or
`metadata_only_not_retrieved`; never infer them from URLs or labels. Do not
download an attachment, follow a Jira issue link, contact Wiki, or describe
the receipt as a complete export of every related system.

The legacy `detail-view` command remains a separate bounded UI-model read and
is not a fallback for this extraction contract.
