# Lenovo owned/unknown job recovery — `LenovoJobRecovery.exe`

Small Windows executable that prepares an evidence-gated recovery for one
retained Lenovo queue job. It performs **no queue mutation itself**: the only
writer is the canonical worker (`scripts/lenovo_worker/worker.py
--reconcile-id/--reconcile-proof`), invoked solely after every fail-closed
gate passes.

Canonical sources mirrored (read-only, never modified):
`docs/lenovo-worker-shortcut.md`,
`scripts/lenovo_worker/job_reconciliation.py`, `job_ownership.py`,
`job_tree.py`, `proc.py`, `launch.py`, `scripts/lan_bridge/lan_bridge.py`.

## Toolchain — Go (deterministic build)

The single exe is built with Go 1.24.7 exactly (stdlib only, no dependencies,
no network): `scripts\lenovo_worker\job_recovery\build-go.bat` pins
`GOTOOLCHAIN=local`, `GOPROXY=off`, `GOOS=windows`, `GOARCH=amd64`,
`GOAMD64=v1`, `CGO_ENABLED=0` and runs
`go build -trimpath -buildvcs=false`, refusing any other toolchain.
`go.mod` additionally pins `toolchain go1.24.7`, so an older toolchain fails
instead of silently building. `-trimpath` strips checkout-local paths and
`-buildvcs=false` omits VCS stamping, so the same sources built with these
settings are byte-identical. Sources are split across small files (`main.go`,
`jsonx.go`, `bridge.go`, `custody.go`, `validate.go`, `recovery.go`,
`proof.go`, `settle.go`, `procs.go`, `receipts.go`, `selftest.go`,
`sanitize.go`, `refusal.go`) so each stays under the file token guard; all compile into
the one exe.

Reproduce: install Go 1.24.7, run `build-go.bat` (any caller cwd works; it
builds from its own directory), then rebuild from a clean checkout
(`git worktree add <dir> <commit>`) and compare full SHA-256
(`certutil -hashfile LenovoJobRecovery.exe SHA256`). Both must equal
`0a0daedd120342a8e6265e708c430643e071d5c236d1f961307110fde3d25f48`
(`go version go1.24.7 windows/amd64`; current commit sources plus the
deterministic build settings). A mismatch means the sources, toolchain, or
flags moved: do not ship the exe. The previous blessed bytes (`d095b357…`)
predate the deterministic flags (their Go build ID differs while all code
bytes match) and are superseded.

## What it does

`--job-id` is required with no default: omitting it refuses
`job-id-invalid` before any config, queue, key, or proof access, so the
tool never acts on a baked-in real id.

1. Reads the Lenovo local config (default
   `%LOCALAPPDATA%\task-runner\lenovo-worker.json`): `queueDir`,
   `sharedDir`, `keyFile`, `keyFormat`, `workerDir`, `python`. Refuses
   `config-unreadable`, `config-invalid` (incl. relative paths;
   `queueDir`/`sharedDir` may be UNC or a mapped share — the SMB share is
   expected there), `queue-missing` (never creates a queue), `key-unusable`,
   `key-misplaced` (key inside queue/share/checkout refuses; the key must
   also stay on a local fixed disk — UNC/device/mapped-drive keys refuse;
   checkout is derived from the canonical `worker.py` path —
   `<repo>/scripts/lenovo_worker/worker.py` → repo root — never from the
   exe install location), `proof-misplaced` (proof inside the queue,
   the shared folder, or the checkout refuses; relative proof paths and a
   proof path equal to the key file refuse — writing the proof must never
   be able to clobber the key).
2. Authenticates `queue/jobs/<id>.json`, `queue/claims/<id>.json`,
   `queue/starts/<id>.json` (HMAC-SHA256 envelopes, kind/id binding,
   constant-time MAC compare — byte-compatible with the canonical bridge;
   base64 is strict `validate=True` semantics: any whitespace inside
   `payload_b64` or the key file refuses).
3. Verifies exact bindings: claim schema/kind/id/held-state, 32-hex
   generation, owner instance/pid types,
   `job_envelope_sha256 == sha256(job bytes)`, resources equality; start
   binds the same generation/owner/child pid/resources/envelope. Anything
   else refuses (`claim-invalid`, `claim-binding-mismatch`,
   `start-mismatch`, `no-durable-start`).
4. Fail-closed liveness: claim-owner pid must be dead (`owner-active`
   refuses — stop that worker first); the recorded child pid plus its
   transitive descendants (Toolhelp32 snapshot) must be absent. Snapshot
   failure, unobservable pids, any `tasklist` execution/observation failure,
   or a possibly resurrected/reparented pid
   refuse (`tree-active` / `inventory-unavailable`) — absence is never
   inferred from unavailability (a non-zero `tasklist` exit is an error,
   not proof of death).
5. Signs a typed `lenovo-job-reconciliation/2` proof carrying
   `lenovo-job-terminal-evidence/1` (`tree_absent: true`, observer,
   `observed_at`) bound to claim generation, job digest, resources, owner
   and child pid, and writes it **outside the queue** (`--proof-out`,
   default `%TEMP%\lenovo-recovery\<id>.proof.json`). The outcome is
   always the truthful non-success (`error`/`unknown`), never a derived
   success.
6. `--invoke` additionally spawns the canonical worker with the proof
   (paths only on the command line — key material never leaves memory, is
   cleared after use, and never appears in stdout/stderr/receipts;
   `--shared` is forwarded so the worker re-enforces its own key custody),
   then re-validates the settlement exactly like the canonical release
   gate plus exactly like the canonical reconcile result: the result must
   equal canonically the expected reconciliation payload (fixed
   non-success derivation, `started_at` from the durable start,
   `finished_at`/digests from the signed proof, bound generation) —
   an authenticated tamper of any other field refuses `verify-failed` —
   plus a `lenovo-job-release/1` binding generation/owner/job digest/
   resources/result digest.
7. Idempotency: `--invoke` first re-validates an existing settlement
   against the signed proof and reports `reconciled` with `"replay": true`
   without spawning; a missing/unverifiable proof is not a replay. Otherwise
   it reuses a still-valid stored proof so the canonical worker takes its
   `skipped + matched` path. A fresh proof against an existing different
   result is refused by the canonical worker (conflict) and reported as
   `reconcile-failed` — never overwritten or resubmitted.

Modes: `--verify-only` (authenticate + gates, write nothing, spawn
nothing), default dry-run (gates + write proof), `--invoke` (full path +
verify), `--self-test` (fixture-only checks, no config/key/queue access).
Exits: `0` ready/reconciled, `2` refused/failed.

Receipts are single JSON lines on stdout (`lenovo-job-recovery/1`)
carrying only digests/pids/reasons: no key, no password, no host, no
absolute path. Every external error is passed through path sanitization
before it reaches a receipt or stderr, so OS messages that embed absolute
paths (config/key/proof/interpreter locations) are redacted.

## Use on Lenovo (owner at the keyboard, live queue)

1. Copy `scripts\lenovo_worker\job_recovery\` to Lenovo; rebuild with
   `build-go.bat` (or trust the shipped exe after checking its SHA-256
   against the task report).
2. Dry-run first (no queue write except the proof file outside the queue):
   `LenovoJobRecovery.exe --job-id <id> --verify-only`
   then `LenovoJobRecovery.exe --job-id <id>` (writes
   `%TEMP%\lenovo-recovery\<id>.proof.json`).
3. Settle: `LenovoJobRecovery.exe --job-id <id> --invoke`. Expect `outcome:
reconciled`.
4. Re-run `--invoke` with the same `--job-id` to confirm idempotent
   `replay: true`.
5. Verify the same id with the bridge `fetch` (result `error`/`unknown`).

Do NOT delete/re-submit the job. Validate on a TEST queue per
`docs/lenovo-worker-shortcut.md` before the live id. Never pass the key
anywhere; the exe reads it only via the config.

## Local verification (fixtures only — live queue untouched, no secrets)

```text
<python> -m unittest tests.lenovo_worker.test_job_recovery_exe -v
```

Covers: exe `--self-test` 13/13 (incl. canonical-exact tamper refusal,
path sanitization, strict base64); `--job-id` required (omitted/garbage
refuses `job-id-invalid`, writes nothing); `--verify-only` + dry-run proof
that the canonical bridge opens (schema v2, generation/resources/typed
evidence bindings) and the canonical worker settles to the truthful
non-success with a fully-bound release; `--invoke` reconciles, re-invoke
is `replay: true` with unchanged bytes, same-proof canonical re-accept
exits 0; authenticated result tamper refuses `verify-failed` (never
replay); proof-write OS errors and UNC key refusals carry no absolute
paths; live-owner refuses with no result file; UNC queue passes the
config gate and refuses as `queue-missing`, key inside the share and UNC
keys refuse as `key-misplaced` (key stays local).
