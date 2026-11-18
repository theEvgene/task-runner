# Managed-write claims and the reservation referee

Opt-in, per-submit declaration of a job's explicit managed-write footprint,
and the authoritative reserved parallel path it unlocks. It extends
`submit` (`docs/lan-bridge.md`), which owns the client, the observation
rule, and the confirmed-state classification; this document owns the claim
semantics, the queue-local quota authority, and the release proof. The
worker side is in `docs/lenovo-worker-shortcut.md`.

`submit --claim <resource>` (repeatable; `submit_job(..., resource_claims=)`
in Python) declares the job's explicit managed-write footprint. It enables
the authoritative reserved parallel path for exactly this job:

```text
python scripts/lan_bridge/lan_bridge.py submit \
  --queue <queue-dir> --key-file <key-path> \
  --command-file <cmd-file> --cwd <working-dir-on-worker> \
  --claim 'file:C:\out\report.csv' --claim 'tcp:127.0.0.1:8080'
```

- The signed reservation (`queue/reservations/<job-id>.json`) and the job
  publish together under one queue-local operating-system lock, so two
  producers cannot both admit the same or a nested footprint. A conflicting
  footprint refuses with exit `2` before anything is published.
- Claims are normalized: absolute case-folded paths, and one exclusive
  `tcp:*:<port>` token per port. A wildcard bind, `localhost`, and a
  specific address naming the same port are the same claim.
- `capacity:<name>` is the only shared claim, and it is never inferred.
  Its quota is one queue-local file, `queue/capacity-quotas.json`, a plain
  JSON object such as `{"cpu": 2, "heavy-build": 1}`. The referee reads it
  inside the same lock as the reservation scan, so every producer behind
  the queue is bounded by the same ceiling and no submit call can raise it.
  At most 8 names per queue, each quota an integer from 1 to 256; a name is
  case-folded like a claim, and names that collide after folding refuse.
  Live holders are counted, an over-limit publish is refused by naming the
  exhausted resource and its limit, and a capacity claim with no entry in
  that file (or an unreadable or malformed file) is refused before
  publication. `--claim 'capacity:cpu'` therefore works once the operator
  writes the file. The Python `capacities=` keyword of
  `reserve_and_submit`/`referee_publish` is only an assertion: every name
  it states must equal the queue file, and a different value is refused,
  so omit it or keep it identical.
- A capacity quota bounds one queue directory; two queue roots on one host
  each apply their own file, so keep one queue per heavy-job pool.
- A refusal names the limiting resource: an overlapping port or path
  token appears in the conflict message, and a quota denial names the
  resource and its limit. Re-submitting an already published job returns
  `owned` and stays idempotent even after the quota file changed.
- Without `--claim` nothing changes: one direct immutable publish, the
  worker's `exclusive:global` fallback, and sequential execution.
- Admission alone does not free anything. The worker releases a footprint
  only by publishing `queue/releases/<job-id>.json` bound to the claim
  owner and generation, the exact job bytes, the resource set, and the
  exact result bytes; a signed result alone keeps the footprint held.

## Dell publication routes

`dell_request.py publish|transfer|publish-existing` already pin an absolute
Lenovo checkout and task worktree. `dell_lan/dell_managed_submit.py` submits
those three routes: it re-hashes the request against the digest the writer
printed, re-derives the claim set from those verified bytes
(`dell_lan/dell_managed_claims.py`), and calls this same `submit_job` seam, so
a claim list edited beside the request cannot be attached to the job. The
claims are the request's own `working-directory:` checkout and worktree: a
second request for the same or a nested target refuses before publication, and
a refused or failed submit leaves no job and no claim of its own.

A read-only request (`--reconcile`) declares no claims. The probe only reads,
so it must stay admissible while the unknown job it observes still holds the
footprint; admission alone frees nothing, and only the worker's proof-bound
release frees it. An unknown outcome therefore keeps the same job id and blocks
a second writer for that target.

The executed command is bound to the same verified bytes, not to the writer
record's own `command` text: the request records its path, interpreter, driver,
dialect, flow directory, and working directory inside its digest, and
`dell_managed_command.py` derives the one command from them. The submit
publishes that derived text and refuses when the command file differs, when the
record's `kind`, `dialect`, `receiptScriptPath`, or `expectedCwd` differs, or
when a read-only request is paired with the write command it would bypass. The
route's driver comes from the schema, so the three routes never share one.

Claims are normalized by the referee on the producer host and re-derived by the
worker. Pass a literal absolute path (no mapped drive, `subst`, or symlink
alias): an alias that resolves differently on Lenovo is refused there instead
of being admitted under the Dell spelling.

The producer referee is `scripts/lenovo_worker/job_reservations.py`: a
public facade over `job_reservation_claims` (claim identities and record
validation), `job_reservation_quota` (the queue-local quota authority),
`job_reservation_store` (bound reads, live scan, release proof), and
`job_reservation_errors` (one refusal type). Its contract tests are
`tests/lenovo_worker/test_reservation_referee.py` (canonical footprints,
live-lock authority, release proof, concurrent worktree race) and
`tests/lenovo_worker/test_reservation_referee_capacity.py` (configured
quota, fail-closed classification, one queue-local authority, two-process
quota race, supported submit route); both share
`tests/lenovo_worker/reservation_fixtures.py`.

## Lost submit response: the exact job id

A managed submit prints `JOBID=<id>` only after the signed publish succeeds.
If that response is lost (client killed, terminal closed, transport error),
the id is still a pure function of the request digest the writer already
recorded:

```text
id = sha256("dell-managed-job-id/1\0" + requestSha256)[:32]
```

`dell_managed_submit.py` submits exactly that id, so it can be recomputed
instead of guessed, and one read-only readback reports the exact state
without publishing anything:

```text
python scripts/dell_lan/dell_managed_submit.py --recover \
  --writer-output <writer-record> --queue <queue-dir> \
  --key-file <key-path> --repo <dell-checkout>
```

It prints one `dell-managed-recovery/1` JSON record: `jobId`, `job`
(`published`/`absent`), `jobEnvelopeSha256`, `requestSha256`, `result`
(`present`/`absent`, with `resultStatus`/`exitCode` when present),
`reservation` (`present`/`absent`/`inert`/`missing`), `footprintHeld`, and
`resources`. Every record is opened by that exact id and re-authenticated;
nothing scans `reservations/`, and nothing is published. Exit `0` means the
state was established, not that the job succeeded.

- `job: absent` means the job publish did not land. A reservation file
  without its job is `inert`: the producer aborted under the referee lock, so
  it holds nothing. The same writer record may then be submitted again - the
  same id and identical signed bytes resolve as `owned` instead of publishing
  a second job or a second reservation.
- `job: published` means the write may execute. Recovery keeps the same id;
  the read-only `--reconcile` probe stays the only admissible job for that
  target until the worker's proof-bound release, which `footprintHeld`
  reflects.
- A job under the id whose signed command is not this request's command is
  refused (`recovery-unproven`), never reported as recovered; so is an
  unreadable or foreign record, and a published write whose reservation is
  `missing` reports `footprintHeld: null`, never `false`.
- The id is a deterministic name, not a secret and not an authentication
  token: every state above comes from the signed envelope. Never discover the
  id by scanning `queue/reservations/*.json` - another job's reservation is
  not evidence about this request - and never re-submit a write just to be
  told the id.
