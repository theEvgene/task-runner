# DCO feature-flag protocol CLI

`scripts/dco-feature-flags.mjs` plans, preflights, creates, and reads back
paired boolean toggles without browser automation. It emits JSON only and
never accepts a credential as a command-line argument.

## Skill installation

The distinct model-invoked skill `create-dco-feature-flags-protocol` does not
replace the existing browser-backed `create-dco-feature-flags` skill. Install
the self-contained directory atomically into an agent skill root:

```powershell
node scripts/install-dco-feature-flags-protocol-skill.mjs `
  --target-root $env:CODEX_HOME\skills
```

The installer refuses an existing destination. The installed skill resolves
its CLI relative to its own `SKILL.md`, independent of the caller's working
directory.

## Observed boundary

The sanitized 2026-11-15 L Loans stage exchange directly observed
`createToggle`, the normalized create response, exact-path `getEntities`, and
`getToggle`. The created ID agreed across all responses. The request used a
dotted parent plus slash key (`smeinterface.l_loans/<key>`); responses and
readback used the slash path (`smeinterface/l_loans/<key>`).

That exact L Loans stage mapping is marked `observed-create-readback`.
Production and Finance Front use the same DCO service contract as an explicit
`inferred-from-stage-contract`; their origins and parent IDs come from the
existing operational skill rather than observed production traffic. The CLI
uses only these methods,

```text
getEntity
getToggleTypes
getEntities
createToggle
getToggle
```

and exposes those evidence labels in every plan and receipt.

The CLI does not ingest captures, cryptographic evidence, reviewer keys, or
trust material at runtime. The implementation is fixed and tested from the
sanitized observation; credentials remain process environment variables only.

## Lenovo bridge route

When the bearer tokens live only on Lenovo, run the identical `plan` /
`apply` commands there through the generic signed queue as
`docs/lenovo-hosted-scripts.md` states. `plan --description-file` carries
free text over the LAN share; `--plan` and `--state` are Lenovo-local paths
on the same host; the confirmation and recovery flow is unchanged; tokens
never travel the queue. There is no DCO-specific wrapper.

## Commands

Build and save an offline plan:

```powershell
node scripts/dco-feature-flags.mjs plan `
  --application l-loans `
  --key example_flag `
  --description "Example" `
  --out $env:TEMP\example-flag-plan.json
```

For a long, quoted, Unicode, or multiline description, write the exact UTF-8
text to a file and pass `--description-file <path>` instead of
`--description` (mutually exclusive; the file form is also the bridge-safe
form over the LAN share).

Review the JSON, then place legitimately obtained tokens in the process
environment for read-only preflight. Separate tokens are preferred;
`DCO_ACCESS_TOKEN` is a shared fallback.

```powershell
$env:DCO_TEST_ACCESS_TOKEN = '<test token>'
$env:DCO_PRODUCTION_ACCESS_TOKEN = '<production token>'
node scripts/dco-feature-flags.mjs apply `
  --plan $env:TEMP\example-flag-plan.json `
  --state $env:LOCALAPPDATA\Codex\dco\example-flag-state.json
```

The first `apply` performs both preflights without mutation and returns
`AwaitingConfirmation`. After the user reviews the preview, a later invocation
may add `--confirm <planDigest>`. It creates test as `true`, verifies the exact
slash path and effective value, then creates production as `false` and verifies
it. Production is never attempted before successful test readback.

## Confirmation, readback, and recovery

The state file is plain operational history bound to the plan digest; it is not
permission to mutate. A sibling lock fails closed under concurrent applies and
recovers only a valid same-host stale owner whose PID is proven absent.

Confirmation remains a separate user action. If a future code revision backed
by observed exchanges reaches `AwaitingConfirmation`, only a later invocation
with `--confirm <planDigest>` may create, and that argument must be present in
the invocation that sends a create request. Immediately before any request the
CLI writes `UnknownOutcome`. Recovery performs exact-path readback only. Proven
absence yields `AwaitingRenewedConfirmation`; retry requires a later
`--renew-confirm <planDigest>`. An unreadable result never becomes permission
to retry.

Stable nonzero states include `AwaitingConfirmation` (3), `Duplicate` (4),
unknown/renewal (5), `FailedTest` (6), and `Partial` (7). Receipts exclude
tokens, authorization headers, cookies, and raw response bodies.
