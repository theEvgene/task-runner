# Existing MR Ready run

Use this Dell → Lenovo scenario for one explicit request to prepare an
existing GitLab merge request for review. It invokes the existing
personal-agent-toolkit `gitlab-mr-flow/scripts/mr_ready_run.py` on Lenovo and
returns its bound Ready/blocker receipt through the existing signed queue.

This route does not transfer a branch, create a Draft MR, merge, or introduce
another GitLab client. History preparation, current-head pipeline checks,
bounded failed-job retry, and the Ready write are all owned by
`mr_ready_run.py` and happen within the single submitted user request.

## Build and submit on Dell

All Lenovo paths below are absolute paths as seen on Lenovo. The checkout must
already contain the MR source branch, and `--lenovo-client-dir` is the existing
personal-agent-toolkit `gitlab-mr-flow/scripts` directory.

```text
python scripts/mr_ready/request.py \
  --mr-url <existing full MR URL> \
  --python <Lenovo Python> \
  --lenovo-client-dir <gitlab-mr-flow scripts dir> \
  --lenovo-checkout <existing source checkout> \
  --type feat --key <Jira key> --subject <short Russian subject> \
  --scope "src/**" [--scope "tests/**"] \
  [--profile <Lenovo required-check profile>] \
  [--description-file <Lenovo description file>] \
  [--previous-head <40-char SHA>] [--max-retries 0|1|2] \
  --request-file <request.json> --command-file <command.txt>
```

Inspect the request, then submit `command.txt` with the standard signed bridge:

```text
python scripts/lan_bridge/lan_bridge.py submit \
  --queue <queue-dir> --key-file <key-path> [--key-format raw|base64] \
  --command-file <command.txt> --cwd <Lenovo checkout> \
  --dialect powershell \
  --timeout 1800 --max-output 262144 \
  --receipt-script-path <request.json receipt_script_path>
```

The command file contains the worker-supported explicit
`powershell.exe -NoProfile -NonInteractive -Command` launcher. The signed job
is the authorization boundary: `--dialect powershell` must be present, and
`--receipt-script-path` must be
the absolute `receipt_script_path` emitted in `request.json`; readback opens
that authenticated job, unwraps the supported launcher, and compares the
actual inner Python script argv to the sealed path before trusting stdout.
Jobs without both authenticated bindings fail closed. Retry is capped by
`--max-retries`, and there is no background watcher. Do not resubmit while a
write outcome is unknown.

## Read the result on Dell

```text
python scripts/mr_ready/readback.py \
  --queue <queue-dir> --key-file <key-path> [--key-format raw|base64] \
  --job-id <job-id> --expected-mr-url <same full MR URL> \
  --bridge-dir scripts/lan_bridge [--receipt-out <receipt.json>]
```

Outcomes:

| Verdict | Meaning | Exit |
| --- | --- | --- |
| `ready` | Ready is confirmed for the receipt's MR, head SHA, target branch/SHA, and passed current-SHA gate | 0 |
| `blocked` | The MR remains Draft; `reason`, `detail`, and `next_action` identify the stop | 1 |
| `mismatch` | MR identity, SHA binding, schema, or Ready evidence is inconsistent | 1 |
| `unknown` | Timeout/truncation, runner failure, unreadable result, or an ambiguous write outcome | 3 |

If preparation rewrites history, the runner returns `prepared_new_head` and
the readback stays `blocked`: publish that new SHA through the separately
authorized normal publication flow, then run this scenario again. Old
pipeline evidence cannot confirm the new head.

For `unknown`, re-fetch the same immutable job id and reconcile the live MR
before any new write. A receipt such as `publish_landed_unconfirmed` is never
reported as confirmed Ready even when readback suggests the write may have
landed.

## Local checks

```text
python -m unittest discover -s tests/mr_ready -v
node scripts/check-file-tokens.mjs  # with the changed files staged
```
