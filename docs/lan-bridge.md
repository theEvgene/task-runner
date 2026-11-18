# Signed LAN command bridge

Agent-operable signed route for one command to a second host over a shared
queue directory. The command travels inside the HMAC-signed job payload (not
as a correlation id); the result is verified by MAC and bound to the requested
job id. The queue is immutable: a published id is never replaced.

Canonical client: `scripts/lan_bridge/lan_bridge.py` (stdlib only; protocol
v1, no crypto change from the reviewed MVP revision). Recovery helpers with
no protocol change live in `scripts/lan_bridge/wait.py`: windowed resume
(`wait_many_windows`), non-blocking verified read (`peek_result`), the one
job-state readback (`job_status`, `status_from_result`,
`status_for_absence`), confirmed-state classification (`classify_result`,
`classify_absence`) and stage timings (`stage_timings`). The older
`summarize_result` projection was removed so one job has one state shape.
Nothing here reimplements the envelope, the MAC, or the queue; scenario docs
(`docs/fresh-base.md`, `docs/pipeline-observer.md`, `docs/dell-lan.md`)
build their exact commands and call this client. The second agent operation
-- keep observing one id across short windows until a confirmed terminal
result or a concrete blocker -- is `scripts/lan_bridge/get.py` (stdlib only,
same protocol; it calls `wait.get_result` and never publishes).

## Required inputs (owner-provided, never invented)

Queue dir plus a 32-byte key file (raw bytes, or base64 text with
`--key-format base64`). Both live outside every repository, outside the queue
itself, and outside any shared LAN folder. The worker on the other host is
already running and was started by its owner.

Paths interpolated into a signed command must not contain `"`, `$`,
backtick, or `%` (PowerShell/cmd expansion); spaces are fine.

## Exact commands

All paths below are placeholders.

1. Submit one command (prints `JOBID=<32hex>`):

```text
python scripts/lan_bridge/lan_bridge.py submit \
  --queue <queue-dir> --key-file <key-path> \
  [--key-format raw|base64] \
  --command-file <cmd-file> --cwd <working-dir-on-worker> \
  --timeout <seconds> --max-output <bytes> \
  [--return-file <path-on-worker> ...] \
  [--dialect powershell|cmd] \
  [--receipt-script-path <absolute-intended-script>] \
  [--expected-cwd <absolute-working-dir-on-worker>] \
  [--claim <resource> ...]
```

`--expected-cwd` is the producer's declared target: it travels inside the
same MAC, and the worker refuses a job whose `--cwd` differs from it before
claiming or spawning anything (`target-cwd-mismatch`, sealed terminal error,
path identities only as fingerprints). Omitting it never asserts a match: the
job stays `unverified-target` and the worker log says so, while the command
still runs exactly as before. Only a caller that can pin the target should
declare it.

Exit `2` refuses without executing: empty command, bad key, key inside the
queue or a repository, duplicate id, unreadable command file, and -- when
`--dialect` is given -- a syntax refusal or an unavailable syntax
validator (see [the submit syntax gate](lan-bridge-syntax-gate.md); without
`--dialect` the gate stays off and the submit path is unchanged).

2. Fetch the bound result (live callers MUST pass explicit `--timeout 600`
   `--poll 30`; defaults are fixture-only):

```text
python scripts/lan_bridge/lan_bridge.py fetch \
  --queue <queue-dir> --key-file <key-path> \
  [--key-format raw|base64] \
  --id <job-id> --timeout 600 --poll 30 \
  [--receipt-out <path>]
```

Live flags above are mandatory. Code defaults (`--timeout 240`, `--poll 30`,
owner default #12/#13) are fixture-only; the 600 s live watch is task policy
scoped below that default, with no script change.

Prints the result payload JSON only after its MAC verifies **and** its
`kind`/`id` equal `result`/`<job-id>` (binding before trust: a valid envelope
for another id never verifies as this one). Exit `0` verified, `2`
verification/usage failure, `3` bounded-wait timeout with the slot left
untouched. `--timeout` here is the _result wait_; `submit --timeout` is the
separate _remote execution limit_. A shorter explicit live `--timeout` is
a bounded probe only: its expiry is **unknown** with the same same-id resume
action, never completion and never a license to publish a new id.

3. Recover after any interruption with the SAME id (no resubmission):

```text
python scripts/lan_bridge/lan_bridge.py fetch \
  --queue <queue-dir> --key-file <key-path> --id <same-job-id> \
  --timeout 600 --poll 30
```

Recovery is stateless (queue files only, no observer memory), so a restarted
observer simply fetches the same id again. A late result sealed under the
original id verifies normally. A short-timeout expiry stays **unknown** and
never counts as completion. Never re-publish while the outcome is
unknown: the queue refuses duplicates (`JobExistsError`), and a second submit
would be a NEW id, not a retry.

4. Keep observing the SAME id until terminal or blocked (no hand-written
   re-poll loop):

```text
python scripts/lan_bridge/get.py \
  --queue <queue-dir> --key-file <key-path> \
  [--key-format raw|base64] \
  --id <job-id> --window 60 --poll 30 --max-windows 10 \
  [--status] [--receipt-out <path>]
```

Primary live pattern: ten `--window 60 --poll 30` windows via
`--max-windows 10` (600 s at a 30 s poll, one bounded call per window); sum
`windows_used` x window (or `elapsed_s`) to 600. A single
`--window 600 --poll 30` call only where the harness holds one 600 s call.
Defaults are fixture-oriented and MUST NOT be used live.

One call holds the validated id/key/queue across windows (a single queue
resolve however many windows it takes) and returns only when a MAC-verified
result bound to the id is found or a concrete blocker appears (exit `2`:
unavailable queue, tampered envelope, bad id, unverifiable request; see
`lan-job-status/1` below). Bounded expiry is exit `3` `waiting`/`unknown`
with the same same-id resume action. Interruption recovery
is re-running the identical command with the identical id: nothing is ever
published, so a late result sealed under the original id verifies normally.
`--status` skips the wait and prints one `lan-job-status/1` readback (exit `0` finished, `3`
waiting/unknown, `2` refused); a bounded timeout prints that same object on
stdout.

## Observation rule (same-id 10-minute watch)

After `submit`, observe the SAME JOBID every 30 s for 10 minutes until a
MAC-verified result bound to that id or a concrete blocker (exit `2`)
arrives. A short-timeout expiry is **unknown**, never completion, and WHILE
the outcome is unknown there is never permission to publish a new JOBID for
the same work. A verified terminal worker timeout, truncation, or failure
is a known outcome: recover as a NEW id per Confirmed states below. Report `no response` only after the full 600 s watch: cite
`elapsed_s >= 600` (or `windows_used` x window >= 600) at a 30 s poll from
the waiter report; a shorter expiry is `waiting`/`unknown` with the same
same-id resume, never `no response`. State the `lan-job-status/1` fields
verbatim with the next same-id action: confirmed error = `finished` with
`terminal.outcome` in worker failure, worker timeout, truncation,
unknown status; unavailability = `waiting`/`unknown`/`refused`. Classify via
`classify_result` / `job_status` in `scripts/lan_bridge/wait.py`, never from
stderr text.

## Syntax gate (syntax-only, opt-in per submit)

With `--dialect powershell|cmd`, `submit` checks the command syntax before
seal/publish through `scripts/lan_bridge/syntax.py::validate`: a refusal
prints a secret-free reason, exits `2`, and leaves no JOBID and no file
under `jobs/`; a pass continues down the unchanged path. Without
`--dialect` the gate stays off. The gate is syntax only (never "exists" or
"safe") and fails closed when its validator cannot run.

Launcher shapes, the accepted `cmd` subset, and diagnostics are specified
in [the submit syntax gate](lan-bridge-syntax-gate.md).

## Managed-write claims (opt-in per submit)

`submit --claim <resource>` (repeatable; `submit_job(...,
resource_claims=)`) declares the job's explicit managed-write footprint and
enables the authoritative reserved parallel path for that job only. Two
producers cannot both admit the same or a nested footprint, and a conflict
refuses with exit `2` before anything is published. `capacity:<name>` is
the only shared claim: its quota comes from `queue/capacity-quotas.json`,
never from an inference or a per-call ceiling.

Claim normalization, the one queue-local quota authority, and the
proof-bound release are specified in [managed-write claims and the
reservation referee](lan-bridge-claims.md). Without `--claim` nothing
changes: one direct immutable publish, the worker's `exclusive:global`
fallback, and sequential execution.

## Failure diagnosis

For a stuck or failed job, follow `lan-queue-diagnosis.md` (same document
directory): same id every 30 s for 10 min, timeout stays unknown,
per-stage user updates. From the repository root this is
`docs/lan-queue-diagnosis.md` (entry: `AGENTS.md`).

## Boundaries

- One command per job, one working directory, one timeout, bounded
  stdout/stderr, declared return files. The bridge never uploads repository
  content by itself; jobs carry commands, and small result files ride back
  only when declared with `--return-file`.
- The key travels through `--key-file` only. It is never printed and never
  lands in any receipt. CLI errors echo only the job id and wait parameters.
- A missing key, a key inside the queue or a repository, an empty command, a
  malformed envelope, a MAC mismatch, or an id/kind mismatch all refuse
  without execution and without a second route.
- A missing queue dir (wrong path, unmounted share) refuses at once with
  exit `2` (`queue unavailable ... check path/mount`): it never waits the
  full timeout and never claims the job is pending. A present queue dir
  with no result file yet is `waiting` when a signed lease holds the job,
  otherwise `unknown`; both are exit `3` with the same-id resume action.
- No live second host is proven by shipping this client: the fixture suite
  below is local only. A live run is a separate, explicitly authorized step.

## Confirmed states vs unknown

Only a MAC-verified envelope bound to the requested id counts as evidence,
and only for what its fields say (`classify_result`):

- `completed`/`ok` + integer `exit_code == 0`, no `timed_out`, no
  `output_truncated` → terminal **success**.
- `completed`/`ok` + non-zero `exit_code` → terminal **worker failure**
  (verified, not success). A verified `error`-status envelope (the worker
  seals any nonzero process exit this way) is **unknown_status**, never
  success by itself: the Jira/DCO receipt-authoritative route in
  `docs/lenovo-hosted-scripts.md` still parses its complete authenticated
  stdout receipt, and only a `Completed` receipt counts as success.
- `timed_out` → terminal **worker timeout**; fix worker limits, re-run as a
  NEW id.
- `output_truncated` → terminal but **incomplete**; the CLI warns on stderr
  (exit stays `0`: the envelope verified). Re-run as a NEW id with larger
  `--max-output`.
- Any other `status` → verified envelope, **unknown meaning**; never treated
  as success.

No verified result yet (bounded wait expired, exit `3`) is `waiting` when a
signed lease holds the job and `unknown` otherwise: neither is evidence the
job is running, done, or failed. The timeout message says so and points at
re-reading the SAME id. There is no "running" outcome anywhere in the client
or `wait.py`.

## One verified job status (`lan-job-status/1`)

`wait.py:job_status` is the single job-state readback; `get.py --status`
prints it and readiness embeds the same object instead of restating job
wording. One shape, one id:

| field | present | meaning |
|---|---|---|
| `jobId` | always | the exact id that was read |
| `outcome` | always | `finished`, `waiting`, `unknown` or `refused` |
| `nextAction` | always | at most one safe next action |
| `proof` | always | `result`/`job`/`lease`, each `signed`, `absent`, `unverifiable` or `not read` |
| `observedOwner` | signed claim bound to this job only | claim `instance` and `pid` (ownership, not liveness) |
| `occupied` | with `observedOwner` only | the signed lease still holds the job |
| `deadline` | from the signed request only | `timeout_seconds` (worker execution limit), never an invented clock |
| `terminal` | `finished` only | `classify_result` outcome, status and exit code |

- `finished`: MAC-verified result bound to this id; `terminal.outcome` keeps
  success apart from worker failure, worker timeout, truncation and unknown
  status.
- `waiting`: no result, but a signed `lenovo-job-claim/1` bound to this exact
  job envelope and to the held state. The claim is checked with the same
  `job_claim.claim_matches` predicate the owning worker applies (schema,
  kind, id, owner types, 32-hex generation, digest, state), so the observer
  cannot report an owner the worker would refuse. The worker-derived resource
  set is not re-derivable on the observing host and stays the owner's final
  check. The lease stays held; `nextAction` re-reads the SAME id and never
  resubmits or releases it. Nothing here frees a lease on a waiter timeout;
  that stays a separate, unshipped change.
- `unknown`: no result and no signed lease, or no signed request for the id.
  Never "running"; `nextAction` never resubmits.
- `refused`: a result file exists but fails its MAC, id or format check (a
  result sealed for another id is refused, never reported as this job's
  completion), or the signed request itself cannot be verified (a tampered
  or foreign-id request file is untrusted, not the absent-id `unknown`; no
  re-read can repair it). The single action asks the queue owner for the
  sealed file. An unavailable queue still refuses fast and names no host
  path.

The waiter reports (`wait_many_windows`, `get_result`, `get.py`) keep
`job_id`, `windows_used`, `probes`/`polls_bound`, `elapsed_s` and `action`,
and carry that same object as `status`; the report's own `outcome` mirrors
`status.outcome` on a non-found expiry, so one report never states two job
states. A found result keeps `outcome: found` and `status.outcome:
finished`.

Executor truth (issues #33/#36): on the worker, a PowerShell launcher
command is replaced by an already-valid encoded outer program. That program
decodes the inner command as data, parses it with `ScriptBlock.Create` inside
an established `try` boundary, and invokes it with
`$ErrorActionPreference='Stop'`, preserving a native exit with
`exit $LASTEXITCODE` (`scripts/lenovo_worker/powershell_truth.py`). A parse
error or a swallowed non-terminating error therefore surfaces as a non-zero
executor exit. The envelope verdict is the exit code only; `stderr`
text never changes it in either direction. Only the submit-gate dialect
`powershell -Command "<script>"` (or a bare unquoted script) is wrapped;
any other launcher shape refuses with a non-zero exit rather than running
unwrapped. Executable-prefix indirections are classified recursively:
`call powershell`, `@powershell`, their combinations, and `cmd /c` or `/k`
payloads (including `/d /s /c` canonical outer quoting and quoted executable
paths) refuse rather than execute unwrapped. Structurally identical chains
whose executable is not PowerShell still follow the ordinary cmd path.
Ambiguous cmd switch prefixes fail closed when their payload is PowerShell.
If the wrapping helper is missing or its classifier breaks, the independent
`cmd_fallback.py` boundary classifier still refuses PowerShell behind chains,
`cmd /c`, `call`, `start`, or `if`. Unbalanced, compound, batch, `for`, and
dynamic executable boundaries also fail closed; statically simple non-PowerShell
commands keep the ordinary cmd path.

Protocol scope: an explicit `--dialect` is stored inside the signed
`lenovo-job/1` payload and drives worker dispatch. Old queued jobs without the
optional field keep the conservative classifier for compatibility. A `cmd`
job whose executable token resolves to PowerShell (including inline cmd quotes
such as `po"wer"shell.exe`) refuses rather than crossing into an unwrapped
dialect; text after an ordinary executable, such as
`echo powershell documentation`, remains ordinary cmd. The executor does not
claim to inspect commands later chosen inside an arbitrary batch file or
native program.

For a job whose result will be accepted as publication evidence,
`--receipt-script-path` seals the absolute intended Python script path. The
readback gate compares that field with the command's actual script argv using
Windows path normalization and exact equality; the same basename under an
attacker directory is not evidence. Old jobs without this optional field can
still execute, but cannot establish a publication receipt.

## Timings without chatter

`fetch` prints one `elapsed/polls/outcome` line to stderr at the end -- no
per-poll log. `wait_many_windows` reports `windows_used`, an exact poll
upper bound, and `elapsed_s`, calling its hook at most once per window.
Stage splits (`stage_timings`) use numeric worker marks only; the protocol's
opaque `started_at`/`finished_at` strings (e.g. fixture `"t0"`/`"t1"`) yield
`unknown` for every stage rather than an invented split.

## Delay diagnosis

See [local fixture measurements](lan-bridge-delay-diagnosis.md).

## Local verification

```text
<python> -m unittest discover -s tests/lan_bridge -v
```

Includes `test_lan_bridge_status.py` for verified job state and claim evidence.
Covers the published interop vector byte-for-byte, MAC tamper and wrong-id
refusal, command-in-payload transmission, result id binding,
duplicate-publish refusal, key handling, a fixture submit/worker/fetch
round trip (the fake worker seals a canned result; the managed-command bytes
in that test are opaque example data), plus the recovery suite
(`test_lan_bridge_recovery.py`): late result after several windows and a CLI
observer restart with exactly one submission, confirmed-vs-unknown
classification, window reports with `unknown` stages, tamper/bad-id/
truncated/unavailable handling with no key material in any output, and the
poll-bound/latency assertions above, plus the continuous get-result suite
(`test_lan_bridge_get_result.py`): late result across windows under the
original id with exactly one submission, bounded expiry as waiting or unknown plus
restart recovery with the same id, tamper/unavailable/bad-id fast blockers,
verified-but-unrecognized status returned as confirmed-yet-unknown (never
success), the single-resolve before/after fixture, and the one-probe
immediate-result assertion. The syntax-gate suites
(`test_submit_syntax_gate.py`, `test_powershell_syntax.py`) are described in
[the submit syntax gate](lan-bridge-syntax-gate.md). No second host is ever
reached.
