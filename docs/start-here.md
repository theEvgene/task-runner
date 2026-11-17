# Dell/Lenovo START HERE: first safe run

> Operator entry. Use this page for a normal first run on either computer:
> prerequisites, one command or shortcut, the visible success, and the single
> action when it fails.
> Do NOT use it for diagnosis. Give the agent `docs/runbook.md` (issue #64) with
> the exact printed `reason` or `jobId` instead of repairing by hand.
> No live host was observed while this page was written, so every live step
> stays `NOT_RUN`/`UNKNOWN` until an owner-observed receipt is recorded.

All values in angle brackets are placeholders. Replace them locally, never
commit them, and never treat a placeholder as a proven host name, share path,
or button label.

| Placeholder               | Means                                                                  | Exact detail                     |
| ------------------------- | ---------------------------------------------------------------------- | -------------------------------- |
| `<python-on-dell>`        | Python executable on Dell                                              | `docs/readiness.md`              |
| `<readiness-config.json>` | owner JSON outside every checkout                                      | `docs/readiness.md`              |
| `<lenovo-config.json>`    | local launcher config, `%LOCALAPPDATA%\task-runner\lenovo-worker.json` | `docs/lenovo-worker-shortcut.md` |
| `<shortcut>`              | desktop shortcut to `scripts/lenovo_worker/Launch-Worker.bat`          | `docs/lenovo-worker-shortcut.md` |
| `<job-id>`                | 32-hex id printed by a submit                                          | `docs/lan-bridge.md`             |

## Before the first run (both computers)

1. Both computers use a Task Runner checkout. Record each
   `git rev-parse HEAD` and keep the two values identical.
2. The owner already provided, outside every checkout: the existing queue, the
   key file, and the Lenovo launcher config. This page creates none of them.
3. Lenovo can reach the existing share with the owner's local account. The
   agent never changes a share, firewall, account, or credential.
4. The owner explicitly authorizes the one read-only Lenovo probe used below.

## Dell: one readiness check and one test submit

`scripts/readiness/check.py` performs the Dell checks, publishes exactly one
signed test job, and verifies one MAC-bound result. It never creates a queue.

```text
<python-on-dell> scripts/readiness/check.py --config <readiness-config.json> --authorize-lenovo-check --wait 600 --poll 30
```

Success: exit `0`, JSON `"schema":"lan-readiness-report/1"`,
`"outcome":"ready"`, and a `"jobId"`. Keep that `jobId`.

| Printed result                                                   | What it means                                                    | The only next action                                                                                                    |
| ---------------------------------------------------------------- | ---------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| exit `0`, `outcome: ready`                                       | Dell checks passed, one result verified                          | none; keep `jobId`                                                                                                      |
| exit `1`, a `components[]` entry `missing`/`stale`/`unconfirmed` | a verified `not-ready` report                                    | read `nextAction`; fix nothing yourself                                                                                 |
| exit `2`                                                         | configuration, policy, or protocol refusal (including a bad MAC) | stop; hand the report to the agent                                                                                      |
| exit `3`, `outcome: waiting` or `unknown`                        | no verified result inside the bounded wait                       | retrieve the same id with `--config <readiness-config.json> --job-id <job-id> --wait 600 --poll 30`; never submit again |

A raw signed submit and fetch instead of the readiness probe is in
`docs/lan-bridge.md`: `submit` prints `JOBID=<32hex>`, and `fetch` needs that
same id with `--timeout 600 --poll 30`. A timeout is `UNKNOWN`, never
completion.

## Lenovo: start worker and cleaner

Prerequisite: the one-time local config and password file from
`docs/lenovo-worker-shortcut.md` already exist outside the checkout, the share,
and the queue. This page creates neither.

Normal step: double-click `<shortcut>`.

Success: exit `0`, one JSON line with `"schema":"lenovo-launch/1"`,
`"outcome":"ready"`, `"scope":"worker-launch"`, `"jobOutcome":"not-evaluated"`,
and stderr `launch ready (worker:... cleaner:...)`.

| Printed result                    | What it means                     | The only next action                                                                       |
| --------------------------------- | --------------------------------- | ------------------------------------------------------------------------------------------ |
| exit `0`, `outcome: ready`        | worker and cleaner report started | none; a second click is `already-running` with the same pids, not a new worker             |
| exit `2`, `outcome: refused`      | nothing new was started           | read the single `reason` and `nextAction`; change no credential, share, pid file, or queue |
| an ambiguous or unexplained state | evidence is incomplete            | stop and hand the agent `docs/runbook.md`                                                  |

The typical first failure is `outcome: refused` with `queue-missing`,
`smb-share-unconfirmed`, or `role-ambiguous`. Report the reason; do not repair.

## Stop and hand over

For anything that differs from the success line above, stop. Give the agent:

1. `docs/runbook.md` (issue #64): the single diagnose -> read-only action ->
   readback path.
2. The exact printed `reason`, `jobId`, or role line, and the checkout SHA.

Do not start a second worker, resubmit a job while its `jobId` is `UNKNOWN`,
edit SMB credentials, or write on Lenovo without the owner's separate
per-action approval. No receipt means `NOT_RUN`/`UNKNOWN`, never `PASS`.

For release and two-host version readback use `docs/release.md` with
`skills/release/SKILL.md`; the pinned two-host readback remains issue #65 and
needs owner authorization.

## Where the exact details live

- Readiness check and report fields: `docs/readiness.md`.
- Worker/cleaner shortcut, config, and refusal reasons:
  `docs/lenovo-worker-shortcut.md`.
- Signed submit/fetch and the observation rule: `docs/lan-bridge.md`.
- Diagnosis and safe repair: `docs/runbook.md` (issue #64).
