---
name: two-host-rollout
description: Roll out one published task-runner revision to Dell and Lenovo and verify both hosts. Use when both machines must converge on one revision with installed Jira CLI/skills.
---

# Two-host rollout

Single entry: `docs/two-host-rollout.md` in the task-runner checkout.
Dual-repo runs (task-runner + PAT, issue #162): `docs/two-host-dual-rollout.md`
with `request-dual-rollout` / `apply-dual-host` / `collect-dual-result` /
`verify-dual-readback` (two pins, four combos, same JOBID rule).
No second instruction exists. A live run needs separate authorization;
shipping this skill proves nothing live.

## Run

1. Confirm the published `targetSha` and whether it is the verified
   `origin/main` tip (`isLatest true`, proven by `git ls-remote` on
   Lenovo or a prior verified readback, never guessed on Dell; the
   executable still re-proves it with its own `git fetch -q origin`
   readback) or an explicitly pinned older revision (`isLatest false`,
   never declared current).
2. Build the Lenovo job with
   `scripts/two_host_rollout/request-rollout.mjs`, submit through
   `scripts/lan_bridge/lan_bridge.py submit --queue <dir> --key-file
   <key> --command-file <cmd> --cwd <lenovo-checkout> --timeout
   <seconds> --max-output <bytes> --dialect powershell --expected-cwd
   <lenovo-checkout>` (cwd binding per `docs/two-host-rollout.md`),
   observe the SAME id every 30 s for 10 min (`fetch --timeout 600
   --poll 30`); a short-timeout expiry is unknown: re-fetch the SAME id,
   never submit a new one.
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
  `current` (latest additionally requires `branch main` with
  `HEAD == origin/main == target`).
- `partial` + `dirty-checkout`/`diverged`/`wrong-branch`/
  `revision-mismatch`/`ownership-conflict`/`install-failed`: the named
  host refused; pre-mutation refusals leave the checkout untouched,
  post-mutation `install-failed` reports the moved `HEAD` with no
  destructive reset; fix per its `nextAction` and rerun the same
  revision. `unknown` (exit `3`) re-fetches the SAME id.
- Secrets never travel the queue; success leaves no backups.
