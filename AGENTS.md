# task-runner

Empty repository baseline. Agent tooling only; no runtime source yet.

## Agent entry

Externally invoked agents start here explicitly; this repo assumes no global auto-discovery. Use only the script your intent needs:

- Diagnose a worker, queue, SMB, or resumed-thread problem before any repair (one table: case, trusted receipt field, read-only action, stop/readback) -> `docs/runbook.md`.
- Monitor a GitLab MR pipeline via the signed Dell-to-Lenovo queue -> `docs/pipeline-observer.md`.
- Prepare an existing GitLab MR for Ready on Lenovo -> `docs/mr-ready.md`.
- Verify the target base and create a task worktree -> `docs/fresh-base.md`.
- Send one signed command through the LAN queue -> `docs/lan-bridge.md`.
- Run Lenovo-hosted Jira/DCO scripts through the signed queue -> `docs/lenovo-hosted-scripts.md` (generic `lan_bridge` submit/fetch/observe; credentials stay in the Lenovo host environment; DCO plan/state stay Lenovo-local; Jira keeps its two-phase confirm flow).
- Diagnose a stuck/failed signed LAN queue job (same JOBID every 30 s for 10 min; timeout is unknown, never a resubmit) -> `docs/lan-queue-diagnosis.md`.
- Check readiness for one selected Dell-to-Lenovo scenario -> `docs/readiness.md` + `scripts/readiness/check.py`.
- Start the Lenovo worker and cleaner with one shortcut after a reboot (existing SMB share, local credentials) -> `docs/lenovo-worker-shortcut.md`.
- Import the first verified copy of a remote branch with no Lenovo checkout -> `docs/first-import.md`.
- Run the full Dell-to-Lenovo dev loop (base, local work, publish, readback) -> `docs/dell-lan.md`.
- Transfer one branch to Lenovo without publication, or publish a branch that already exists there -> `docs/dell-lan-standalone.md` (drivers `lenovo_transfer.py`, `lenovo_publish_existing.py`; combined `publish` route in `docs/dell-lan.md` unchanged).
- Release on Lenovo through the signed queue (bare preview; versioned tag only with explicit version plus explicit confirmation) -> `docs/release.md` + `scripts/release/request.py` (single canonical release entry; `docs/release-tag-preview.md` stays in the tree as legacy and is not an entry).
- Install this repository's skill links (managed junctions under `~/.agents/skills`, never overwriting foreign entries) -> `skills/release/SKILL.md` + `skills/release/scripts/install.ps1` (PAT-owned links migrate only with explicit opt-in and exact ownership proof).

Corporate routing and the signed queue apply where those docs state them. Do not pass secrets through task scripts, and rely only on guarantees those docs state.

## Agent skills

### Issue tracker

Issues live in GitHub Issues for `theEvgene/task-runner`. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five canonical triage roles, label string equal to role name. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout: root `GLOSSARY.md` + `docs/adr/`. See `docs/agents/domain.md`.

### Test design

Contract tests at the cheapest proving level; no coverage-percentage target. See `docs/agents/test-design.md`.

### File token guard

One file must not exceed ~5,500 tokens (staged guard in pre-commit). See `docs/agents/file-token-guard.md`.

### Release

Standalone-installable `release` skill (preview-first Lenovo release pointer, managed junctions, opt-in old-link migration). See `skills/release/SKILL.md`.

### LAN operations

Repository-owned pointers for the canonical Dell development and signed bridge
flows live in `skills/dell-lan-local-dev/SKILL.md` and
`skills/lan-task-bridge/SKILL.md`.
