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
```

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

## Read GreenHopper detail-view data

```powershell
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
node scripts/jira-protocol.mjs transitions --issue LEL-4122
```

Choose an available transition id and the current board column whose
`statusIds` contain its `targetStatus`. The first invocation is read-only:

```powershell
node scripts/jira-protocol.mjs transition `
  --issue LEL-4122 `
  --transition-id 41 `
  --target-column 5934577
```

Review the receipt and repeat with its `confirmation` value:

```powershell
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

## Assign

The first invocation is read-only and produces a confirmation digest:

```powershell
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
node scripts/jira-protocol.mjs create-epic `
  --summary 'Improve service resilience' `
  --description 'Context and acceptance notes'
```

If the current server model has other required fields, the result is
`MissingRequiredFields` with safe field ids and labels. Supply them explicitly:

```powershell
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
- `/xboard/issue/matchesBoard` reports the key in `issuesInBacklog` for rapid
  view `298290`;
- every submitted field is returned by key-specific Jira issue readback with
  the submitted value. Summary and Description are exact strings; structured
  custom fields must expose the submitted value, id, key, or name unambiguously.

Fields absent from the live Quick Create model are rejected before writing. If
any readback invariant is absent or mismatched, the result is `UnknownOutcome`.
The CLI does not equate Jira's initial workflow state with backlog placement
and does not invent a separate rank write.

## Exit states

| Exit | Meaning                                                                |
| ---: | ---------------------------------------------------------------------- |
|  `0` | read succeeded or a write was confirmed by readback                    |
|  `2` | invalid input, unsupported response, or policy/HTTP failure            |
|  `3` | exact confirmation is required                                         |
|  `4` | Quick Create requires more server-declared fields                      |
|  `5` | a write may have happened but readback did not prove the outcome       |
|  `6` | extraction returned evidence but its declared REST scope is incomplete |

The CLI performs no live write unless an exact live-model-bound confirmation
digest is supplied. Tests use injected transports and local data only.
