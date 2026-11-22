# Lenovo queue/worker screen (read-only)

> Agent entry. Use ONLY this scenario when: the operator wants to watch the
> existing signed queue and launcher roles without typing PowerShell reads.
> Do NOT use it for: stopping/restarting the worker (issue #170, not built),
> claim recovery (`docs/lenovo-job-recovery.md`), queue diagnosis
> (`docs/runbook.md`), or any live-queue debugging without owner consent.

One local screen, refreshed automatically, over the EXISTING queue and the
launcher state dir. Read-only: it never publishes, seals, deletes, kills,
retries, recovers or sends anything.

## Open it (one command on Lenovo)

Double-click `scripts\lenovo_worker\Show-Queue.bat` (optional first arg: an
alternate config path; default
`%LOCALAPPDATA%\task-runner\lenovo-worker.json`, the same file the launcher
uses). Or run directly:

```powershell
python scripts\lenovo_worker\queue_screen.py --config "$env:LOCALAPPDATA\task-runner\lenovo-worker.json"
```

Useful flags: `--interval 5` (watch refresh seconds, 1..300),
`--recent 10` (recent results shown, 0..50), `--once` (single snapshot),
`--json` (machine-readable snapshot), or explicit
`--queue/--key-file/--key-format/--state-dir` instead of `--config`.
Stop the watch with Ctrl+C (the queue is untouched). Exit `0` = screen shown
(even with connection lost: the loss itself is the result);
exit `2` = refused (bad config/key/args: nothing verifiable).

## What the states mean

| Screen state                                   | Meaning                                                                                                          | Next step                               |
| ---------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- | --------------------------------------- |
| `waiting` (active)                             | signed lease holds the job: ownership, NOT liveness                                                              | re-read the SAME id; never resubmit it  |
| `start UNCONFIRMED` / owner not observed alive | active job whose execution cannot be proven                                                                      | treat as unknown, never as success      |
| `unknown` (pending)                            | signed request, no lease, no verified result                                                                     | re-read the SAME id; never resubmit     |
| `refused`                                      | file present but unverifiable: not evidence of anything                                                          | ask the queue owner for the sealed file |
| `finished <outcome>`                           | MAC-verified terminal result only (`success`, `worker_failure`, `worker_timeout`, `truncated`, `unknown_status`) | consume it, or re-run as a NEW id       |
| `connection: lost`                             | queue dirs unreadable: nothing below is health evidence                                                          | check path/mount                        |
| worker/cleaner `up`                            | pid alive with a matching command line                                                                           | none                                    |
| worker/cleaner `stopped`                       | no record, or a provably dead pid (stale record)                                                                 | start via the launcher if needed        |
| worker/cleaner `unknown`                       | ambiguous record, foreign command, or unprovable command line                                                    | verify, then remove that state file     |

Missing data is never shown as healthy. Two lease-held jobs are listed
side by side (the existing one-or-two-active bound); a third waits.
No command text, working directory, output, file content, password,
host name or private path ever appears on the screen, in `--json`, or in
logs — only job ids, states, counts and timings.

No automatic recovery, no retries, no telemetry: unconfirmed jobs stay
unconfirmed until the worker seals a signed result or the manual recovery
flow settles them.

## Local verification (fixtures, no network, no live queue)

```text
<python> -m unittest discover -s tests/lenovo_worker -v
node scripts/check-file-tokens.mjs
```

Covers (in `tests/lenovo_worker/test_queue_screen.py`): pending-as-unknown,
active-as-waiting with unconfirmed start, two active jobs with confirmed
starts, dead owner staying unknown, verified success/failure/timeout
outcomes, unverifiable-result refusal, connection loss on a missing queue,
stale pid as stopped, CLI `--once` output with privacy, and bad-key refusal.

Live screen check on an isolated Lenovo queue: NOT run by the agent (needs
the owner at the keyboard). Record its commands, exits and revision in
issue #169 when done; until then treat this as fixture-verified only.
