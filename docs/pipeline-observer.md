# Pipeline observer

Read-only Dell → Lenovo pipeline check for one MR and one SHA through the
existing signed queue. Use only this scenario when the question is
"is this MR green for this SHA" from a host that cannot reach GitLab.

## Trigger

Use this scenario — and only this scenario — when all of these hold:

- you are on the GitLab-unreachable side (Dell) and need a verified
  pipeline verdict for a known MR URL and source SHA;
- the existing signed queue and its standard client are available;
- no push, retry, rebase, MR edit, or network change is requested.

Anything else (publish, diagnose-and-fix, freshness preflight) belongs
to a different scenario.

## Required inputs

- Full canonical MR URL, e.g. `https://<host>/<group>/<project>/-/merge_requests/<iid>`.
- Expected 40-char lowercase source SHA.
- Required-check profile JSON (`pipeline-observer-checks/1`) with
  at least one entry in `required_jobs` or `required_child_pipelines`
  (empty profiles are refused: strict `passed` must confirm a
  mandatory check).
- Owner-provided absolute Lenovo paths: interpreter, flow dir
  (`<checkout>/scripts/pipeline_observer`, holds `probe.py`),
  checkout, and the existing gitlab-mr-flow `scripts` dir
  (provides `gitlab_api` + recover support for live reads).
- Queue dir + key file outside any repository (key via `--key-file` only).

## Exact commands

Dell builds the request (no network, no key):

```text
python scripts/pipeline_observer/request.py \
  --mr-url <MR URL> --expected-sha <sha> \
  --profile <profile as spelled on Lenovo> \
  --python <absolute Lenovo interpreter> \
  --lenovo-flow-dir <absolute flow dir on Lenovo> \
  --lenovo-checkout <absolute Lenovo checkout> \
  --lenovo-client-dir <existing gitlab-mr-flow scripts dir on Lenovo> \
  --request-file <request.json> --command-file <command.txt>
```

The command file holds one PowerShell line (`& '...' '...'`:
single-quoted, apostrophes doubled, so `$`, backticks, and spaces
stay literal). Submit it with the existing signed bridge client (`&`
is required: a line starting with a quoted path is not an invocation
without it), then verify:

```text
python scripts/pipeline_observer/readback.py \
  --queue <queue-dir> --key-file <key-path> [--key-format raw|base64] \
  --job-id <job-id> \
  --expected-mr-url <MR URL> --expected-sha <sha> \
  --bridge-dir <existing bridge scripts dir> \
  [--receipt-out <receipt.json>]
```

Lenovo runs the single probed command from the request
(`probe.py --url … --profile … --expected-sha … --client-dir …`).

## Outcomes

| Readback verdict | Meaning | Exit |
| --- | --- | --- |
| `passed` | strict gate passed for this SHA (every required check green) | 0 |
| `failed` / `missing_required_check` / `pending` / `canceled` / `skipped` / `policy_blocked` | terminal or waiting state, carried with evidence; never `passed` | 1 |
| `mismatch` | wrong MR/SHA, bad MAC/kind/ID, unreadable success stdout, or inconsistency (e.g. `passed` with no pipeline/SHA) | 1 |
| `unknown` | timeout, truncation, untrusted status/exit, unreadable failure stdout, or SHA-less non-passed receipt (e.g. Lenovo-side outage) | 3 |

`unknown` keeps the slot: re-fetch the same job id and reconcile before
any new job id. A moved MR head invalidates the receipt; re-probe the
new head. Parallel calls must use separate request/command/receipt files.

## Boundaries

- Read-only: GET reads only; no retries, pushes, rebases, or MR writes.
- One SHA: the receipt binds MR URL + SHA + pipeline ID + status.
- Strict `passed` needs every declared required check; a green parent
  with a missing required child is never `passed`. The pipeline ID is
  required only for `passed`; `pending` with no pipeline yet is carried
  as `pending`.
- MR binding uses canonical form, so a trailing slash or `/diffs`
  suffix on the expected URL still binds.
- A `diagnosis` (with a `stop`/diagnose decision) is attached only
  when a concrete failed job is pinned; waiting states carry no
  diagnosis. Trace reads use the failed job's own project.
- Key stays with the standard client; never printed or stored in receipts.
- Fixture tests are local only and never live evidence; live runs need
  separate authorization.

## Local verification

```text
<python> -m unittest discover -s tests/pipeline_observer -v
node scripts/check-file-tokens.mjs --all
```

Covers: running vs completed pipelines, SHA change, bad MAC, timeout,
truncation, late answer on the same ID, parallel separate files,
exact generated command through PowerShell with `$`/backticks in
paths, probe/readback `main()` against fixture and real bridge
clients (raw + base64 keys).
