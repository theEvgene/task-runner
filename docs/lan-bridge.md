# Signed LAN command bridge

Agent-operable signed route for one command to a second host over a shared
queue directory. The command travels inside the HMAC-signed job payload (not
as a correlation id); the result is verified by MAC and bound to the requested
job id. The queue is immutable: a published id is never replaced.

Canonical client: `scripts/lan_bridge/lan_bridge.py` (stdlib only; protocol
v1, no crypto change from the reviewed MVP revision). Recovery helpers with
no protocol change live in `scripts/lan_bridge/wait.py`: windowed resume
(`wait_many_windows`), non-blocking verified read (`peek_result`),
confirmed-state classification (`classify_result`,
`classify_absence`, `summarize_result`) and stage timings (`stage_timings`).
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
  [--return-file <path-on-worker> ...]
```

Exit `2` refuses without executing: empty command, bad key, key inside the
queue or a repository, duplicate id, unreadable command file.

2. Fetch the bound result (default: wait up to 240 s, checking every 30 s;
   owner choice from #12/#13; explicit values stay allowed):

```text
python scripts/lan_bridge/lan_bridge.py fetch \
  --queue <queue-dir> --key-file <key-path> \
  [--key-format raw|base64] \
  --id <job-id> [--timeout <seconds>] [--poll <seconds>] \
  [--receipt-out <path>]
```

Prints the result payload JSON only after its MAC verifies **and** its
`kind`/`id` equal `result`/`<job-id>` (binding before trust: a valid envelope
for another id never verifies as this one). Exit `0` verified, `2`
verification/usage failure, `3` bounded-wait timeout with the slot left
untouched. `--timeout` here is the _result wait_; `submit --timeout` is the
separate _remote execution limit_. An explicit short `--timeout` is allowed
but proves nothing: expiry always means **unknown**, never "still running".

3. Recover after any interruption with the SAME id (no resubmission):

```text
python scripts/lan_bridge/lan_bridge.py fetch \
  --queue <queue-dir> --key-file <key-path> --id <same-job-id> \
  --timeout 240 --poll 30
```

Recovery is stateless (queue files only, no observer memory), so a restarted
observer simply fetches the same id again. A late result sealed under the
original id verifies normally. Never re-publish while the outcome is
unknown: the queue refuses duplicates (`JobExistsError`), and a second submit
would be a NEW id, not a retry.

4. Keep observing the SAME id until terminal or blocked (no hand-written
   re-poll loop):

```text
python scripts/lan_bridge/get.py \
  --queue <queue-dir> --key-file <key-path> \
  [--key-format raw|base64] \
  --id <job-id> [--window <seconds>] [--poll <seconds>] \
  [--max-windows <n>] [--receipt-out <path>]
```

One call holds the validated id/key/queue across windows (a single queue
resolve however many windows it takes) and returns only when a MAC-verified
result bound to the id is found or a concrete blocker appears (exit `2`:
unavailable queue, tampered envelope, bad id). `--window` defaults to 60 s,
`--poll` to the owner default 30 s; `--max-windows 0` (the default) waits
without a window bound, otherwise expiry is exit `3` unknown with the same
same-id resume action. Interruption recovery is re-running the same command
with the same id: nothing is ever published, so a late result sealed under
the original id verifies normally.

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
  with no result file yet stays pending (exit `3` unknown, re-read SAME id).
- No live second host is proven by shipping this client: the fixture suite
  below is local only. A live run is a separate, explicitly authorized step.

## Confirmed states vs unknown

Only a MAC-verified envelope bound to the requested id counts as evidence,
and only for what its fields say (`classify_result`):

- `completed`/`ok` + integer `exit_code == 0`, no `timed_out`, no
  `output_truncated` → terminal **success**.
- Non-zero exit → terminal **worker failure** (verified, not success).
- `timed_out` → terminal **worker timeout**; fix worker limits, re-run as a
  NEW id.
- `output_truncated` → terminal but **incomplete**; the CLI warns on stderr
  (exit stays `0`: the envelope verified). Re-run as a NEW id with larger
  `--max-output`.
- Any other `status` → verified envelope, **unknown meaning**; never treated
  as success.

No verified result yet (bounded wait expired, exit `3`) is **unknown**: not
evidence the job is running, done, or failed. The timeout message says so and
points at re-reading the SAME id. There is no "running" outcome anywhere in
the client or `wait.py`.

## Timings without chatter

`fetch` prints one `elapsed/polls/outcome` line to stderr at the end -- no
per-poll log. `wait_many_windows` reports `windows_used`, an exact poll
upper bound, and `elapsed_s`, calling its hook at most once per window.
Stage splits (`stage_timings`) use numeric worker marks only; the protocol's
opaque `started_at`/`finished_at` strings (e.g. fixture `"t0"`/`"t1"`) yield
`unknown` for every stage rather than an invented split.

## Delay diagnosis (measured, local fixtures)

Dominant delay is worker execution plus queue wait, both outside this
client: an already-present result costs exactly one queue read and returns
sub-second (asserted in `LatencyDiagnosisTests`). Per empty poll the client
spends one directory probe. The owner default (240 s / 30 s) therefore cuts
empty-poll probes ~3x versus the previous 660 s / 10 s default for the same
wait (at most 10 reads per full window versus 67), at the price of a 30 s
detection-latency bound instead of 10 s -- an explicit trade-off; a shorter
wait can still be chosen per call with `--poll`. No deeper client-side cut
was made: slicing the 30 s sleep into hidden 1 s probes would silently
redefine the owner-chosen check interval, so it stays out of scope under
#12/#13. Protocol, MAC, id binding, immutability and key handling are
unchanged.

Measured this round (local fixture only: window 0.4 s / poll 0.05 s, late
result sealed after two empty windows; reproduce with
`python -m unittest discover -s tests/lan_bridge -p
test_lan_bridge_get_result.py`, probe counting in `BeforeAfterTests`):

| metric                              | before: re-invoked `fetch` per window (2 calls) | after: one `get_result`  |
| ----------------------------------- | ----------------------------------------------- | ------------------------ |
| queue-dir resolves                  | 2 (one per call)                                | 1 (validated once)       |
| queue reads to the same late result | 10                                              | 19                       |
| elapsed                             | 0.42 s                                          | 0.82 s                   |
| already-present result              | 1 read, sub-second                              | 1 read, 0.008 s observed |

Reads and elapsed scale with when the late result arrives in both paths, so
the before/after gap there is fixture arrival time, not mechanism speed, and
is reported as such rather than as an optimization win. The deterministic
saving is the per-window setup: one validation + one queue resolve per
`get_result` however many windows it observes, versus one per `fetch` call.
Dominant delay stays worker-side (execution + queue wait): an
already-present result costs exactly one read in both paths. Stage splits
(`stage_timings`) still use only confirmed numeric worker marks -- opaque
protocol strings yield `unknown` for every stage, and `queue_s`/`delivery_s`
have no confirmed marks at all, so they stay `unknown` here. No live second
host is proven by any of these numbers.

## Local verification

```text
<python> -m unittest discover -s tests/lan_bridge -v
```

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
original id with exactly one submission, bounded expiry as unknown plus
restart recovery with the same id, tamper/unavailable/bad-id fast blockers,
verified-but-unrecognized status returned as confirmed-yet-unknown (never
success), the single-resolve before/after fixture, and the one-probe
immediate-result assertion. No second host is ever reached.
