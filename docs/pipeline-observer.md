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
  `required_jobs` and `required_child_pipelines`.
- Owner-provided absolute Lenovo paths: interpreter, flow dir, checkout.
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
  --request-file <request.json> --command-file <command.txt>
```

Submit `command.txt` with the existing signed bridge client, then verify:

```text
python scripts/pipeline_observer/readback.py \
  --queue <queue-dir> --key-file <key-path> --job-id <job-id> \
  --expected-mr-url <MR URL> --expected-sha <sha> \
  --bridge-dir <existing bridge scripts dir> \
  [--receipt-out <receipt.json>]
```

Lenovo runs the single probed command from the request
(`probe.py --url … --profile … --expected-sha …`).

## Outcomes

| Readback verdict | Meaning | Exit |
| --- | --- | --- |
| `passed` | strict gate passed for this SHA (every required check green) | 0 |
| `failed` / `missing_required_check` / `pending` / `canceled` / `skipped` / `policy_blocked` | terminal or waiting state, carried with evidence; never `passed` | 1 |
| `mismatch` | wrong MR/SHA, bad MAC/kind/ID, or unreadable success stdout | 1 |
| `unknown` | timeout, truncation, untrusted status/exit, or unreadable failure stdout | 3 |

`unknown` keeps the slot: re-fetch the same job id and reconcile before
any new job id. A moved MR head invalidates the receipt; re-probe the
new head. Parallel calls must use separate request/command/receipt files.

## Boundaries

- Read-only: GET reads only; no retries, pushes, rebases, or MR writes.
- One SHA: the receipt binds MR URL + SHA + pipeline ID + status.
- Strict `passed` needs every declared required check; a green parent
  with a missing required child is never `passed`.
- Key stays with the standard client; never printed or stored in receipts.
- Fixture tests are local only and never live evidence; live runs need
  separate authorization.

## Local verification

```text
<python> -m unittest discover -s tests/pipeline_observer -v
node scripts/check-file-tokens.mjs --all
```

Covers: running vs completed pipelines, SHA change, bad MAC, timeout,
truncation, late answer on the same ID, and parallel separate files.
