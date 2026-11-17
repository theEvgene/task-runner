# Signed LAN queue failure diagnosis

Read-only runbook for a stuck or failed Dell–Lenovo signed queue job.
Normative protocol stays in `lan-bridge.md`; worker/cleaner setup in
`lenovo-worker-shortcut.md`; preflight in `readiness.md`.
This file adds only ordering, forks, and messaging.

Link convention: paths here are relative to this document (`docs/`);
from the repository root they are `docs/<name>.md`.
The repository entry for this scenario is `AGENTS.md`.

Verify the real client before quoting commands:

```text
<python> scripts/lan_bridge/lan_bridge.py --help
<python> scripts/lan_bridge/lan_bridge.py fetch --help
<python> scripts/lan_bridge/get.py --help
```

`scripts/lan_bridge/wait.py` exposes a Python library function
`peek_result()` only — it has NO CLI. Never run
`wait.py peek_result --...`; the supported CLIs are `lan_bridge.py`
(`submit`/`fetch`) and `get.py`.

## Non-negotiable rule

- One JOBID, one poll interval, one window: re-read the SAME id every
  30 s for 10 min (`fetch --timeout 600 --poll 30`, or
  `get.py --window 60 --max-windows 10 --poll 30`).
- Bounded-wait expiry (exit `3`) is **unknown**: never "still running",
  never "failed", never permission for a second submit.
- A duplicate id is refused (`JobExistsError`); a second submit would be
  a NEW id, not a retry.
- New JOBID only after a MAC-verified terminal verdict bound to the
  published id (`completed`/`ok` exit `0`, worker failure, worker
  `timed_out`, truncation handled per `lan-bridge.md`), OR when publish
  refusal before any JOBID existed is proven (submit refused, no JOBID
  issued — per the submit-client receipt, not from a later file read).
- Deletion or absence of `jobs/<id>` NEVER proves the submit did not
  happen: wrong queue dir, different mount, different host/share, stale
  path, or worker/cleaner move can all hide the file. Without a proven
  pre-JOBID publish refusal the outcome stays UNKNOWN — keep the SAME id.
- Fixing an exit-`2` blocker (queue unavailable, tamper, bad id, bad key
  placement) or a network blocker for an ALREADY-PUBLISHED id is NEVER
  permission for a new JOBID: fix the cause, then resume the SAME id.
  The outcome of the published task is still unknown until verified.

## Messaging obligation (every stage)

After EACH stage below, send one intermediate message with exactly:

1. what was checked;
2. established FACT vs still UNKNOWN (never promote a hypothesis);
3. the next stage.

During any bounded wait or blocked wait, inform the user that waiting
continues, which JOBID is observed, and when the next update comes.
No silent 10-minute gaps.

## Stage 0 — Fix inputs (agent, no queue touch)

Record JOBID (`32hex`), queue dir, key-file path (existence only, never
content), submit time, and how much of the 10 min already elapsed.
If JOBID is missing, stop: there is nothing to diagnose.
Fact example: "JOBID `abc…` known, submitted at HH:MM, 4 min elapsed".
Unknown example: "JOBID unknown — cannot proceed".

## Stage 1 — Known JOBID + signed result (fast check first, never a 10-min block up front)

Do NOT open with a blocking 10-minute `fetch`. Fast verified read first
with a really supported client, then continue Stages 2–6 in parallel
with the long same-id observation:

Fast single check — exactly one of (poll stays 30 s, the owner default):

```text
<python> scripts/lan_bridge/lan_bridge.py fetch --queue <queue> --key-file <key> --id <job-id> --timeout 30 --poll 30
```

or exactly one short window:

```text
<python> scripts/lan_bridge/get.py --queue <queue> --key-file <key> --id <job-id> --window 60 --max-windows 1 --poll 30
```

Exit `0` with a MAC-verified envelope bound to this id is the ONLY
success proof. Exit `2` is a concrete fast blocker (see Stage 7.2).
Exit `3` is UNKNOWN — keep the SAME id and go to Stage 2 immediately;
do not wait 10 minutes before diagnosing.

Long observation of the SAME id (does not block independent
diagnosis; announce each window per the messaging obligation):

```text
<python> scripts/lan_bridge/lan_bridge.py fetch --queue <queue> --key-file <key> --id <job-id> --timeout 600 --poll 30
```

or:

```text
<python> scripts/lan_bridge/get.py --queue <queue> --key-file <key> --id <job-id> --window 60 --max-windows 10 --poll 30
```

Classify only via `lan-bridge.md` "Confirmed states vs unknown".
A MAC-verified envelope bound to this id is the ONLY success proof.
Anything else (empty slot, exit `3`) stays UNKNOWN — go to Stage 2.
Hypotheses about worker speed are not facts.

## Stage 2 — Dell access to the local queue (agent)

Check from Dell only: queue dir present, `jobs/<job-id>` present,
`results/<job-id>` absent/present. A missing queue dir refuses at once
(exit `2`, `queue unavailable … check path/mount`): concrete blocker,
never "pending". Do not create a queue.

Owner may confirm the same read-only fact in PowerShell (no secrets,
no network probe, existing mount only):

```powershell
Test-Path '<queue-dir>'
Get-ChildItem '<queue-dir>\jobs' -Filter '<job-id>*'
Get-ChildItem '<queue-dir>\results' -Filter '<job-id>*'
```

Explorer cross-check (Dell, existing path only, no config change):

```powershell
explorer.exe '<queue-dir>'
```

Forks: queue reachable + job file present + no result → worker side
(Stages 4–5), keep SAME id. Queue dir missing → Stage 6 (safe
recovery), never a resubmit. Job file itself missing or deleted proves
nothing about whether the submit landed (different queue, mount, host,
or worker/cleaner move are all possible): outcome stays UNKNOWN, keep
the SAME id. A new submit is allowed only on proven submit refusal
before any JOBID existed — never from a later `jobs/<id>` absence.

Report: "queue reachable, job file present/absent, result present/absent".

## Stage 3 — Real current Dell address and Lenovo setup (agent checks what it can, owner confirms the rest)

Agent first checks by every means available to it; only what the agent
cannot prove becomes an owner question. Do not guess from remembered
paths. The agent MAY run one explicitly authorized single read-only
signed diagnostic for the named resource (see Stage 4); broad scans
stay forbidden.

Facts to establish (never key/password content, never full JSON dumps):

- current Dell host/share name actually exporting the queue;
- Lenovo `queueDir`, `shared` UNC, checkout revision
  (`git rev-parse HEAD` recorded at copy time), Python path.

Dell read-only PowerShell (existing paths only):

```powershell
hostname
ipconfig
Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' } | Select-Object IPAddress, InterfaceAlias
Get-SmbMapping | Select-Object LocalPath, RemotePath, Status
Test-Path '<queue-dir>'
Get-ChildItem '<queue-dir>' -Force
```

Lenovo config read WITHOUT exposing secrets — never
`Get-Content <json> | Out-String` whole. Read only safe fields:

```powershell
$cfgPath = "$env:LOCALAPPDATA\task-runner\lenovo-worker.json"
$cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json
$cfg | Select-Object queueDir, sharedDir, workerDir, stateDir, trashDir, python, workerPoll, cleanerInterval
$cfg.smb | Select-Object unc, username
Test-Path $cfg.queueDir
Test-Path $cfg.sharedDir
```

Never display: key file content, `passwordFile` content, any password,
the full raw JSON. Key-file path existence only.

Forks: UNC in config ≠ actually mapped share, or `queueDir` ≠ existing
queue, or revision ≠ pinned checkout → hypothesis only until the owner
confirms it — record as UNKNOWN with a named check. Never edit config
blindly; one field changes only after its mismatch is confirmed.

## Stage 4 — SMB availability from Lenovo (one authorized read-only probe allowed, no scans)

Distinguish: `queue unavailable` (share/path wrong) vs empty result slot
(share up, worker silent). Agent on Dell can only state its own
`Test-Path` fact. Whether Lenovo sees the same share is established by
exactly one of, in order:

1. one explicitly authorized single read-only signed diagnostic via the
   canonical client (`lan_bridge.py submit` + `fetch`/`get.py` on the
   SAME probe id, e.g. a one-line `Test-Path`/`Get-ChildItem`/`hostname`
   of the NAMED share), then the MAC-verified bound result; or
2. if no such authorization exists, an owner fact: does the Lenovo
   shortcut host still map the SAME UNC with the SAME user, and what was
   its last `launch ready` / `already-running` line?

That diagnostic probe is a SEPARATE task with its OWN NEW JOBID,
observed by itself under that probe id to its own terminal verdict. The
original stuck task keeps being observed under its ORIGINAL id (Stage 1
long observation continues in parallel). The probe never replaces,
retries, or repeats the original task, and its result — success or
failure — is only a fact about the named share at probe time; it never
permits a new submit of the original id.

Lenovo read-only check of the confirmed UNC only (no secrets, existing
share only, only when authorized per above):

```powershell
Test-Path '<confirmed-unc>'
Get-ChildItem '<confirmed-unc>' -Force
```

or via the already-read safe config fields from Stage 3:

```powershell
Test-Path $cfg.sharedDir
Get-ChildItem $cfg.sharedDir -Force
Test-Path $cfg.queueDir
```

Lenovo Explorer cross-check (confirmed path only, no config change):

```powershell
explorer.exe '<confirmed-unc>'
```

Agent performs NO broad live network probe, NO scan (`ping` sweeps,
`Test-NetConnection` port sweeps, share enumeration are out of scope).
One targeted read-only check of the documented UNC is allowed when
authorized; anything beyond the existing share is UNKNOWN until the
owner confirms it.

SMB errors (`share unavailable`, `bad credentials`) are fixed on the
Lenovo side per `lenovo-worker-shortcut.md`, then resume the SAME id —
never a new submit for the already-published job.

## Stage 5 — Worker/cleaner command and logs prove nothing without a bound result (ask owner, no restart by default)

Actual command lines come only from the Lenovo config
(`workerCommand` / `cleanerCommand`, default: sibling `worker.py` /
`cleaner.py` in the pinned checkout) plus `stateDir\<role>.pid` and
`stateDir\<role>.log`. Presence of a process or log lines is NOT proof
that THIS JOBID executed — only a MAC-verified result bound to the id is.

`Get-Process` `.Path` shows `python.exe`, never `worker.py` — use the
command line instead, redacting sensitive data before sharing:

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like '*worker.py*' -or $_.CommandLine -like '*cleaner.py*' } | Select-Object ProcessId, ParentProcessId, CreationDate, CommandLine
Get-Content '<stateDir>\worker.log' -Tail 20
Get-Content '<stateDir>\cleaner.log' -Tail 20
```

Redact before pasting: key content, passwords, full command secrets —
keep only script paths, pids, timestamps, exit lines.

Already-running handling (never kill/replace blindly):

- `already-running` + pids alive + `CommandLine` matches the configured
  command/binding → confirmed facts only (launcher reports
  `already-running`, pid alive, command line matches): do nothing, keep
  observing the SAME id. This is NOT a health verdict for THIS JOBID —
  whether this job executed stays UNKNOWN until Stage 1 verifies it.
- pid file stale (provably dead pid after a reboot) → the launcher
  auto-recovers under its lock on next double-click; no manual cleanup,
  no duplicate process.
- ambiguous state (live pid under a changed command/binding, foreign
  command line, unprovable command line, corrupt pid content) → do NOT
  restart, do NOT delete, do NOT `--force`. Verify first
  (`tasklist /FI "PID eq N"`), then remove that `<role>.pid` file only
  after proving no such process runs. A restart without establishing
  active-task state risks silently dropping access and jobs.

Report process/log facts separately from the verdict: "worker process
visible (fact), THIS job executed (unknown until Stage 1 verifies)".
Never treat `already-running`, a pid file, or a log tail as success.

## Stage 6 — Network/router only if necessary, never scan, never reconfigure blindly

Enter only when Stages 2–5 point at connectivity (share was working,
paths unchanged, queue locally reachable, result still unknown).
Allowed: confirm cable/VPN physically present, confirm the SAME UNC is
still the documented one, re-check `Test-Path` on Dell, one authorized
single read-only check of the named resource (Stage 4). Router state is
an owner-confirmed fact (which device, which indicator was checked) —
not an agent probe result unless explicitly authorized for that device.
Forbidden: port scans, ping sweeps, `Test-NetConnection` sweeps,
firewall/share reconfiguration, autostart installation, blind config
edits. Corporate policy is unchanged (`lenovo-worker-shortcut.md`).
Anything beyond the existing share is UNKNOWN until the owner confirms it.

Forks: Dell `Test-Path` fails but Explorer/mapping shows the share →
record both facts separately (CLI fact vs Explorer/mapping fact), fix
mount, resume SAME id. Dell share fact present but Lenovo probe/mapping
fact fails → Lenovo-side SMB fix, resume SAME id. Both sides report the
share and the job file visible → confirmed facts only (Dell visible,
Lenovo visible); this alone excludes nothing — back to Stage 5, keep
SAME id.

## Stage 7 — Safe recovery and verification (agent + owner)

1. Keep observing the SAME id until 10 min expire or a verified result
   lands (`fetch --timeout 600 --poll 30`, same-id resume after any
   interruption). Announce each window per the messaging obligation.
2. If exit `2` (unavailable queue, tamper, bad id, bad key placement):
   fix the named cause, then resume the SAME id. A new submit only on
   proven submit refusal before any JOBID existed (no JOBID issued); a
   later `jobs/<id>` absence or deletion never counts as such proof.
   An already-published id never earns a new id by fixing a blocker.
3. If exit `3` after 10 min: still UNKNOWN. Do NOT resubmit blindly.
   Reconcile per the scenario (`dell-lan.md` §7, `readiness.md`
   same-id retrieval, `pipeline-observer.md` same-id re-fetch).
4. New JOBID only after a verified terminal verdict (`completed`/`ok`
   with exit `0`, worker failure, worker `timed_out`, truncation handled
   per `lan-bridge.md`). Fixing a blocker is not a verdict.
5. Verify with fixtures only where applicable
   (`<python> -m unittest discover -s tests/lan_bridge -v`); live proof is
   only the bound verified result. Record JOBID, exit, revision.

## What this runbook never does

- No second submit while outcome is unknown; no queue creation.
- No secret handling (key/password content never printed, never receipted;
  config reads select safe fields only, never whole-JSON dumps).
- No broad live network scan and no blind reconfiguration; one
  authorized single read-only diagnostic of the named resource only.
- No worker kill/restart without establishing active-task state and
  pid/command ownership.
- No conflicting records, no foreign files, no ticket publish/close.
- No hypothesis stated as an established cause.
