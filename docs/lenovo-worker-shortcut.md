# Lenovo one-shortcut worker + cleaner with SMB connect

> Agent entry. Use ONLY this scenario when: after a Lenovo reboot the SMB
> access to Dell is gone and the worker must be back with one double-click.
> Do NOT use it for: queue protocol questions (`docs/lan-bridge.md`), base
> import or publishing (`docs/dell-lan.md`, `docs/fresh-base.md`), or any
> live-queue debugging without owner consent.

One shortcut restores access to the EXISTING share and starts the worker
(`scripts/lenovo_worker/worker.py`) and the cleaner
(`scripts/lenovo_worker/cleaner.py`). Nothing is installed as autostart.

## Worker delivery (determined from this repo)

This repository vendors no prebuilt worker binary and no service: the worker
entry is `scripts/lenovo_worker/worker.py` with its sibling executor classifiers
(`powershell_truth.py` and `cmd_fallback.py`; all stdlib only, same HMAC protocol
as `scripts/lan_bridge/lan_bridge.py`), and the only cleaner is the sibling
`cleaner.py`. Delivery = this checkout
at the pinned revision the operator copies to Lenovo. `launch.py` starts the
sibling copies by default; `--worker-dir` or explicit `workerCommand` /
`cleanerCommand` in the local config names another checkout. A remembered
path from another machine is never trusted: the config must name it.

## One-time setup (on Lenovo, ~5 minutes)

1. Copy this checkout to Lenovo and note its revision (`git rev-parse HEAD`).
2. Copy `scripts/lenovo_worker/config.example.json` to
   `%LOCALAPPDATA%\task-runner\lenovo-worker.json` (outside Git AND outside
   the share) and fill real absolute paths. The example holds fixture values
   only.
3. Save the SMB password as plain text (user-accepted, see #23) into the
   `passwordFile` path from step 2 — one line, no trailing spaces. This file
   stays outside the repo and outside the share.
4. Lock both local files to the current user (PowerShell, admin not needed):

```powershell
icacls "$env:LOCALAPPDATA\task-runner\lenovo-worker.json" /inheritance:r /grant:r "$env:USERNAME:(R)"
icacls "C:\Users\owner\.secrets\lan-smb-password.txt" /inheritance:r /grant:r "$env:USERNAME:(R)"
```

Verify with `icacls <file>` — only the current user may appear.

5. Create the shortcut: right-click
   `scripts\lenovo_worker\Launch-Worker.bat` → Send to → Desktop shortcut.
   Optional: shortcut Properties → Advanced → Run as administrator OFF
   (never needed), Window: Minimized.

## Normal use

Double-click the shortcut. Success = one JSON line on stdout with
`"outcome":"ready"` and stderr `launch ready (worker:… cleaner:…)`.
A second click while both run reports `already-running` with the same pids
and starts nothing new.

## What it guarantees

- Existing queue only: a missing `queue/`, `queue/jobs/` or `queue/results/`
  refuses with `queue unavailable … check path/mount`. No empty queue is
  ever created, and the worker seals into a fresh dir never. `trashDir` and
  `stateDir` must sit outside the queue and the shared folder (checked
  lexically before anything is created), so creating them can never
  materialize queue parents and the cleaner can never see launcher state.
- Existing jobs/results are never rewritten: results are published
  immutably; a sealed id is skipped, never replaced. A result the worker
  cannot seal for infrastructure reasons is reported as failed, never as
  sealed, and a finished `--once`/`--max-jobs` batch exits 2 when any job
  was refused or failed. Invalid `working_directory`/spawn failures seal a
  bounded terminal error (exit 2) instead of crashing or re-executing.
  Declared return files keep the bridge contract (any path the job names)
  with byte bounds (8 files, 256 KiB each, 1 MiB total); truncation sets
  `output_truncated`. Outputs plus encoded files are fitted to a safe
  payload ceiling so the signed envelope stays under 2 MiB; a size refusal
  seals one minimal fallback result so the job never re-executes.
- Key custody: a key inside the queue, the checkout, or the share refuses
  before any work. The password travels only inside a PowerShell
  `-File` script that reads it from the local file; it never appears in
  process args, stdout, stderr, logs, or receipts. The file holds one line;
  a trailing CRLF is trimmed the same way in the fixture check and in the
  real SMB mapping, and a multi-line file refuses.
- One launch at a time: an exclusive state lock is held from before any
  inspection until after both pid files are written, and both roles are
  inspected before either is spawned. A provably dead pid (stale, legacy, or
  changed config under a dead pid) is a safe reboot recovery: it is removed
  under the lock and the role is started fresh, so a reboot needs no manual
  cleanup and never duplicates a live process. Ambiguous pid state (live pid
  under a changed command/binding, foreign command line, unprovable command
  line, corrupt content, or an unrecognized pid format around a live pid)
  refuses with the exact pid file to inspect and starts nothing and deletes
  nothing. The launcher never kills, never replaces, never `--force`s. Remediation:
  verify no such process runs (`tasklist /FI "PID eq N"`), then remove
  `stateDir\<role>.pid`.
- Process identity is bound to the full command array plus its configuration
  (`queue`/`keyFile` for the worker, `shared`/`trash` for the cleaner). A
  live pid recorded under a different command or binding refuses instead of
  being adopted.
- The shortcut propagates the launcher exit: `ready` is exit 0, any refusal
  is exit 2.
- Corporate policy is unchanged: no autostart, no firewall/share change,
  no credential written to the queue or the share.

## Failures (all exit 2; ambiguous/config failures start nothing new)

| Message                       | Meaning                     | Fix                                   |
| ----------------------------- | --------------------------- | ------------------------------------- |
| `cannot read config`         | config path wrong           | point the shortcut at the real JSON   |
| `share unavailable …`         | Dell down / UNC wrong       | check host, share name, cable/VPN     |
| `bad credentials …`           | SMB refused user/password   | fix user or password file content     |
| `queue unavailable …`         | share up, queue path wrong  | fix `queueDir` to the existing queue  |
| `key unusable / key inside …` | key file wrong or misplaced | re-issue key outside queue/share/repo |
| `trash/state … must be outside` | trash/state inside queue/share | move them outside both             |
| `ambiguous <role> state`      | live foreign/unprovable pid, live pid under changed config | verify, then remove that pid file |
| `stale pid (auto-recovered)`   | dead pid after a reboot (legacy or changed config) | no action: relaunched fresh under the lock |
| `another launch is in progress` | concurrent double-click   | wait seconds, re-run to adopt pids    |
| `<role> exited at once`       | child crashed               | read `stateDir\<role>.log`            |

## Manual post-reboot validation (REQUIRED, manual only)

Automated tests below use fixtures only and prove no live run. After a real
reboot, with the owner present and a TEST queue (never the live queue or
foreign jobs):

1. Reboot, log in, double-click the shortcut → `ready`, two processes in
   `tasklist` matching `worker.py` and `cleaner.py`.
2. Submit one test job, run one `fetch`, confirm a verified result.
3. Double-click again → `already-running`, same pids.
4. Disconnect the share / wrong password → clear refusal, no new queue.

Status: NOT run by the agent (live validation needs the owner at the
keyboard after a reboot). Record its commands, exits, and revision in the
issue when done; until then treat this scenario as fixture-verified only.

## Local verification (fixtures, no network, no live queue)

```text
<python> -m unittest discover -s tests/lenovo_worker -v
```

Covers: worker seals one result and skips sealed/foreign ids; failed seals
reported as failed with batch exit 2; bounded return files with truncation
flag; invalid cwd seals a bounded terminal error without re-execution;
outputs plus encoded files fitted under the 2 MiB envelope with a minimal
fallback seal instead of re-execution; missing queue refuses without creating it; key inside
queue/share/checkout refuses; cleaner moves only top-level `*.tmp` outside
the configured queue/trash/state and refuses misplaced trash/state; launch
success + repeat is idempotent with command/binding adoption; dead pid
records recover after a reboot without duplicates while live foreign/changed
pids still refuse with nothing started or deleted; concurrent
launch serializes on the state lock; misplaced trash/state refuses before
anything is created; unavailable share and bad credentials refuse; password absent from all
outputs, logs, and child command lines.
