---
name: create-dco-feature-flags-protocol
description: Create paired boolean DCO feature flags for L Loans UI or Finance Front through the non-browser protocol CLI. Use this distinct route when explicitly requested or when browser automation is unsuitable. Do not use to update, delete, archive, or personalize flags.
---

# Create DCO Feature Flags through the Protocol

Use the bundled CLI as the execution boundary. Collect only feature-flag
`key`, `description`, and optional `application`; L Loans UI is the default.
Run it on the host that holds the bearer tokens: locally when the tokens are
local, or on Lenovo through the generic signed queue when the tokens live
only there (see `docs/lenovo-hosted-scripts.md` in the pinned task-runner
checkout; plan and state stay Lenovo-local, same confirmation flow, no
DCO-specific wrapper). Preserve description capitalization and strip Markdown
emphasis only when it
is formatting.

## Plan

Resolve `scripts/dco-feature-flags.mjs` relative to this `SKILL.md`, never
relative to the caller's working directory. Run `plan` with the collected
values and an `--out` path outside the checkout. For a long, quoted, Unicode,
or multiline description, write the exact UTF-8 text to a file and pass
`--description-file` instead of `--description` (mutually exclusive; the file
form is also the bridge-safe form over the LAN share). The CLI accepts no capture,
review, trust-store, or signing inputs.

Show the immutable preview and `planDigest`: application, key, description,
test value `true`, production value `false`, boolean type, personalization
off, no expiry, and the returned `protocolBasis`. Planning performs no network
request. State explicitly that the L Loans stage create/readback mapping is
observed, while production and Finance Front reuse it as a documented
inference with bindings from the existing operational skill.

Complete when the user has reviewed the exact preview.

## Preflight and confirmation

Read [protocol-cli.md](references/protocol-cli.md), then run `apply` with the
saved plan and a private state path outside the checkout, without `--confirm`.
Obtain bearer tokens through the host's secret mechanism and expose them only
as `DCO_TEST_ACCESS_TOKEN` and `DCO_PRODUCTION_ACCESS_TOKEN` (or the shared
`DCO_ACCESS_TOKEN` fallback). Keep credentials out of arguments, prompts,
files, receipts, and logs.

The first invocation checks the parent, boolean type, and exact-path duplicates
in both environments. It never creates and normally returns
`AwaitingConfirmation`. Show the immutable preview and ask for the same single
explicit confirmation used by the existing UI skill. Only a later invocation
may pass `--confirm <planDigest>`.

Treat the CLI result as authoritative:

- `Duplicate`: report the existing exact-path target and stop.
- `AwaitingConfirmation`: show the current preview and ask for explicit user
  confirmation. Only a later invocation may pass `--confirm <planDigest>`.
- `UnknownOutcome`: rerun the same `apply` without confirmation so it performs
  readback only; do not issue another create.
- `AwaitingRenewedConfirmation`: show the unchanged digest and request renewed
  confirmation before using `--renew-confirm <planDigest>`.
- `TestVerified`: resume production with the already approved digest by passing
  `--confirm <planDigest>` again; do not ask for a second approval unless the
  plan changed.
- `Completed`, `FailedTest`, or `Partial`: report every observed row and the
  exact state prominently.

After confirmation, the CLI creates test with effective value `true`, performs
exact-path `getEntities` plus `getToggle` readback, and only then creates
production with effective value `false`. It never creates production when test
readback is absent, unreadable, mismatched, or has the wrong effective value.
The runtime does not ingest a capture, reviewer decision, signature, or trust
material to enable mutation.

Complete when the CLI returns a terminal receipt or a state requiring user
input. A stale lock is recovered only when same-host metadata is valid and the
recorded PID is proven absent. Never delete a lock manually. The state file is
operational history, not authorization: any invocation that could create must
carry the exact current confirmation argument. Test and production are
separate systems, and rollback never means modifying or deleting an existing
flag.

## Report

Return the receipt's state, digest, application, `protocolBasis`, and each
environment's preflight, mutation, expected value, observed value, and toggle
ID when present. Preserve each target's `protocolEvidence` label so observed
and inferred behavior is never conflated. Distinguish a new user decision in
`confirmationRequired` from reuse of the already approved digest in
`confirmationArgumentRequired`. Keep the state file for recovery until the
manager accepts the result.
