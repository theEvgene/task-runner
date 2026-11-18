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
