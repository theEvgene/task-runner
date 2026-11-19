# Issue #80 — two-producer live check: sanitized evidence note

Scope: operative live acceptance only for `theEvgene/task-runner#80`.
Parent revision: `f9a0f146c6cd442179b9b72e71102205dbedf7ca`.
No runtime, protocol, queue, worker, or test change in this note.

## Authorization boundary

- User explicitly authorized the existing signed queue for two harmless
  read-only jobs and waived per-sender cryptographic identity.
- No other live work was authorized; this worker ran no live jobs.

## Supplied observation (manager-provided, not independently re-verified here)

- Date: 2026-11-19. Checkout HEAD at observation:
  `f9a0f146c6cd442179b9b72e71102205dbedf7ca`.
- Two independent local submit processes published two distinct jobs:
  - `f0256409885d20e8f17c6958d5c6d065`
  - `a5f8f3321b6b65818e5459a799917845`
    with distinct markers.
- Both results were MAC-verified, exit `0`, and each matched its own ID
  and marker. A valid signed result read under the other ID was refused
  (wrong-ID binding held; no cross-ID acceptance).
- Observed execution was sequential (13:07:38–42 UTC and 13:07:43–46 UTC).
  No concurrent-execution claim is made.
- 33 focused local readiness and bridge tests passed (supplied; not re-run
  on this host — no Python runtime here).
- Prior same-ID unknown contract is covered by tests, not by a live
  reproduction in this check.

## Limits explicitly not claimed

- Worker process release revision was not independently established:
  checkout revision only.
- Shared key proves possession and MAC/ID binding, not the identity of
  individual senders (waived per authorization).
- Independent read-only re-verification of queue artifacts was not possible
  from this checkout: no queue/key material and no Python runtime are
  available here. Evidence is recorded as supplied.
- No host identities, keys, or full sensitive paths are recorded in this note.

## Acceptance mapping (#80)

- Results never cross IDs, MAC, or sender ownership: supported for ID/MAC
  binding by the two supplied results plus the wrong-ID refusal; sender
  ownership beyond shared-key possession is explicitly not claimed.
- Unknown does not authorize duplicate submit: not live-reproduced in this
  check; contract coverage is test-only.
- Concurrent submit vs concurrent execution distinguished: only sequential
  execution was observed; no concurrent-execution or conflicting-write
  claim is made.

Status: evidence-only record. Live re-verification, if required, needs the
authorized queue/key and a Python-capable host; no further live jobs were run.
