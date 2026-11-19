# Test design

Write contract and behavior tests at the cheapest level that proves the behavior. A disposable fixture that fails on the broken contract and passes on the fixed one beats a broad integration run that cannot name its assertion.

There is no coverage-percentage target: an arbitrary number measures lines, not the contracts that break in production. Add a test when a contract is unproven or a regression escaped; do not add tests to move a percentage.

## Selection in this repo

Run only the checks that exist:

- `npx lint-staged` (staged-file formatting).
- `node scripts/check-file-tokens.mjs` (staged-file token guard).
- `node scripts/check-file-tokens.mjs --base <base-SHA> --head <head-SHA>` (final range contents before review).
- `python -m unittest discover -s tests/<area> -v` (Python fixtures per
  area: `dell_lan`, `mr_ready`, `release`, `runbook`, `lenovo_worker`,
  `lan_bridge`, and others under `tests/`; disposable local Git
  repositories, the real signed queue, fixture doubles for external flows
  at their documented seams; no network, no live host).
- `node --test tests/fresh_base/` (Node fixtures for the base-only flow,
  same offline rules).

`npm run typecheck` / `npm run test` script entries are absent: there is no
TypeScript source to typecheck, and Node suites run directly through
`node --test`. Add a script entry only when that capability lands.

No project-specific suite map is transferred here. The toolkit-internal selector (`scripts/windows-tooling-suite-map.json` and its drift guard) belongs to the toolkit repository, not to this empty baseline.

Source: minimal transferable subset of `docs/agents/test-design-and-selection.md` in `personal-agent-toolkit-installed` (suite-map mechanics deliberately omitted).
