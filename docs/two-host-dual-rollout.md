# Dual-repo two-host rollout (issue #162, includes #159)

One verifiable run moves Dell and Lenovo to two explicitly chosen pinned
revisions — one for task-runner, one for Personal Agent Toolkit — installs
both skill sets on each host, and proves all four combos
(2 hosts x 2 repos). Each repo keeps its native installer and its own
receipt scheme; this flow only bundles their evidence. A live run needs
separate authorization.

## Inputs (owner-provided, never invented)

- `taskRunnerSha` (full SHA) plus `isLatest` (`true` only when it is the
  verified `origin/main` tip; freshness is re-proven by
  `apply-host.mjs` itself via `git fetch -q origin`).
- `patSha` (full PAT SHA). PAT freshness is the pinned commit itself:
  `bootstrap/rollout.ps1` resolves `-Revision` locally (no fetch) and
  records it as `pinnedRevision`.
- Task-runner checkout + skills dir per host; PAT installed checkout
  (durable, outside the temp dir — the PAT rollout refuses a temporary
  checkout) + skills dir per host, plus PAT Codex/OpenCode/practices dirs
  where they differ from defaults. Queue dir + 32-byte key file as usual.

## Native installers and receipts (observed contracts, not invented)

- Task-runner half: `scripts/two_host_rollout/apply-host.mjs`. Receipt
  schema `two-host-rollout-host/1` with checkout, `targetSha`, branch,
  `head`, `originMain`, `clean`, `skills.state`. Rules live in
  `docs/two-host-rollout.md` (junction vs managed-copy recognition from
  #159, no reset/force, no secrets).
- PAT half: `<pat-checkout>/bootstrap/rollout.ps1 -Revision <sha>
  -InstalledCheckout <dir> -SkillsDirectory <dir> -ReceiptPath <file>`.
  Receipt schema `personal-agent-toolkit/skill-rollout@1`; success needs
  `ok: true`, pinned == installed == requested SHA,
  `verification.verified: true` with exactly one verification pass, a
  clean checkout, and empty mismatched/errors/problems lists (see that
  repo's `docs/agents/skill-rollout.md`).
- Dual-host receipt: schema `two-host-dual-host/1` bundling both halves
  plus both checkout paths, so each host/repo combo shows checkout,
  revision, install result and readback.

## Steps

1. Verify both pins on Lenovo (`git ls-remote` there or a prior verified
   readback). Never guess on Dell.
2. Build the Lenovo job (Dell, files only):
   `node scripts/two_host_rollout/request-dual-rollout.mjs --node ...`.
   Submit through the signed client with the exact flags from
   `docs/two-host-rollout.md` (same `--cwd`/`--expected-cwd` binding) and
   observe the SAME id every 30 s for 10 min. A short-timeout expiry is
   **unknown**: re-fetch the SAME id, never submit a new one.
3. Run the Dell dual step locally:
   `node scripts/two_host_rollout/apply-dual-host.mjs --checkout ...`.
   TR half runs first through its native installer; on TR refusal PAT is
   untouched. On PAT refusal the moved TR HEAD is reported honestly with
   no rollback.
4. Save the Lenovo receipt ONLY with
   `node scripts/two_host_rollout/collect-dual-result.mjs ...` from the
   MAC-verified same-job fetch stdout (`--fetch-timeout 600 --poll 30`).
   Exit `3` is unknown (re-fetch the SAME id); exit `1` is terminal
   mismatch (NEW id only after fixing inputs).
5. Combine:
   `node scripts/two_host_rollout/verify-dual-readback.mjs ...`.
   `updated` only when all four combos verify against the same two pins;
   anything else is `partial`/`mismatch` naming the cause and the safe
   next action.

## Refusals (fail closed)

Foreign links, changed copies, mixed models, dirty/diverged checkouts and
PAT checkout mismatches refuse before any overwrite; no `reset --force`,
no secret ever enters argv or a receipt. Partial success is `partial`,
never `updated`. Rerun with the same two revisions is safe: both halves
detect already-current and change nothing.

## Diagnose

Single-repo diagnosis is unchanged (`diagnose-checkouts.mjs` for the
task-runner checkout, PAT `verify-install.ps1` readback for the PAT
checkout). Facts are point-in-time; the dual apply reverifies both
immediately before any authorized change.

## Verification (local only)

```text
node --test tests/two_host_rollout/dual-lib.test.mjs tests/two_host_rollout/dual-scenario.test.mjs
node scripts/check-file-tokens.mjs --all
```

Fixture runs prove the contracts offline only; a live rollout is proven
solely by the four verified receipts, never by fixtures.
