# Dell/Lenovo agent runbook: diagnose, safe repair, readback

> Agent entry. Use this document when a worker, queue, SMB share, or resumed
> agent thread behaves unexpectedly and you need one diagnose -> safe repair ->
> readback path. It names the existing command, the trusted receipt field, one
> read-only action, and the stop criterion for each observed state.
> Do NOT use it for: first-time scenario setup, protocol changes, a second
> queue/worker/transport, or any Lenovo-side write without the owner's separate
> per-action consent.

This is the single diagnostics entry for worker, queue, SMB, and thread state.
Scenario documents keep their own exact commands (`docs/lan-bridge.md`,
`docs/readiness.md`, `docs/lenovo-worker-shortcut.md`, `docs/dell-lan.md`);
this runbook replaces the scattered memo-style facts and creates no metadata
registry, cache, installer, queue, or worker.

## Entry facts

Status values are fixed:

- `OBSERVED-LOCAL` - the named repository file, schema, or link was read in
  this checkout. It never means a live host was seen.
- `NOT_VERIFIED` - the host fact was not observed by the agent; only its own
  launcher output or a MAC-verified readback may promote it.
- `UNKNOWN` - evidence is missing, hidden, or ambiguous. It is never `PASS`.

No Lenovo host was observed while writing this document, so every live-process
row stays `NOT_VERIFIED` until that host's own readback is recorded.

`lenovo-launch/1` exists only as the launcher's stdout for that run; the
launcher writes no receipt file. The launch-lock owner file
`<stateDir>/.launch.lock/owner.json` records the current lock owner's `pid` and
`started` and is removed when the run ends. Role PID files also persist under
`<stateDir>`. A
resumed thread therefore reads the stdout capture the owner kept with the
handoff; without that capture the launcher facts stay `NOT_VERIFIED` and are
never reconstructed from memory.

| Fact              | Host role   | Concrete source                           | Revision / launcher / readback                                               | Status         |
| ----------------- | ----------- | ----------------------------------------- | ---------------------------------------------------------------------------- | -------------- |
| diagnose-entry    | Dell+Lenovo | `AGENTS.md`, `docs/runbook.md`            | `git rev-parse HEAD` of the observed checkout; link from `AGENTS.md`         | OBSERVED-LOCAL |
| signed-queue      | Dell+Lenovo | `scripts/lan_bridge/lan_bridge.py`        | envelope fields `v: 1`, `kind`, `id`, and `mac`; readback is a MAC-verified result bound to `id` | NOT_VERIFIED   |
| worker-process    | Lenovo      | `scripts/lenovo_worker/worker.py`         | launcher `scripts/lenovo_worker/launch.py`; readback `lenovo-launch/1` roles | NOT_VERIFIED   |
| cleaner-process   | Lenovo      | `scripts/lenovo_worker/cleaner.py`        | launcher `scripts/lenovo_worker/launch.py`; readback `roles[].state`         | NOT_VERIFIED   |
| launcher-shortcut | Lenovo      | `scripts/lenovo_worker/Launch-Worker.bat` | exit 0 `ready` / exit 2 refused; readback stdout JSON `lenovo-launch/1`      | NOT_VERIFIED   |
| readiness-report  | Dell+Lenovo | `scripts/readiness/check.py`              | report `lan-readiness-report/1`; readback `jobId` and `outcome`              | NOT_VERIFIED   |
| checkout-revision | Dell+Lenovo | `docs/readiness.md`                       | expected `<40-lowercase-hex>` in the owner config; `git rev-parse HEAD`      | NOT_VERIFIED   |

## Diagnose path

1. Name the observed case from the table below. One case per run.
2. Read only its trusted receipt field. A field that is absent is `UNKNOWN`.
3. Take the allowed read-only action once. It never mutates the queue, a
   share, a checkout, a credential file, or a process.
4. Stop at the stated readback or refusal. A repair that writes is a separate
   action with the owner's recorded consent; it is never inferred from a
   diagnosis.

## Observed states

| Case                | Observed state                                                          | Host role   | Source (repo path)                                                                 | Trusted receipt field                                                                                    | Allowed read-only action                                                                            | Stop / readback                                                                                        |
| ------------------- | ----------------------------------------------------------------------- | ----------- | ---------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| queue-missing       | queue dir, or its `jobs/` or `results/`, is absent or unmounted        | Dell+Lenovo | `scripts/readiness/check.py`, `scripts/lan_bridge/lan_bridge.py`, `scripts/lenovo_worker/launch.py` | queue dir absent -> `lan-readiness-report/1` component `signed-queue` `state: missing`; a missing `jobs/` or `results/` -> `lenovo-launch/1` `outcome: refused` with `reason: queue-missing` and a `detail` starting `queue unavailable` | read the report component or the refusal reason; compare the owner queue path with the config | refusal: queue dir absent -> exit 1, reason `queue directory is absent`; missing `jobs/` or `results/` -> launcher exit 2 `queue unavailable`; a missing `jobs/` is refused at exit 2 instead of a `waiting` or `unknown` claim, while a missing `results/` with no signed lease stays exit 3 `waiting`/`unknown` |
| bad-mac-or-id       | MAC invalid, wrong key, or `kind`/`id` not the requested one            | Dell+Lenovo | `scripts/lan_bridge/lan_bridge.py`, `scripts/readiness/check.py`                   | MAC verification plus `kind`/`id` equality; no payload is returned                                       | re-fetch the same id with the owner key; compare the requested id and the printed job id            | refusal: exit 2 for MAC, id, or kind mismatch; nothing from that envelope is trusted or relayed        |
| unknown-no-result   | no verified result arrived within the bounded wait                      | Dell        | `scripts/lan_bridge/lan_bridge.py`, `scripts/lan_bridge/get.py`                    | exit 3 and `lan-readiness-report/1` `outcome: waiting` (a signed lease holds the job) or `unknown`, with `jobId`; its `status` member mirrors that outcome | re-fetch that same id through `--job-id` or `get.py --id`; keep the id                              | readback: exit 0 MAC-verified result, or exit 3 `waiting`/`unknown` with the same id; an unverifiable request or result is exit 2 `refused`; never `running`            |
| stale-pid           | recorded pid is dead after a reboot, or the config changed under a dead pid | Lenovo      | `scripts/lenovo_worker/launch.py`                                                  | `lenovo-launch/1` `outcome: ready` with `roles[].state: started` and the new pids; stderr `launch ready (` plus `role:state:pid` triples; a dead record prints no separate marker | read the handoff's captured launcher stdout; verify each reported pid with a local `tasklist` query | readback: `outcome: ready`, `roles[].state: started`, and those pids live; a second launch adopting them (`already-running`, same pids) is an owner-consented action, not the agent's readback |
| worker-absent       | no worker or cleaner process consumes the queue                         | Lenovo      | `scripts/lenovo_worker/worker.py`, `scripts/lenovo_worker/cleaner.py`              | `lenovo-launch/1` `roles[].state`; process presence alone never proves worker readiness                  | read the handoff's captured launcher stdout; compare the pids with a local `tasklist` query; report the gap to the owner | refusal: `outcome: refused` exit 2, or `NOT_VERIFIED` until an owner-authorized probe returns a result |
| worker-foreign-pid  | pid file holds a live foreign or unprovable command                     | Lenovo      | `scripts/lenovo_worker/launch.py`                                                  | `lenovo-launch/1` `outcome: refused`, `reason: role-ambiguous`, and a `detail` starting `ambiguous <role> state` with the case and the pid; exit 2 | read the refusal reason; query only that pid with `tasklist`; report the named pid | refusal: exit 2 with `detail` starting `ambiguous <role> state` naming the live pid; the pid-file path is named only by the remove-variant reasons; nothing is started, stopped, or replaced |
| smb-auth            | share connect reports `bad credentials`                                 | Lenovo      | `scripts/lenovo_worker/launch.py`                                                  | `lenovo-launch/1` `outcome: refused`, `reason: smb-auth-refused` or `smb-credential-file-invalid`, and a `detail` prefixed `bad credentials`                             | read the refusal reason; check which credential-file key the config names, without printing content | refusal: exit 2 `bad credentials`; the agent performs no credential write                              |
| smb-network         | share connect reports `share unavailable` or `queue unavailable`        | Lenovo      | `scripts/lenovo_worker/launch.py`                                                  | `lenovo-launch/1` `outcome: refused`, `reason: smb-share-unconfirmed` or `queue-missing`, and its `detail`                                             | read the refusal reason; report the named share; the owner checks host, share name, cable, or VPN   | refusal: exit 2 `share unavailable` or `queue unavailable`; no share or queue is created               |
| unsupported-runtime | a required tool or interpreter is absent, or a role exits at once       | Dell+Lenovo | `scripts/readiness/check.py`, `scripts/lenovo_worker/launch.py`                    | `lan-readiness-report/1` components[] `state: missing`; `lenovo-launch/1` `reason: role-start-failed` with a `detail` `<role> exited at once` | read the report tool component; compare the declared tools in the config; read the bounded role log | refusal: launcher exit 2 for `<role> exited at once`, or report exit 1 carrying a `state: missing` component; installing a runtime is a separate owner action |
| wrong-cwd           | job working directory is invalid, or differs under the same id          | Lenovo      | `scripts/lan_bridge/lan_bridge.py`, `scripts/readiness/check.py`                   | verified result `exit_code` non-zero with `status` error; refusal `exact selected readiness checklist`   | re-fetch the same id; compare the job working directory with the owner config                       | readback: verified non-zero `exit_code` (terminal, no re-execution) or refusal exit 2                  |
| drifted-checkout    | Lenovo checkout revision differs from the expected revision             | Lenovo      | `scripts/readiness/check.py`, `docs/readiness.md`                                  | `lan-readiness-report/1` revision component `state: stale` (only when the owner config declares a revision); the report carries `jobId` and `outcome` | read the revision component; compare the expected value with a host `git rev-parse HEAD` readback   | readback: `state: stale` plus expected and actual revision; never `PASS`                               |
| consent-required    | the next step writes on Lenovo (checkout, process, credential, remote)  | Lenovo      | `scripts/dell_lan/lenovo_remote.py`, `scripts/dell_lan/lenovo_publish_existing.py` | request `authorized` list; refusal reason `consent-missing` exit 2                                       | compare the intended action with the recorded authorization list in the request JSON                | refusal: exit 2 `consent-missing` when the action is not listed; no side effect                        |

## Thread reconnect

A new agent thread that continues existing work reuses the same already-running
worker and the same signed job id. It never starts a process and never shares or
inherits another agent's job, ownership, or context.

1. Read `AGENTS.md` and this runbook. A reconnect repeats no scenario setup.
2. Take the job id from the previous receipt or its `JOBID=<32hex>` line. That
   id is the only continuation key; there is no second lookup store.
3. Fetch that id read-only with `scripts/lan_bridge/get.py --id <job-id>` or
   `fetch --id <job-id>`. Both never publish.
4. Keep the id until a MAC-verified result bound to it arrives. An expired
   bounded wait is `waiting` when a signed lease holds the job and `unknown`
   otherwise: re-fetch the same id, never publish a new one. A request or
   result file that fails its MAC, id, or format check is `refused` (exit 2)
   and stays the queue owner's to seal, never this agent's to replace.
5. With no observed id, report the missing handoff to the owner. An agent never
   claims an id it did not observe, and never reuses a foreign claim, start, or
   release record as its own.
6. A Lenovo-side write (checkout update, worker or cleaner start, credential
   change, push, merge request, undraft) needs the owner's separate per-action
   consent recorded before the action (`docs/dell-lan.md`, "Consent boundary").
   A reconnect does not inherit the previous agent's consent.
7. Stop at a verified readback (MAC-verified result bound to the id) or an
   explicit refusal. A missing receipt is `UNKNOWN`, never `PASS`.

## Repair boundary

- A repair is either a read-only re-read of the same receipt or an owner action
  named in the row. The agent never performs a live host, SMB, credential, or
  publication change on its own.
- Never infer an owner, a worker identity, or a deadline from a symptom. Name
  them only from a trusted receipt field or the owner's explicit input; a guess
  is `UNKNOWN`.
- Never create a second queue, worker, cleaner, transport, registry, or receipt
  store to work around a case in the table.
- Never paste, copy, or attach role log contents, key material, credential
  values, or private host paths into a report. Name the placeholder or the
  config key instead.
- Every recovery ends at its readback or refusal. No case ends with an assumed
  success, and no unknown is promoted to `PASS`.

## Local verification

The fixture map names the existing fast test closest to each diagnostic row.
The `What it proves` cell states the exact limit of that proof; a mapped test
is never treated as a live check.

| Case                | Proving test                                                                                                                | What it proves                                                            |
| ------------------- | --------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------- |
| queue-missing       | `tests/lan_bridge/test_lan_bridge_recovery.py::QueueAvailabilityTests::test_missing_queue_dir_refused_fast_no_remote_claim` | a missing queue refuses fast and claims no remote job                     |
| bad-mac-or-id       | `tests/lan_bridge/test_lan_bridge_recovery.py::RefusalAndHygieneTests::test_tampered_envelope_refused_peek_and_fetch`       | a tampered envelope is refused by both read paths                         |
| bad-mac-or-id       | `tests/readiness/test_readiness.py::ReadinessTests::test_bad_mac_is_refused`                                                | a bad MAC is a policy refusal, never a pass                               |
| unknown-no-result   | `tests/lan_bridge/test_lan_bridge_recovery.py::ConfirmedVsUnknownTests::test_absence_is_unknown_not_running`                | absence is `unknown`, never a running state                               |
| unknown-no-result   | `tests/readiness/test_readiness.py::ReadinessTests::test_timeout_then_same_id_retrieval_never_resubmits`                    | a timeout resumes the same id and never publishes again                   |
| stale-pid           | `tests/lenovo_worker/test_lenovo_launch.py::LaunchTests::test_stale_pid_recovers_after_reboot`                              | a dead recorded pid is recovered under the launch lock without duplicates |
| stale-pid           | `tests/lenovo_worker/test_lenovo_fix.py::RebootRecoveryTests::test_dead_bound_records_recover_without_duplicate`            | dead bound records recover without a duplicate process                    |
| worker-absent       | `tests/readiness/test_readiness.py::ReadinessTests::test_success_is_mac_bound_and_secret_free`                              | a ready report is a MAC-bound result bound to the job id and free of key material; the absent-consumer case is not exercised here |
| worker-foreign-pid  | `tests/lenovo_worker/test_lenovo_fix.py::RebootRecoveryTests::test_live_foreign_pid_still_refuses`                          | a live foreign pid refuses and nothing is touched                         |
| smb-auth            | `tests/lenovo_worker/test_lenovo_launch.py::LaunchTests::test_bad_credentials_refuse`                                       | bad credentials refuse                                                    |
| smb-network         | `tests/lenovo_worker/test_lenovo_launch.py::LaunchTests::test_unavailable_share_refuses`                                    | an unavailable share refuses                                              |
| unsupported-runtime | `tests/readiness/test_readiness.py::ReadinessTests::test_missing_component_is_not_success`                                  | a missing required component is never ready                               |
| wrong-cwd           | `tests/lenovo_worker/test_lenovo_fix.py::InvalidCwdTests::test_bad_cwd_seals_terminal_without_reexecution`                  | an invalid cwd seals one terminal result without re-execution             |
| drifted-checkout    | `tests/readiness/test_readiness.py::ReadinessTests::test_stale_component_is_not_success`                                    | a stale worker component yields `not-ready` with exit 1; the revision probe branch that reports it is not exercised by this fixture |
| drifted-checkout    | `tests/readiness/test_readiness.py::ReadinessTests::test_same_id_recovery_rejects_changed_submitted_checklist`              | a same-id resume is bound to the exact submitted job payload, so a changed checklist member is refused before any re-execution |
| consent-required    | `tests/dell_lan/test_dell_ready_consent.py::ReadyConsentTests::test_driver_refuses_ready_without_undraft_before_any_write`  | a missing authorization refuses before any write                          |

From the repository root:

```text
python -m unittest discover -s tests/runbook -p "test_*.py" -v
```

The checks read this document, `AGENTS.md`, and the referenced test files only.
They start no process, open no queue or share, use no key, and reach no host:
a document check is not a live check. Live host, SMB, and credential paths stay
`NOT_RUN` until an owner-observed readback is recorded, so this runbook never
claims more than the fixture map proves.

An incomplete role start leaves `<role>.starting`; the owner verifies the role before removing this marker.
