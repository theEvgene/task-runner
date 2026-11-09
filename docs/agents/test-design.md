# Test design

Write contract and behavior tests at the cheapest level that proves the behavior. A disposable fixture that fails on the broken contract and passes on the fixed one beats a broad integration run that cannot name its assertion.

There is no coverage-percentage target: an arbitrary number measures lines, not the contracts that break in production. Add a test when a contract is unproven or a regression escaped; do not add tests to move a percentage.

## Selection in this repo (empty baseline)

Run only the checks that exist:

- `npx lint-staged` (staged-file formatting).
- `node scripts/check-file-tokens.mjs` (staged-file token guard).
- `npm run typecheck` / `npm run test`: **absent**. This repo has no source yet, so there is nothing to typecheck or test. Omit them from the hook. Add the corresponding script and its hook line only when that capability lands (typed source → `typecheck`; first contract test → `test`).

No project-specific suite map is transferred here. The toolkit-internal selector (`scripts/windows-tooling-suite-map.json` and its drift guard) belongs to the toolkit repository, not to this empty baseline.

Source: minimal transferable subset of `docs/agents/test-design-and-selection.md` in `personal-agent-toolkit-installed` (suite-map mechanics deliberately omitted).
