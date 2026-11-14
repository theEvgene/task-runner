# Selected LAN scenario readiness

Agent entry: use `scripts/readiness/check.py` before starting either a raw
signed LAN command (`lan-bridge`) or the full Dell-to-Lenovo publication loop
(`dell-lan`). It combines Dell-local prerequisite checks with one read-only
Lenovo probe. It does not install, configure, publish, contact a corporate
service, or prove more than the named checks.

## Choose the scenario

- `lan-bridge` proves that the Dell queue and key are usable and that a
  worker returned one MAC-bound result. Add every worker-side file or tool
  required by the intended command to the configuration.
- `dell-lan` additionally requires explicit local and Lenovo component lists
  and the expected Lenovo task-runner checkout revision. It checks files,
  executable discovery, and the local Git revision. The final
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
SHA-256; a different digest is `stale`, not success. A `dell-lan` input also
includes this object inside `lenovo`:

```json
{
  "git": "git",
  "revision": {
    "name": "task-runner-revision",
    "repository": "<task-runner-checkout-on-lenovo>",
    "expected": "<40-lowercase-hex>"
  }
}
```

For `dell-lan`, list at least the Dell share/checkout and the Lenovo
task-runner driver/publication-flow paths as components. The configuration is
the complete selected checklist: omitted optional needs cannot be inferred.

## Run and recover

Explicit authorization applies to this one read-only Lenovo probe:

```text
<python> scripts/readiness/check.py --config <outside-config.json> \
  --authorize-lenovo-check [--wait 240] [--poll 30] [--worker-timeout 60]
```

The command performs all Dell checks first. A local failure publishes
nothing. After those pass it uses the canonical `scripts/lan_bridge` client
to publish exactly one signed job with a unique ID and to verify the result's
MAC, `kind`, ID, status, exit code, worker timeout, and truncation flags.

Exit `0` is `ready`; exit `1` is a verified `not-ready` report containing a
`missing`, `stale`, or `unconfirmed` condition and its next action; exit `2`
is a policy/configuration/protocol refusal (including a bad MAC); exit `3` is
`unknown` because no verified result arrived during the bounded wait.

On exit `3`, copy `jobId` from the JSON and retrieve that exact ID:

```text
<python> scripts/readiness/check.py --config <same-config.json> \
  --job-id <same-32hex-id> [--wait 240] [--poll 30]
```

The retrieval form never submits. Do not create a new job while the prior
outcome is unknown.

## Output and boundaries

The JSON report is `lan-readiness-report/1` and contains the scenario, job
ID, overall outcome, named component states, and next action. It never emits
the key, command, configured paths, queue contents, or worker stderr. Parallel
runs have independent random IDs and the canonical queue's immutable,
no-clobber files; no shared receipt or observer state is used.

The Lenovo probe is fixed read-only code: filesystem metadata/digests,
executable lookup, and optional local `git rev-parse HEAD`. It has no network
operation. A returned result proves that the worker executed this job; mere
presence of a worker file never counts as worker readiness. Live Lenovo and
corporate checks remain separate authorized operations.

## Local fixture verification

```text
<python> -m unittest discover -s tests/readiness -p "test_*.py" -v
```

The suite uses temporary files, an ephemeral fake key, and a fake worker. It
covers success, a stale component, bad MAC, timeout followed by same-ID
retrieval, and concurrent calls. It performs no network or corporate access.
