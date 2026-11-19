---
name: two-host-rollout
description: Roll out one published task-runner revision to Dell and Lenovo and verify both hosts. Use when both machines must converge on one revision with installed Jira CLI/skills.
---

# Two-host rollout

Single entry: `docs/two-host-rollout.md` in the task-runner checkout.
No second instruction exists. A live run needs separate authorization;
shipping this skill proves nothing live.

## Run

1. Confirm the published `targetSha` and whether it is the verified
   `origin/main` tip (`isLatest true`, proven by `git ls-remote` on
   Lenovo or a prior verified readback, never guessed on Dell) or an
   explicitly pinned older revision (`isLatest false`).
2. Build the Lenovo job with
   `scripts/two_host_rollout/request-rollout.mjs`, submit through
   `scripts/lan_bridge/lan_bridge.py` (`--dialect powershell`,
   `--expected-cwd` per `docs/lan-bridge.md`), observe the SAME id every
   30 s for 10 min; never resubmit while unknown.
3. Run `scripts/two_host_rollout/apply-host.mjs` on Dell with the same
   target.
4. Save the Lenovo receipt ONLY with
   `scripts/two_host_rollout/collect-lenovo-result.mjs` from the
   MAC-verified same-job fetch stdout (`--fetch-timeout 600 --poll 30`
   live), then combine both receipts with
   `scripts/two_host_rollout/verify-readback.mjs`. Report `updated`
   only when both hosts verify.

## Read the result

- `updated`/`already-current`: both hosts on the target, clean, skills
  `current`.
- `partial` + `dirty-checkout`/`diverged`/`wrong-branch`/
  `revision-mismatch`/`ownership-conflict`: the named host refused and
  nothing there was overwritten; fix per its `nextAction` and rerun the
  same revision. `unknown` (exit `3`) re-fetches the SAME id.
- Secrets never travel the queue; success leaves no backups.
