# Jira protocol subtasks

Subtask companion to `docs/jira-protocol.md`: session inputs, TLS, exit
states, and the `flag` flow live there. This note covers the subtask REST
create contract only.
## Create an LEL subtask under an explicit parent

Subtask creation uses the Jira Server 8.20 REST create contract with live
create metadata, not the Quick Create form. The CLI first reads
`GET /rest/api/2/issue/createmeta?projectKeys=LEL&expand=projects.issuetypes.fields`,
selects the project-`LEL` subtask issue type declared there, and reads
the parent by key (`project`, `issuetype`) to prove it exists in `LEL`/`10848`
and is not itself a subtask. A subtask cannot have a child subtask. When live
metadata exposes exactly one subtask type, that type is used. When it exposes
more than one (for example `10340` and `10101`), the CLI stays fail-closed:
plan without `--issue-type-id` is rejected with the live type list and no
default is guessed.

```powershell
$env:NODE_USE_SYSTEM_CA = '1'
node scripts/jira-protocol.mjs create-subtask `
  --parent LEL-4122 `
  --summary 'Fix validation edge case' `
  --description 'Context and acceptance notes'
```

If the live subtask metadata declares other required fields, the result is
`MissingRequiredFields` with safe field ids and labels. Supply them explicitly:

```powershell
$env:NODE_USE_SYSTEM_CA = '1'
node scripts/jira-protocol.mjs create-subtask `
  --parent LEL-4122 `
  --summary 'Fix validation edge case' `
  --field customfield_10481=SERVICE
```

When live metadata exposes multiple subtask types, pass the explicit selection:

```powershell
$env:NODE_USE_SYSTEM_CA = '1'
node scripts/jira-protocol.mjs create-subtask `
  --parent LEL-4122 `
  --issue-type-id 10101 `
  --summary 'Fix validation edge case' `
  --field customfield_10481=SERVICE
```

The id is validated live: it must be a positive integer and match a
`subtask: true` entry for project `LEL`. Unknown, non-subtask, or malformed
ids are rejected before any write. The selected id is part of the confirmation
digest (`operation.issueTypeId`, `submission.subtaskType`) and of the
`Completed` readback (`readback.issueTypeId`); changing it invalidates a prior
confirmation.

For a long, quoted, Unicode, or multiline Summary or Description, write the
exact UTF-8 bytes to a file and pass `--summary-file` / `--description-file`
instead of the inline form; the pairs are mutually exclusive and the file is
read as UTF-8. Summary is trimmed outer-whitespace-exact; Description is
byte-exact.

When all required fields are present, the result is `AwaitingConfirmation`.
Its digest binds the parent key and identity, the live subtask type, every
submitted field and resolved select value, and the safe live model. Repeat the
identical inputs with `--confirm '<confirmation>'`. The CLI refetches metadata
and the parent; any change produces a new digest and no write.

After a confirmed write, `Completed` requires all of the following readback by
key-specific `GET /rest/api/2/issue/{key}`:

- the returned key resolves to project `LEL` / `10848`;
- its issue type id equals the live subtask type id selected at plan time;
- its `parent.key` equals the requested parent key;
- every submitted field is returned with the submitted value. Summary and
  Description are exact strings; structured custom fields must expose the
  submitted value, id, key, or name unambiguously.

Fields absent from the live create metadata are rejected before writing. If any
readback invariant is absent or mismatched, the result is `UnknownOutcome`.
When the REST create returned a valid issue key, an `UnknownOutcome` receipt
retains that `issueKey`; invariant failures also report `verification`
(`isExpectedProject`, `isExpectedType`, `isExpectedParent`,
`fieldsPersisted`). These fields do not prove completion or authorize a retry.
HTTP failures during create report a fixed `stage` (`subtask_meta`,
`subtask_parent`, or `subtask_submission`) without exposing request contents.
Select-field labels are resolved against live `allowedValues`; ambiguous or
unknown labels are rejected before submission. The Epic flow is unchanged.
