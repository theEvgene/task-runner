# task-runner

Empty repository baseline. Agent tooling only; no runtime source yet.

## Agent entry

Externally invoked agents start here explicitly; this repo assumes no global auto-discovery. Use only the script your intent needs:

- Monitor a GitLab MR pipeline via the signed Dell-to-Lenovo queue -> `docs/pipeline-observer.md`.
- Verify the target base and create a task worktree -> `docs/fresh-base.md`.
- Send one signed command through the LAN queue -> `docs/lan-bridge.md`.
- Run the full Dell-to-Lenovo dev loop (base, local work, publish, readback) -> `docs/dell-lan.md`.

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
