# Jira protocol handoff contract

This document is the portable handoff contract for the Jira CLI protocol in
`scripts/jira-protocol.mjs` and `scripts/jira_protocol/`. It lets an agent
that never saw the original captures execute one authorized scenario on
another machine and tell apart, for every step, what was **observed** in the
application, what was **inferred** from code or contracts, and what is still
**assumed**. Unknowns stay unknown: a gap is recorded, never filled in.

The chosen scenario below is single subtask creation (`create-subtask`),
because it exercises the full write path the CLI actually proves: live
model, confirmation, submission, and key-specific readback. The same
categories and evidence rules apply to the follow-up commands
(`transition-to-status`, `create-subtasks`); their command references live
in `docs/jira-protocol.md`.

## Step ledger for `create-subtask`

| # | Step | Stage | Category | Source | Verification | Gaps |
| - | ---- | ----- | -------- | ------ | ------------ | ---- |
| 1 | Read live create metadata | `subtask_meta` | inferred | Jira Server 8.20 REST `createmeta` contract; the exact request was not in the captures | shape matches `projects[].issuetypes[]` with a live `subtask: true` type | live shape may differ; any mismatch fails closed before planning |
| 2 | Read parent by key | `subtask_parent` | inferred | Jira Server 8.20 REST issue contract (`fields=project,issuetype`); same precedent as `extract` full-reads | key match, project `LEL`/`10848`, non-subtask type id | failures here prove nothing about the parent; HTTP 401/403 is terminal |
| 3 | Build plan and confirmation digest | local | inferred | `createSubtask` in `scripts/jira_protocol/operations.mjs` (deterministic digest over operation plus live model) | digest equality on the confirmed rerun; any live change yields a new digest and no write | digest proves agreement on inputs, not server state |
| 4 | Submit the REST create | `subtask_submission` | inferred | Jira Server 8.20 REST create contract; live route and authentication remain unverified | none at this point: a returned key is not proof | success responses do not prove persistence; an unreadable 2xx body is `UnknownOutcome` |
| 5 | Key-specific readback | readback | inferred | Jira Server 8.20 REST issue contract | project, issue type id, `parent.key`, every submitted field persisted | any mismatch or read error is `UnknownOutcome` with the key retained; never a retry |
| 6 | Existing-children listing (batch only) | `batch_existing` | inferred | REST field expansion `fields=subtasks` on the parent; fixture-defined shape | every entry has an `LEL-N` key and a string summary | absent or malformed list fails the batch closed: matches stay unverified |

Assumed (not relied upon anywhere): the GreenHopper detail-view hybrid
model as a source of REST facts; any static workflow map as a source of
truth for routes; backlog membership as proof of placement; the cause of
the early uncaptured 403 that carried no stage. The 403 lesson is kept as a
negative rule: without a stage, the failing step is unknown, and no later
fix may be cited as its proven cause.

## Handoff evidence format

`toHandoffEvidence` in `scripts/jira_protocol/handoff.mjs` builds one
sanitized record per executed step:

```json
{
  "schema": "jira-handoff-evidence/1",
  "stage": "subtask_submission",
  "method": "POST",
  "path": "/rest/api/2/issue",
  "httpStatus": "unknown"
}
```

Rules:

- `stage` is a fixed token (`subtask_meta`, `subtask_parent`,
  `subtask_submission`, `subtask_readback`, `batch_existing`,
  `batch_item_submission`, `batch_item_readback`, `route_state`,
  `route_detail`, `route_transitions`, `route_board`, `route_step`,
  `route_step_readback`). The stage of a refusal names the step that
  refused, never a guessed cause.
- `method` is `GET`, `POST`, or `PUT`. The `path` never carries a query
  string, fragment, credential, header, or body; paths carrying `=`, `&`,
  `;`, `%`, or secret-like segments are rejected.
- `httpStatus` is the observed status or `"unknown"`. Transports that expose
  `response.status` (the real CLI transport does; minimal fixtures may not)
  emit the observed status; unknowns are not estimated from surrounding
  steps. Single-subtask `Completed` receipts carry four entries
  (meta, parent, submission, separate readback); route plans carry the
  observed discovery reads; batch steps carry per-item submission plus
  separate readback.
- `assertHandoffClean` rejects secret-bearing keys unless redacted and
  rejects any extra key on evidence objects, so request/response bodies
  cannot ride along. Secret matching is substring-based, so prefixed names
  (`my_cookie`, `X-Auth-Token`, `session_id`) are caught. Receipts are
  additionally serialized through `redactSecrets`.

## Moving the protocol between machines

- Install the skill suite from the same task-runner revision on the target
  host (`docs/jira-agent-skills.md`); commands resolve relative to the
  loaded `SKILL.md`, never to the caller's checkout.
- Carry only plans, digests, and file paths across the boundary (signed
  queue or LAN share). Session material (`JIRA_SESSION_COOKIE`,
  `JIRA_ATL_TOKEN`) lives only in the host environment that already holds
  it and is never printed, queued, or committed.
- Multiline or non-ASCII free text travels as UTF-8 files
  (`--summary-file`, `--description-file`, `--comment-file`, batch
  `--file`); inline and file forms of one field stay mutually exclusive.
- A confirmation digest binds the exact inputs plus the live model. After
  any move, re-plan first: a changed model yields a new digest, and the old
  one authorizes nothing.

## Safe diagnosis conditions

1. Read-only first: every write has a planning invocation that performs no
   mutation.
2. Confirm bound to live state: repeat identical inputs with the exact
   `--confirm` digest; anything else is a new plan.
3. Before each next step, re-discover: current status, available
   transitions, live metadata, existing children. Already-reached targets
   cause no further writes.
4. `UnknownOutcome` means the write may have happened. Preserve the
   receipt, re-plan read-only from current state, and never repeat the
   mutation blindly.
5. HTTP 401/403 is a terminal policy denial for the route. Do not switch
   clients, credentials, or protocols around it.
6. Refusals (unsupported shape, unavailable target, ambiguous match,
   workflow form) stop with the reason. Probing ids, guessing columns, or
   inventing discovery commands is out of scope.

## What counts as readback proof

- Transition: direct issue-by-key read whose `status.id` equals the
  planned target status id. Board placement or the action label is not
  proof.
- Subtask: project `LEL`/`10848`, the live subtask type id, exact
  `parent.key`, and every submitted field persisted. Epic: same project
  and type plus board or backlog placement.
- Plan-then-write-then-readback is the only success path; fixtures prove
  it with injected transports (see below), never with a live write.

## Fixture versus live

- Fixtures use injected transports and local data only and must cover the
  plan → write → readback path, the error/ambiguous path (no success
  claimed, exactly one submission attempt), the absence of secrets in
  receipts, and the preserved 401/403 plus `UnknownOutcome` rules.
- A live run needs separate per-action authorization on top of session and
  queue ownership, and its result is reported only as the observed receipt
  (`Completed` with readback, or the exact failure state). Live evidence
  never replaces the regression fixtures, and an unperformed live check is
  reported as `not verified` with its reason.
