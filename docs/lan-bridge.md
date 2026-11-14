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
  [--return-file <path-on-worker> ...] \
  [--dialect powershell|cmd] \
  [--receipt-script-path <absolute-intended-script>]
```

Exit `2` refuses without executing: empty command, bad key, key inside the
queue or a repository, duplicate id, unreadable command file, and -- when
`--dialect` is given -- a syntax refusal or an unavailable syntax
validator (see "Syntax gate" below; without `--dialect` the gate stays
off and the submit path is unchanged).

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

## Syntax gate (syntax-only, opt-in per submit)

With `--dialect powershell|cmd`, `submit` checks the command syntax
before seal/publish through the small seam
`scripts/lan_bridge/syntax.py::validate(command, dialect)`. The check
point sits in `submit_job` ahead of JOBID generation: a refusal prints
the reason to stderr, exits `2`, and leaves no JOBID and no file under
`jobs/`. A pass continues down the unchanged
seal/publish/MAC/immutability path.

- Explicit dialect, no guessing. The dialect must be `powershell` or
  `cmd`; anything else refuses naming the explicit choice. Without
  `--dialect` the gate is off and the submit path is unchanged.
- Worker shell compatibility. The worker executes through the platform
  shell (`shell=True`; `COMSPEC`/`cmd.exe` on Windows), so a bare
  PowerShell line is not portable. `--dialect powershell` requires an
  explicit launcher: `powershell -Command <script>` (or
  `powershell.exe`/`pwsh`, with optional launcher flags). A bare line
  such as `& '...' ...` refuses as ambiguous even when its PowerShell
  syntax is valid; wrap it as
  `powershell -NoProfile -Command "& '...' ..."`. Only one `-Command`
  script argument is accepted: no unquoted cmd operator
  (`| & < > ^ ( )`) may sit outside a double-quoted script, no cmd
  token may follow its closing quote, and `-File`/other launcher shapes
  refuse. This is a launcher-shape check, not a full cmd parse of the
  launcher.
- `powershell` runs `Parser.ParseInput` (AST + `Token[]` +
  `ParseError[]`) on the inner `-Command` script; a syntax error
  refuses with the `powershell` dialect, a secret-free parser reason,
  and the source offset within the submitted command file. `cmd` is a
  deliberately narrow subset: one command line, balanced double quotes,
  no NUL, and no unquoted `| & < > ^ ( )` operator. That refuses
  pipelines, redirection, compounds, and subshells, so malformed forms
  such as `echo hi |` refuse; it is not a full cmd grammar check.
- Syntax only. A pass says nothing about the runtime: cmdlet/path
  existence, execution policy, providers, rights, and the network are
  not checked. A valid line naming a nonexistent cmdlet or path passes,
  and a pass must never be read as "exists" or "safe". The gate is not
  a security sanitizer or an allow-list.
- Fail-closed when active. If the validator cannot run (no
  `pwsh`/`powershell` runtime, nonzero parser-probe exit even with
  output, or unreadable probe output), submit refuses before any write
  instead of skipping the check. The operator may pin or blank
  discovery with `TASK_RUNNER_POWERSHELL_BIN` (a path, or empty to
  force unavailable). Without `--dialect` the gate stays off and the
  submit path keeps its legacy behavior.
- Diagnostics never carry secrets: only the dialect, a secret-free
  reason (parser free text and caught-exception text pass through a
  redactor that removes command-derived tokens of any length, including
  short ones, and truncates), and the offset, plus the usual call
  parameters. Command text, key material and queue contents are never
  printed.

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
immediate-result assertion. The syntax-gate suites are
`test_submit_syntax_gate.py` (seam via stub adapters, ambiguous dialect,
fail-closed validator, secret-free diagnostics, legacy pass-through) and
`test_powershell_syntax.py` (`Parser.ParseInput` refusals with offset, the
wrapped `& '...'` shape, launcher-boundary refusals including an external
cmd operator, the nonexistent-cmdlet negative case, nonzero-probe
fail-closed, and a missing PowerShell backend). No second host is ever
reached.
