# Selected LAN scenario readiness

Agent entry: use `scripts/readiness/check.py` before starting either a raw
signed LAN command (`lan-bridge`) or the full Dell-to-Lenovo publication loop
(`dell-lan`). It combines Dell-local prerequisite checks with one read-only
Lenovo probe. It does not install, configure, publish, contact a corporate
service, or prove more than the named checks.

For exactly one short command on an explicit host after readiness, use
`docs/selected-check.md` (`scripts/readiness/selected.py`); it reuses this
environment model and the same signed queue, adds the command/cwd/revision and
real-exit receipt, and is not a second runner.

## Choose the scenario

- `lan-bridge` proves that the Dell queue and key are usable and that a
  worker returned one MAC-bound result. Add every worker-side file or tool
  required by the intended command to the configuration.
- `dell-lan` additionally requires explicit local and Lenovo component lists.
  It checks files and
  executable discovery. The final
  `corporate-access` condition remains `unconfirmed`: file or credential
  presence is not proof that the corporate service is reachable. Its next
  action is a separately authorized live preflight on Lenovo.

Do not select `dell-lan` for a base-only import or a pipeline observation;
those have their own entry documents in `AGENTS.md`.

## Owner-provided input

Put a JSON file outside every checkout. Paths below are placeholders; never
commit a real queue, key, share, checkout, or corporate path.

```json
{
  "schema": "lan-readiness-config/1",
  "scenario": "lan-bridge",
  "queue": "<queue-dir>",
  "keyFile": "<key-file>",
  "keyFormat": "raw",
  "local": {
    "components": [],
    "tools": [{"name": "git", "command": "git"}]
  },
  "lenovo": {
    "python": "<absolute-python-on-lenovo>",
    "cwd": "<existing-working-directory-on-lenovo>",
    "components": [
      {"name": "driver", "type": "file", "path": "<driver-on-lenovo>",
       "sha256": "<64-lowercase-hex>"}
    ],
    "tools": [{"name": "git", "command": "git"}]
  }
}
```

Components have `type: file|directory`. A file may include its expected
SHA-256; a different digest is `stale`, not success.

For `dell-lan`, list at least the Dell share/checkout and the Lenovo
task-runner driver/publication-flow paths as components. The configuration is
the complete selected checklist: omitted optional needs cannot be inferred.

## Run and recover

Explicit authorization applies to this one read-only Lenovo probe:

```text
<python> scripts/readiness/check.py --config <outside-config.json> \
  --authorize-lenovo-check --wait 600 --poll 30 [--worker-timeout 60]
```

Live `--wait 600 --poll 30` flags are mandatory; code defaults
(`--wait 240`, `--poll 30` owner default #12/#13) are fixture-oriented and
MUST NOT be relied on live. A shorter explicit live wait is a bounded probe
only: its expiry is unknown with the same same-id resume, never completion.

The command performs all Dell checks first. A local failure publishes
nothing. After those pass it uses the canonical `scripts/lan_bridge` client
to publish exactly one signed job with a unique ID and to verify the result's
MAC, `kind`, ID, status, exit code, worker timeout, and truncation flags.

Exit `0` is `ready`; exit `1` is a verified `not-ready` report containing a
`missing`, `stale`, or `unconfirmed` condition and its next action; exit `2`
is a policy/configuration/protocol refusal (including a bad MAC or an
unverifiable job request); exit `3` is `waiting` or `unknown` because no
verified result arrived during the bounded wait.

On exit `3`, copy `jobId` from the JSON and retrieve that exact ID:

```text
<python> scripts/readiness/check.py --config <same-config.json> \
  --job-id <same-32hex-id> --wait 600 --poll 30
```

The retrieval form never submits. Do not create a new job WHILE the prior
outcome is unknown. A short-timeout expiry is unknown, never completion;
report `no response` only after a full 600 s watch at a 30 s poll with
`elapsed_s >= 600` cited from the waiter report
(`docs/lan-bridge.md` "Observation rule").

## Output and boundaries

The JSON report is `lan-readiness-report/1` and contains the scenario, job
ID, overall outcome, named component states, and next action. Whenever a job
id was read, its `status` member is the same `lan-job-status/1` object
`wait.py`/`get.py --status` emit, so job state is never restated in a second
format; the bounded-wait report's overall `outcome` mirrors
`status.outcome`. A report produced before submission (a failed local check)
has no `status` member because no job was read. A directory component also
carries `observedRevision` and `revisionEvidence` (`verified`, `legacy`, or
`untrusted`); these are evidence for the selected check and never change this
report's readiness outcome. It never emits
the key, command, configured paths, queue contents, or worker stderr. Parallel
runs have independent random IDs and the canonical queue's immutable,
no-clobber files; no shared receipt or observer state is used.

A local failure report also carries one `reason` code and one bounded
`nextAction` from the same classifier the Lenovo launcher uses. Only local
codes can appear here (`queue-missing`, `key-unusable`, `tool-missing`,
`component-unconfirmed`, `preflight-incomplete`); no SMB or launch code is
emitted by this path, and `reason` is a preflight field only: once a job id
was read, that job's state comes from its `status` member alone. That path
publishes no signed job: an uncertain preflight never submits. A generic
share/path failure is never reported as bad credentials, and contradictory
observations are `unknown` with one next action.

The Lenovo probe is fixed read-only code: filesystem metadata/digests and
executable lookup. It has no network
operation. A returned result proves that the worker executed this job; mere
presence of a worker file never counts as worker readiness. Live Lenovo and
corporate checks remain separate authorized operations. For every configured
directory component the probe also reads `git rev-parse HEAD` and emits that
40-hex SHA together with a digest of the component path
(`lenovo-readiness-probe/2`). A directory that is not a readable git checkout
reports a null revision. Revision is not a readiness gate and never changes
`ready`/`not-ready` here; `docs/selected-check.md` consumes it as a
pre-submit pin. Evidence from the older `lenovo-readiness-probe/1` is still
accepted here for presence, but it carries no revision and is never verified
revision evidence for a selected check.

## Local fixture verification

```text
<python> -m unittest discover -s tests/readiness -p "test_*.py" -v
```

The suite uses temporary files, an ephemeral fake key, and a fake worker. It
covers success, a stale component, bad MAC, timeout followed by same-ID
retrieval, and concurrent calls, plus an uncertain preflight that reports one
path-free reason and publishes no signed job. It performs no network or
corporate access.
