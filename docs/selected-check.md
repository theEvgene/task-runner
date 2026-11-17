# One bounded, host-bound selected check

Agent entry: use `scripts/readiness/selected.py` when you need the answer from
exactly one short command on an explicitly chosen host, with its real exit and
one bounded receipt. It is not a CI runner, a second queue, or a replacement
for `docs/readiness.md`: it reuses the existing readiness environment model and
the existing signed queue.

It does not install, update, configure, or repair a host. It never falls back
to another machine and never resubmits a job whose outcome is unknown.

## Descriptor (`lan-selected-check/1`)

Put one JSON file outside every checkout. It names one host, one exact argv,
one cwd, one expected checkout revision, and a declared read-only effect. The
effect is a contract: this seam refuses any value other than `read-only`; it
cannot prove that an arbitrary program really is read-only, so choose the
command accordingly. There is no list of checks: one descriptor is one check.

```json
{
  "schema": "lan-selected-check/1",
  "check": {
    "name": "<short name>",
    "host": "dell",
    "effect": "read-only",
    "argv": ["<executable>", "<arg>", "..."],
    "cwd": "<target working directory>",
    "expectedRevision": "<40-lowercase-hex>",
    "timeoutSeconds": 120,
    "maxOutputBytes": 65536
  },
  "dell": {"revisionRepository": "<checkout on this host>", "git": "git"}
}
```

- `argv` is a real argument vector, not a shell line. It is length-bounded and
  executed without a shell. Child stdout/stderr are discarded, so the receipt
  is the only output and no private check output is relayed.
- `timeoutSeconds` is capped at 600 by this seam. A timeout is a failed check,
  never a pass.
- `expectedRevision` is the pinned 40-hex checkout SHA. A different local SHA
  is `NOT_RUN`, not success.
- `dell.revisionRepository` must be the checkout that contains `check.cwd`.
  The revision is read from that repository only: a cwd outside its git
  toplevel, or a repository that names another checkout, is `NOT_RUN`
  (`revision-unbound`) before anything is spawned.
- `cwd`, the executable and the revision are preflight conditions on the
  target host.

For a Lenovo check, replace the `dell` block with:

```json
{
  "lenovo": {
    "queue": "<queue-dir>",
    "keyFile": "<key-file>",
    "keyFormat": "raw",
    "python": "<Lenovo python already proven by readiness>",
    "git": "git",
    "readinessConfig": "<outside readiness config>",
    "readinessJobId": "<32-lowercase-hex readiness job id>",
    "requiredComponents": {"cwd": "<directory component name>",
                           "tool": "<tool component name>"},
    "waitSeconds": 600,
    "pollSeconds": 30,
    "evidenceWaitSeconds": 0.5
  }
}
```

The `readinessConfig` is the same `lan-readiness-config/1` file used by
`docs/readiness.md`; `readinessJobId` is the `jobId` printed by that run. The
check is bound to the evidence: the named directory component must be
`check.cwd`, the named tool component must be `argv[0]`, and the descriptor
python must equal the readiness python. A missing or unbound readiness result
is `NOT_RUN` and publishes no check job; a bad MAC is a protocol refusal
(exit `2`) and likewise publishes nothing.

## Run

```text
<python> scripts/readiness/selected.py --descriptor <outside.json> \
  [--receipt-out <outside.json>] [--wait 600] [--poll 30] [--job-id <id>]
```

`--wait` and `--poll` override only the Lenovo descriptor values. Use the
owner live defaults (`600` / `30`); short values are bounded fixture probes
whose expiry is `UNKNOWN`.

`--job-id` is the same-id recovery path, mirroring `scripts/readiness/check.py`:
it re-reads exactly one existing Lenovo job and never submits. The named job
must be the exact selected check (same signed command, cwd, timeout, output
bound and dialect); otherwise the run is `NOT_RUN` (`job-unbound`) and still
submits nothing. A Dell descriptor refuses `--job-id`.

Exit `0` is `PASS`; exit `1` is a verified `FAIL` or `NOT_RUN` receipt; exit
`2` is a descriptor, policy, or protocol refusal that happens before any
check job exists (including a bad MAC on the readiness evidence); exit `3` is
`UNKNOWN` because no verified result arrived during the bounded wait, or
because the published job's result could not be verified. An `UNKNOWN`
receipt keeps the submitted `jobId` so the operator can re-read it.

## What is proven where

- Dell: the local preflight checks cwd, executable, that `revisionRepository`
  is the checkout containing `cwd`, and `git rev-parse HEAD` before anything is
  spawned. A refused preflight starts no process. The child's real integer
  return code is the result; HEAD is re-read after the child and a moved
  revision cannot pass.
- Lenovo: the local preflight checks the queue, the key, the readiness
  evidence and the descriptor-to-evidence pins before any submit. It then
  publishes exactly ONE signed job through the existing queue. That job
  carries a fixed preamble which re-checks cwd, executable and the checkout
  revision on the target host and prints one small JSON line. A preamble
  refusal prints `NOT_RUN`, exits `78`, and never runs the check. A preamble
  check timeout prints `FAIL`, exits `79`, and is never a pass. Otherwise the
  preamble runs the exact argv without a shell, prints `RUN` with the real
  child exit, and exits with that same code.
- The receipt accepts the result only when the signed envelope verifies
  (MAC and id), the envelope exit agrees with the receipt exit, a signed
  `RUN` names exactly the expected SHA, and the consuming adapter has real
  role evidence for the requested host. A missing result, a worker timeout,
  truncated output, an unreadable or unverifiable result, or a disagreement
  between envelope and receipt is `UNKNOWN` or `FAIL`, never `PASS`.

A failed local preflight publishes no remote job. The target-host preamble is
part of the one submitted job; it is the only way to verify a remote revision
without a second submit, and it refuses before the check is spawned. The
readiness probe that supplies worker/cwd/tool evidence is a separate earlier
read-only run with its own explicit authorization. When a remote result is
unknown or unverifiable, the receipt keeps the job id and the next run uses
`--job-id` instead of publishing another job.

## Receipt (`lan-selected-check-receipt/1`)

One JSON object is printed (and optionally written by `--receipt-out`):

- `check`: the name, the declared effect, a command digest and argument count,
  and a cwd digest. Absolute paths, command text and child output are not
  included.
- `requestedHost`, `observedHost` and `hostEvidence`, plus `hostBound`.
  `observedHost` names the adapter that consumed a verified result: `dell`
  for the local child, `lenovo` for the MAC-verified result of the one job
  submitted to the owner queue. `hostEvidence` names that evidence
  (`local-child` or `signed-queue-result`) and is null when no result was
  verified. `hostBound` is true only when a verified result exists, the
  consuming adapter is the requested host and its evidence is present. This
  is a transport-role binding, not a worker-reported hostname attestation.
  `PASS` requires `hostBound` true.
- `outcome`: `PASS`, `FAIL`, `NOT_RUN` or `UNKNOWN`.
- `revision`: the expected SHA, the observed SHA when readable, its state
  (`confirmed`, `stale`, `unconfirmed`) and the evidence source
  (`local-git` or `signed-target-preamble`). `confirmed` requires the observed
  SHA to equal the expected SHA: a signed `RUN` naming another SHA is
  `NOT_RUN` (`revision-stale`), never `PASS`.
- `preflight`: `confirmed` or `refused` with one bounded reason code.
- `execution`: the transport kind, `jobId` for a signed job, `verified`, the
  real `exitCode`, `timedOut`, `outputTruncated` and one reason code. The
  `jobId` is kept on every post-submit error, including a bad MAC or an
  unreadable readback, so the outcome can be re-read.
- `nextAction`: one bounded action. On `UNKNOWN` it names the same id and
  tells the operator to re-read it with `--job-id`; never resubmit while the
  outcome is unknown.

The receipt never contains the key, queue contents, worker stderr, credentials
or private absolute paths. A `NULL` field means the fact was not observed; it
is never promoted to `PASS`.

## Limits

- One chosen check per descriptor; no suite, benchmark, or matrix, and no new
  checked runner.
- No alternate host, no install, no credential or SMB change.
- The declared read-only effect is not enforced on an arbitrary program.
- A Lenovo check needs a prior readiness result for that worker and
  environment. Without it the result is `NOT_RUN`; the selected check does not
  create its own readiness evidence. `--job-id` recovery does not need that
  evidence again, only the named job and the key.
- The host role in a receipt is the transport binding described above; the
  signed result carries no worker-reported hostname, so the receipt never
  claims one.
- A live run needs the owner's explicit authorization on top of queue/key
  ownership. The fixture suite below reaches no host.

## Local fixture verification

```text
<python> -m unittest discover -s tests/readiness -p "test_selected_check*.py" -v
```

The suite uses temporary files, an ephemeral fake key and a fake worker. It
covers the Dell pass, stale revision, a revision repository outside the check
cwd, missing cwd/tool, nonzero exit, the real target preamble, the Lenovo
pass, a refused target preamble, nonzero exit, `UNKNOWN` without
resubmission, same-id recovery without a second submit, an unverifiable
result that keeps its job id, and refused or tampered readiness evidence. It
performs no network, SMB, or corporate access.
