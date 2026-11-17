# Lenovo one-shortcut worker + cleaner with SMB connect

> Agent entry. Use ONLY this scenario when: after a Lenovo reboot the SMB
> access to Dell is gone and the worker must be back with one double-click.
> Do NOT use it for: queue protocol questions (`docs/lan-bridge.md`), base
> import or publishing (`docs/dell-lan.md`, `docs/fresh-base.md`), or any
> live-queue debugging without owner consent.

One shortcut restores access to the EXISTING share and starts the worker
(`scripts/lenovo_worker/worker.py`) and the cleaner
(`scripts/lenovo_worker/cleaner.py`). Nothing is installed as autostart.

## Worker delivery (determined from this repo)

This repository vendors no prebuilt worker binary and no service: the worker
entry is `scripts/lenovo_worker/worker.py` with its sibling executor classifiers
(`powershell_truth.py` and `cmd_fallback.py`; all stdlib only, same HMAC protocol
as `scripts/lan_bridge/lan_bridge.py`), and the only cleaner is the sibling
`cleaner.py`. Delivery = this checkout
at the pinned revision the operator copies to Lenovo. `launch.py` starts the
sibling copies by default; `--worker-dir` or explicit `workerCommand` /
`cleanerCommand` in the local config names another checkout. A remembered
path from another machine is never trusted: the config must name it.

## One-time setup (on Lenovo, ~5 minutes)

1. Copy this checkout to Lenovo and note its revision (`git rev-parse HEAD`).
2. Copy `scripts/lenovo_worker/config.example.json` to
   `%LOCALAPPDATA%\task-runner\lenovo-worker.json` (outside Git AND outside
   the share) and fill real absolute paths. The example holds fixture values
   only.
3. Save the SMB password as plain text (user-accepted, see #23) into the
   `passwordFile` path from step 2 — one line, no trailing spaces. This file
   stays outside the repo and outside the share.
4. Lock both local files to the current user (PowerShell, admin not needed):

```powershell
icacls "$env:LOCALAPPDATA\task-runner\lenovo-worker.json" /inheritance:r /grant:r "$env:USERNAME:(R)"
icacls "C:\Users\owner\.secrets\lan-smb-password.txt" /inheritance:r /grant:r "$env:USERNAME:(R)"
```

Verify with `icacls <file>` — only the current user may appear.

5. Create the shortcut: right-click
   `scripts\lenovo_worker\Launch-Worker.bat` → Send to → Desktop shortcut.
   Optional: shortcut Properties → Advanced → Run as administrator OFF
   (never needed), Window: Minimized.

## Normal use

Double-click the shortcut. Success = one JSON line on stdout with
`"outcome":"ready"` and stderr `launch ready (worker:… cleaner:…)`.
A second click while both run reports `already-running` with the same pids
and starts nothing new.

## What it guarantees

- Existing queue only: a missing `queue/`, `queue/jobs/` or `queue/results/`
  refuses with `queue unavailable … check path/mount`. No empty queue is
  ever created, and the worker seals into a fresh dir never. `trashDir` and
  `stateDir` must sit outside the queue and the shared folder (checked
  lexically before anything is created), so creating them can never
  materialize queue parents and the cleaner can never see launcher state.
- Existing jobs/results are never rewritten: results are published
  immutably; a sealed id is skipped, never replaced. A result the worker
  cannot seal for infrastructure reasons is reported as failed, never as
  sealed, and a finished `--once`/`--max-jobs` batch exits 2 when any job
  was refused or failed. Invalid `working_directory`/spawn failures seal a
  bounded terminal error (exit 2) instead of crashing or re-executing.
  A job that carries the signed `expected_working_directory` is compared with
  its `working_directory` before any claim or spawn: a provable difference
  seals one `target-cwd-mismatch` terminal error naming only path
  fingerprints, and the command never starts. A job without a comparable
  expectation still runs, but its target is reported as `unverified-target`
  in the worker log, never as verified.
  Declared return files keep the bridge contract (any path the job names)
  with byte bounds (8 files, 256 KiB each, 1 MiB total); truncation sets
  `output_truncated`. Outputs plus encoded files are fitted to a safe
  payload ceiling so the signed envelope stays under 2 MiB; a size refusal
  seals one minimal fallback result so the job never re-executes.
- Before any command starts, the worker immutably publishes a MAC-verified
  `queue/claims/<job-id>.json` envelope, binding the signed job bytes, one
  owner and generation, and the complete authenticated resource set. Existing
  claim bytes are read, authenticated, and checked against those bindings;
  malformed, forged, or mismatched claims refuse rather than acquiring
  authority from their filename. Immediately after a successful spawn the
  worker publishes a MAC-verified `queue/starts/<job-id>.json` record
  binding the same generation to the exact spawned child pid and owner. A
  missing signed result never clears this claim: after a worker crash or
  failed result seal, later scans report `owned` and a finite batch exits
  `3` (unknown) instead of running the side effect again. Recovery is only
  through `--reconcile-id ID --reconcile-proof PATH`: PATH must be a signed
  `lenovo-job-reconciliation/2` envelope bound to the claim generation, job
  envelope SHA-256, exact resources, the claim owner, and the durable start
  child pid, carrying typed `lenovo-job-terminal-evidence/1` that states
  `tree_absent: true`, an observer, and an observation time. Reconciliation
  refuses while the claim owner pid is still alive (stop that worker first),
  when no durable start record binds the same generation/owner, when the
  recorded child tree may still be alive (pid or descendants/group members
  observable, or inventory unavailable), when typed evidence names a
  different tree, or when `tree_absent` is not asserted. It also refuses the
  previous `lenovo-job-reconciliation/1` schema and any opaque
  `evidence_b64` bytes: a key holder can sign arbitrary bytes, so bytes
  never authorize success. A crash-killed execution has no observed exit
  code, signal, or output, so the sealed terminal state is always
  `status: error`, `termination: unknown`, `exit_code: null`, with
  `reconciliation.outcome_derivation: unobserved-crash`; it is never `ok`.
  Age, a live owner, a live child, an unsigned assertion, a hash without
  bytes, or deleting/re-submitting the job is not proof. On settlement the
  worker publishes the signed result and a signed
  `queue/releases/<job-id>.json` record whose generation, job, resource,
  and result bindings are all re-validated before idempotent acceptance.
- Parallel admission requires an authoritative reservation: a signed
  `lenovo-job-reservation/1` envelope in
  `queue/reservations/<job-id>.json` bound to the exact job envelope
  SHA-256, one owner/generation, an explicitly normalized resource set, and
  a resource digest. The supported managed-write producer entry is
  `lan_bridge.submit_job(..., resource_claims=[...])` (`submit --claim` on
  the bridge CLI); a submit without claims keeps its legacy one-publish,
  sequential behavior. The producer referee
  (`scripts/lenovo_worker/job_reservations.py`) holds one queue-local
  operating-system lock while it refuses the same or nested footprint from
  a different job and publishes the reservation plus the job; a refused
  attempt publishes nothing and a failed job publish removes only its own
  provisional reservation. The operating system owns that lock and drops it
  when its holder exits, so elapsed age never breaks a live lock and a
  contender never steals one. Resource identities are normalized paths plus
  one exclusive `tcp:*:<port>` token per port, so a wildcard bind,
  `localhost`, and a specific address naming that port are the same claim.
  The worker re-derives every binding and adds its own normalized working
  directory, so a payload cannot omit the directory it runs in. A
  reservation is live until `queue/releases/<job-id>.json` proves this exact
  job, resource set, claim generation, and result bytes reached a terminal
  result: a signed result alone, a missing job (producer aborted under the
  lock, so the record is inert), age, or a timeout never frees a footprint.
  Producer `resource_claims` / `resource_scope` in the payload remain
  self-attestation: without a verified reservation the job receives the
  `exclusive:global` fallback and runs sequentially. The
  production-compatible default is still one active job; `--max-active 2`
  is an upper bound that only the reservation proof can fill, so two
  reserved independent jobs run together and a third waits. Unmodelled side
  effects outside the declared footprint remain the producer's documented
  boundary (#88): only explicit managed write footprints are admitted in
  parallel.
- Every command runs in its own process group/session. On Windows the child is
  created suspended, assigned to a Job Object with kill-on-close, durably
  recorded as `queue/starts/<job-id>.json`, and only then resumed, so no
  command code runs before kill-on-close ownership exists. Containment
  failure, a failed start record, or a failed resume terminates and reaps the
  still-suspended child and leaves no command side effect. A worker crash in
  that window leaves the child suspended (no command code, no external
  effect); a crash after resume kills the tree with the closing job handle.
  The worker terminates the job explicitly, so even a deliberately detached
  grandchild cannot outlive it. Assignment or termination that cannot be
  proven stays unknown: no terminal result is sealed. A normal exit seals a result only after an explicit job
  termination plus a descendant sweep proves the tree absent (stragglers are
  killed within a bound); survivors leave the job owned/unknown with no
  result. Timeout kills only that job tree and signs a timeout only after the
  tree-kill operation succeeds and the child/output pipes settle within a
  bound (`exit_code: 124`, `termination: timeout`, plus observed process
  exit and POSIX signal when available). A failed/nonzero tree kill,
  unproven containment/termination, or unsettled pipes returns promptly as
  owned/unknown and publishes no terminal result. Neighboring jobs are not
  addressed.
- Key custody: a key inside the queue, the checkout, or the share refuses
  before any work. The password travels only inside a PowerShell
  `-File` script that reads it from the local file; it never appears in
  process args, stdout, stderr, logs, or receipts. The file holds one line;
  a trailing CRLF is trimmed the same way in the fixture check and in the
  real SMB mapping, and a multi-line file refuses.
- One launch at a time: an exclusive state lock is held from before any
  inspection until after both pid files are written, and both roles are
  inspected before either is spawned. A provably dead pid (stale, legacy, or
  changed config under a dead pid) is a safe reboot recovery: it is removed
  under the lock and the role is started fresh, so a reboot needs no manual
  cleanup and never duplicates a live process. Ambiguous pid state (live pid
  under a changed command/binding, foreign command line, unprovable command
  line, corrupt content, or an unrecognized pid format around a live pid)
  refuses with the exact pid file to inspect and starts nothing and deletes
  nothing. The launcher never kills, never replaces, never `--force`s. Remediation:
  verify no such process runs (`tasklist /FI "PID eq N"`), then remove
  `stateDir\<role>.pid`.
- Process identity is bound to the full command array plus its configuration
  (`queue`/`keyFile` for the worker, `shared`/`trash` for the cleaner). A
  live pid recorded under a different command or binding refuses instead of
  being adopted.
- The shortcut propagates the launcher exit: `ready` is exit 0, any refusal
  is exit 2.
- Corporate policy is unchanged: no autostart, no firewall/share change,
  no credential written to the queue or the share.

## Failures (all exit 2; ambiguous/config failures start nothing new)

| Message                       | Meaning                     | Fix                                   |
| ----------------------------- | --------------------------- | ------------------------------------- |
| `cannot read config`         | config path wrong           | point the shortcut at the real JSON   |
| `share unavailable …`         | Dell down / UNC wrong       | check host, share name, cable/VPN     |
| `bad credentials …`           | SMB refused user/password   | fix user or password file content     |
| `queue unavailable …`         | share up, queue path wrong  | fix `queueDir` to the existing queue  |
| `key unusable / key inside …` | key file wrong or misplaced | re-issue key outside queue/share/repo |
| `trash/state … must be outside` | trash/state inside queue/share | move them outside both             |
| `ambiguous <role> state`      | live foreign/unprovable pid, live pid under changed config | verify, then remove that pid file |
| `roles[].state: started` (fresh pid) | dead pid after a reboot (legacy or changed config) | no action: relaunched fresh under the lock |
| `another launch is in progress` | concurrent double-click   | wait seconds, re-run to adopt pids    |
| `<role> exited at once`       | child crashed               | read `stateDir\<role>.log`            |

## Manual post-reboot validation (REQUIRED, manual only)

Automated tests below use fixtures only and prove no live run. After a real
reboot, with the owner present and a TEST queue (never the live queue or
foreign jobs):

1. Reboot, log in, double-click the shortcut → `ready`, two processes in
   `tasklist` matching `worker.py` and `cleaner.py`.
2. Submit one test job, run one `fetch`, confirm a verified result.
3. Double-click again → `already-running`, same pids.
4. Disconnect the share / wrong password → clear refusal, no new queue.

Status: NOT run by the agent (live validation needs the owner at the
keyboard after a reboot). Record its commands, exits, and revision in the
issue when done; until then treat this scenario as fixture-verified only.

## Local verification (fixtures, no network, no live queue)

```text
<python> -m unittest discover -s tests/lenovo_worker -v
```

Covers: worker seals one result and skips sealed/foreign ids; authenticated
durable claims prevent duplicate execution after a worker crash; retained
claims make finite batches exit unknown and reconcile only from a claim-bound
typed proof while the owner is dead and the recorded tree is proven absent,
sealing a truthful non-success `termination: unknown` instead of an asserted
success; real short subprocesses prove two independently reserved jobs are
active together while a third waits, the same/nested footprint and a
reservation bound to other job bytes are refused before execution,
self-attested disjoint claims still serialize, and the supported producer
entry (`lan_bridge.submit_job` with `resource_claims`, exercised through the
real worker) is what creates the reservation; a live referee lock is never
reclaimed because its timestamp aged, an overlapping claim is refused while
a signed result exists without its proof-bound release, release validation
rejects a wrong generation, resource set, or result digest, and TCP
wildcard/alias claims for one port collide while independent ports do not; global/shared-
resource serialization, malformed-claim refusal without execution, and
isolated stop/timeout (including nonzero Windows tree-kill handling);
unproven Windows containment/termination retains unknown with no terminal
result, a delayed containment failure runs no command code, and a worker
crash in the spawn/containment window leaves no side effect;
detached descendants are killed before success is sealed and
post-spawn I/O failure retains unknown instead of a terminal result; release
records re-validate generation, job, resource, and result bindings; failed seals
reported as failed with batch exit 2; bounded return files with truncation
flag; invalid cwd seals a bounded terminal error without re-execution;
outputs plus encoded files fitted under the 2 MiB envelope with a minimal
fallback seal instead of re-execution; missing queue refuses without creating it; key inside
queue/share/checkout refuses; cleaner moves only top-level `*.tmp` outside
the configured queue/trash/state and refuses misplaced trash/state; launch
success + repeat is idempotent with command/binding adoption; dead pid
records recover after a reboot without duplicates while live foreign/changed
pids still refuse with nothing started or deleted; concurrent
launch serializes on the state lock; misplaced trash/state refuses before
anything is created; unavailable share and bad credentials refuse; password absent from all
outputs, logs, and child command lines.
