# Jira protocol CLI

`scripts/jira-protocol.mjs` is a JSON-only command-line client for the Jira
protocol evidenced by the two sanitized 2026-11-14 captures supplied with the
implementation task. It is intentionally limited to the LEL project and the
Kanban board represented by those captures.

## Fixed observed context

| Item                        |                          Observed value |
| --------------------------- | --------------------------------------: |
| Jira                        | `https://jira3.tcsbank.ru`, Jira 8.20.7 |
| LEL project                 |                   key `LEL`, id `10848` |
| LEL Epic issue type         |                                 `10000` |
| LEL Service Team Kanban IVK |                     rapid view `298290` |

The capture directly observed board data and project workflow discovery. The
downloaded Jira client code established the details, per-issue transition,
`transitionAndRank`, Quick Create, and assignment-action shapes. No business
write was performed while producing the captures. For that reason every CLI
write has a read-only planning invocation, an exact confirmation digest, and a
post-write readback. A failed readback or unreadable 2xx mutation response is
`UnknownOutcome`, never permission to retry automatically. Archive client code
also establishes issue-key-specific
`GET /rest/api/2/issue/{key}?fields=...` and
`/xboard/issue/matchesBoard`. The `extract` command additionally uses the Jira
Server 8.20 REST issue and comment collection contracts. Those exact full-read
requests were not captured against this instance, so receipts preserve that
provenance and keep server-rendered UI, Wiki, remote links, and attachment
bodies outside the claimed scope.

## Session inputs

The CLI accepts no credential flags and writes no credential files. Supply an
already authorized Jira browser-compatible session through the environment:

```powershell
$env:JIRA_SESSION_COOKIE = '<Cookie header value>'
$env:JIRA_ATL_TOKEN = '<atl_token from the same session>' # assignment only
$env:NODE_USE_SYSTEM_CA = '1'
```

Launch every Node invocation with `NODE_USE_SYSTEM_CA=1` in the environment
(POSIX shells: prefix `NODE_USE_SYSTEM_CA=1 node ...`) so the corporate chain
resolves from the host system store. TLS verification stays enabled:
`NODE_TLS_REJECT_UNAUTHORIZED` is never set to `0`, `rejectUnauthorized` is
never disabled, and no error handling is added around it.

### Lenovo owner-provided cookie file (local process only)

On Lenovo the owner provides the session cookie as a file rather than typed
text: shortcut `Documents\performance\task-runner\jira-cookie*.lnk` points at
`%LOCALAPPDATA%\LanTaskRunnerData\private\jira-cookie.txt`. Read it only on the
host that already holds it (a Lenovo-local run, or the Lenovo worker process
for a signed-queue run). Keep its bytes out of command files, plans, receipts,
queue payloads, logs, and the repository, and never print them.

Discover both ends without a username and without launching the shortcut:

```powershell
$profile = [Environment]::GetFolderPath('UserProfile')
$linkDir = Join-Path $profile 'Documents\performance\task-runner'
$localAppData = [Environment]::GetFolderPath('LocalApplicationData')
$expected = Join-Path $localAppData 'LanTaskRunnerData\private\jira-cookie.txt'
```

List `$linkDir\jira-cookie*.lnk`, read each link target through
`WScript.Shell` `CreateShortcut(...).TargetPath` (read-only metadata; do not
invoke or double-click the link, and do not open Nessy, a browser, or any UI),
and proceed only when the resolved target equals `$expected` and that file
exists. With several matches prefer the one targeting `$expected`; with no
match there, stop and report cookie discovery as unverified.

Load the file into the current process only, keeping a configured session
intact:

```powershell
if ([string]::IsNullOrEmpty($env:JIRA_SESSION_COOKIE)) {
  $env:JIRA_SESSION_COOKIE =
    (Get-Content -Raw -Encoding utf8 $expected).Trim()
}
$env:NODE_USE_SYSTEM_CA = '1'
node scripts/jira-protocol.mjs extract --issue LEL-4122
```

A non-empty `JIRA_SESSION_COOKIE` wins; the file is a fallback, never an
override. `JIRA_ATL_TOKEN` still comes from the same-session environment, not
from this file. TLS verification stays enabled as above.

Production is pinned to `https://jira3.tcsbank.ru`. `JIRA_BASE_URL` is accepted
only when `JIRA_PROTOCOL_TEST_MODE=1`, and even then only an HTTP(S) loopback
origin (`localhost`, `127.0.0.1`, or `::1`) is permitted. This keeps a real
session cookie from being routed to an arbitrary or cleartext remote origin.
HTTP 401 and 403 are final policy denials; the client does not switch protocols
or retry them.

Receipts never include the cookie, `atl_token`, Quick Create `formToken`, raw
request headers, or the Quick Create field HTML that can contain hidden state.

## Extract REST-visible issue content

```powershell
$env:NODE_USE_SYSTEM_CA = '1'
node scripts/jira-protocol.mjs extract --issue LEL-4122
```

The read-only extractor requests `fields=*all&expand=names,schema` from the
standard issue resource and pages the dedicated comments collection with
`startAt` and `maxResults`. It validates the requested LEL key, an observed
issue `self` URL against the configured Jira origin and issue identity, page
metadata, unique numeric comment ids, stable totals, and a final unchanged
issue `updated` timestamp. It advances by rows actually returned, so a server
page-size cap is supported. Defaults are 50 requested comments per page, 100
pages, and 10,000 comments. `--page-size` and `--max-pages` can change the
first two bounds.

The result uses `jira-protocol-result/2` and contains:

- the caller-visible Jira issue resource and dedicated comments;
- attachment metadata plus sanitized URL classifications, without downloads;
- inward/outward Jira issue-link inventory, without linked-issue traversal;
- `fields.parent` presence and one extraction of the standard immediate parent;
- per-category completeness, pagination evidence, provenance, and gaps.

Missing `attachment`, `issuelinks`, or `parent` fields are unobserved, never
silently converted to empty values. Explicit `parent: null` proves absence. A
parent 404 means not found or not visible; HTTP 401/403 remains terminal. A
duplicate comment id, malformed page, changing total, empty page before total,
concurrent issue update, or safety-bound exhaustion returns
`comprehensive: false` with the precise gap.

`comprehensive` applies only to the declared REST-visible metadata scope.
`fullContentProven` remains false: server-rendered GreenHopper sections, Wiki
pages, remote-link endpoints, attachment bodies, and attachment text are
`unproven_not_retrieved` or `metadata_only_not_retrieved`. The extractor does
not contact those systems or URLs.

Compact selected-field reads are detailed in `docs/jira-protocol-read-fields.md`.

## Find sibling issues by assignee

```powershell
$env:NODE_USE_SYSTEM_CA = '1'
node scripts/jira-protocol.mjs siblings --issue LEL-4122 --assignee example.user
```

The read-only search resolves the immediate `fields.parent` of the source
issue, reads the parent's listed subtasks, and reads each listed child once
by key for its caller-visible assignee. It returns only the children
assigned to the exact login, each as key, summary, status, and
`{origin}/browse/{key}` link. The source issue itself is never listed as its
own sibling. A child matches when the login equals its REST `assignee.name`
or legacy `assignee.key` exactly.

`completeness.comprehensive` is true only when the parent, the child list,
and every child read are complete. Only an explicit `assignee: null` proves
a child is unassigned and lists it under `unassigned`, never matched; a
missing `assignee` field (`assignee_unobserved`), a non-object assignee
(`malformed_assignee`), an object without a usable `name`/`key`
(`assignee_identity_unobserved`), or a matching child without a usable
summary/status (`incomplete_child_content`) lists the child under
`unreadable` with its reason and makes the receipt incomplete. Unreadable
children do not hide the readable rest. A missing
`parent` or `subtasks` field is unobserved, never an empty value; explicit
`parent: null` proves there are no siblings. A parent 404 is
`not_found_or_not_visible`. A 401/403 on the source read is terminal; on a
parent or child read it is an incomplete receipt with reason
`access_denied`, never an empty or full result. The short entries carry no
comments, attachments, or issue links and are not a substitute for
`extract`.

## Read GreenHopper detail-view data

```powershell
$env:NODE_USE_SYSTEM_CA = '1'
node scripts/jira-protocol.mjs detail-view --issue LEL-4122
```

This returns the complete response body of this one observed UI endpoint:

```text
GET /rest/greenhopper/1.0/xboard/issue/details.json
    ?rapidViewId=298290&issueIdOrKey=LEL-4122&loadSubtasks=true
```

It is a GreenHopper hybrid model containing JSON metadata and server-rendered
HTML sections. Session-bearing `atl_token` and `formToken` values are redacted
at the output boundary independent of HTML attribute order, quoting, or case;
other detail-view data is preserved.

This command is deliberately not named `get` and does not claim comprehensive
Jira issue data. It remains available for the observed hybrid UI model and is
not a fallback for `extract`. Its response must not be merged into the REST
receipt as if it proved comments, attachments, links, or related-system data.

## Discover and apply a transition

Discover transitions for an issue, subtask, or Epic:

```powershell
$env:NODE_USE_SYSTEM_CA = '1'
node scripts/jira-protocol.mjs transitions --issue LEL-4122
```

Choose an available transition id and the current board column whose
`statusIds` contain its `targetStatus`. The first invocation is read-only:

```powershell
$env:NODE_USE_SYSTEM_CA = '1'
node scripts/jira-protocol.mjs transition `
  --issue LEL-4122 `
  --transition-id 41 `
  --target-column 5934577
```

Review the receipt and repeat with its `confirmation` value:

```powershell
$env:NODE_USE_SYSTEM_CA = '1'
node scripts/jira-protocol.mjs transition `
  --issue LEL-4122 `
  --transition-id 41 `
  --target-column 5934577 `
  --confirm '<confirmation>'
```

Before writing, the client re-discovers the issue-specific transition and live
board columns. The confirmation digest includes the displayed transition name
and id and the displayed column name and id, so a semantic rename invalidates
an earlier confirmation. It refuses transitions with `hasTransitionView=true`: those use
a workflow form that can require additional fields, and bypassing that form
with an empty transition would be unsafe. After the write it reads the target
issue directly by key through `/rest/api/2/issue/{key}` and confirms the status
id. This readback does not depend on the issue being present in top-level
`allData.issuesData.issues`, so the same screenless protocol works for tasks,
subtasks, and Epics. The board is still authoritative for validating that the
chosen target column currently contains the transition target status.

## Drive an issue to a target status

`transition-to-status` plans and executes a live-discovered route to an
explicit target status id or unambiguous live status name. It consults no static workflow map: every edge
comes from the issue-specific transitions actually returned for the current
state, and every column check comes from the live board. A name is resolved
read-only against `GET /rest/api/2/status` filtered to rapid view `298290`
(trim, collapse whitespace, case-fold; exactly one match required).

```powershell
$env:NODE_USE_SYSTEM_CA = '1'
node scripts/jira-protocol.mjs transition-to-status --issue LEL-4122 --status 10217
```

The same `--status` slot accepts a name (`--status Done`). Review the plan
(current and target status, first step, available
transitions) and repeat with its `confirmation` value. One user approval
covers the named goal and the execution of the necessary intermediate
transitions; after confirmation the
executor re-discovers before every step, verifies each step by issue-by-key
readback, and stops without an automatic retry on any unknown outcome or
ambiguity: an already-reached status writes nothing, an unknown or ambiguous
name stops with its reason, an unavailable or
ambiguous next step fails with its reason, and a workflow-form-only path is
refused. Single-step `transition` is unchanged.

## Assign

The first invocation is read-only and produces a confirmation digest:

```powershell
$env:NODE_USE_SYSTEM_CA = '1'
node scripts/jira-protocol.mjs assign `
  --issue LEL-4122 `
  --assignee example.user
```

After review, repeat with `--confirm`. The implementation uses the captured
Jira action `/secure/AssignIssue.jspa` with the numeric issue id, assignee login,
and session `atl_token`, then reads the target issue directly by key and
verifies `fields.assignee.name` (or the legacy key). The verification is not
limited to issues present in top-level board data. If the assignee cannot be
confirmed, the result is `UnknownOutcome`.

## Create an LEL backlog Epic

Quick Create fields are project-, type-, permission-, and configuration-
dependent. The capture proves that the client first obtains a server model;
it does not prove a universal static create payload. This CLI follows that
model rather than hardcoding guessed custom fields.

```powershell
$env:NODE_USE_SYSTEM_CA = '1'
node scripts/jira-protocol.mjs create-epic `
  --summary 'Improve service resilience' `
  --description 'Context and acceptance notes'
```

If the current server model has other required fields, the result is
`MissingRequiredFields` with safe field ids and labels. Supply them explicitly:

```powershell
$env:NODE_USE_SYSTEM_CA = '1'
node scripts/jira-protocol.mjs create-epic `
  --summary 'Improve service resilience' `
  --field customfield_10481=SERVICE
```

When all required fields are present, the result is `AwaitingConfirmation`.
Its digest binds the operation plus every non-token submitted field/default and
the safe live model metadata. Repeat the identical inputs with
`--confirm '<confirmation>'`. The CLI refetches the model; a changed default or
field model produces a new digest and no write. Refreshed CSRF/form-token values
are intentionally excluded from the digest and never printed.

After a confirmed write, `Completed` requires all of the following readback:

- the returned key resolves to project `LEL` / `10848`;
- its issue type is `Epic` / `10000`;
- rapid view `298290` reports the key either in `issuesInBacklog` or in its
  board data with a status mapped to one of its columns;
- every submitted field is returned by key-specific Jira issue readback with
  the submitted value. Summary and Description are exact strings; structured
  custom fields must expose the submitted value, id, key, or name unambiguously.

Fields absent from the live Quick Create model are rejected before writing. If
any readback invariant is absent or mismatched, the result is `UnknownOutcome`.
When Quick Create returned a valid issue key, an `UnknownOutcome` receipt retains
that `issueKey` for readback; invariant failures also report placement and field
`verification` results. These fields do not prove completion or authorize a
retry. HTTP failures during create report a fixed `stage` for model retrieval
or submission without exposing request contents. Select-field labels supplied
by the caller are resolved against the current model's options; ambiguous or
unknown labels are rejected before submission.
The readback reports `inBacklog` and `onBoard` separately. `onBoard: null` means
board data was not queried because backlog membership already proved placement.
The CLI does not equate an initial workflow state with placement and does not
invent a separate rank write.

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

## Create a batch of subtasks from a file

Batch creation is detailed in `docs/jira-protocol-batch.md`: `create-subtasks`
plans every entry against the parent's live children (`new`, `exists`,
`ambiguous`), writes only unmatched entries after confirmation, and requires
`--journal <path>` for a confirmed write with per-item in-flight markers,
resume, and explicit `--resolve-unknown` recovery. See that file for the
matching, journal binding, and blocking rules.

The portable step ledger, sanitized handoff evidence, transfer rules, and
fixture/live boundary for these flows are defined in
`docs/jira-protocol-handoff.md`.

## Exit states

| Exit | Meaning                                                                             |
| ---: | ----------------------------------------------------------------------------------- |
|  `0` | read succeeded or a write was confirmed by readback                                 |
|  `2` | invalid input, unsupported response, or policy/HTTP failure                         |
|  `3` | exact confirmation is required                                                      |
|  `4` | Quick Create or subtask create metadata requires more server-declared fields        |
|  `5` | a write may have happened but readback did not prove the outcome                    |
|  `6` | extraction or sibling search returned evidence but its declared scope is incomplete |

The CLI performs no live write unless an exact live-model-bound confirmation
digest is supplied. Tests use injected transports and local data only.

### Flag with a required comment

Run `$env:NODE_USE_SYSTEM_CA='1'; node scripts/jira-protocol.mjs flag --issue LEL-1234 --action add --comment "Reason"` (use `remove` to clear). For a long, quoted, Unicode, or multiline comment, write the exact UTF-8 text to a file and pass `--comment-file <path>` instead; `--comment` and `--comment-file` are mutually exclusive. Repeat with the returned `--confirm` digest to submit. The confirmed operation refuses an already matching flag state to avoid replaying a comment. After a write, both the flag and a new comment ID must be observed; an unknown outcome requires manual readback before retry. When the flag write returns a 2xx with an empty or non-JSON body, the CLI performs exactly one safe flag-only readback (no second write) and reports `Completed` only when the flag and the new comment are both proven; otherwise it stays `UnknownOutcome`. `create-epic` and `create-subtask` offer the same pair as `--summary-file` / `--description-file`. The GreenHopper route and payload derive from browser evidence; the subtask REST createmeta/create/readback route follows the Jira Server 8.20 REST contract and the live CLI route and authentication remain unverified. The server-side cause of an unreadable flag write body is not evidenced by a live capture; only the client-side unreadable-2xx branch is fixture-proven. No live write was performed.
