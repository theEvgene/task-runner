# task-runner

Empty repository baseline. Agent tooling only; no runtime source yet.

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
