# Two-host rollout (issue #133)

One supported scenario updates Dell and Lenovo to one explicitly chosen
published revision, installs the Jira CLI/skills, and verifies revision and
installed files per machine. Reuses `scripts/install-cli-jira-skills.mjs`
and the signed LAN route (`docs/lan-bridge.md`). No live run happens
without separate authorization.

## Inputs (owner-provided, never invented)

- Published `targetSha` (full SHA) plus `isLatest` (`true` only when the
  target is the verified `origin/main` tip).
- Dell checkout path, Lenovo checkout path (username-free: `Documents`
  `repositories\task-runner` resolved on Lenovo), Lenovo Node path.
- Skills dirs on each host, queue dir + 32-byte key file, LAN share dir.

## Freshness (before choosing isLatest)

`isLatest true` is allowed only when the target equals the actual remote
tip observed on Lenovo immediately before the rollout: run
`git ls-remote <origin-url> refs/heads/main` on Lenovo through the same
signed queue (or reuse a prior verified readback receipt for the same
target) and compare. Never derive `isLatest` on Dell. A stale Lenovo
checkout must `git fetch origin` first: a stale `origin/main` ref that
happens to equal an old target never counts as latest. A pinned older
revision always uses `isLatest false`.

## Steps

1. Verify the published target on Lenovo (`git ls-remote` there or a
   prior verified readback). Never guess it on Dell.
2. Build the Lenovo job (Dell, files only):
   `node scripts/two_host_rollout/request-rollout.mjs --node <abs> ...`.
   Submit with `scripts/lan_bridge/lan_bridge.py submit --dialect powershell
--expected-cwd <lenovo-checkout>` and observe the SAME id every 30 s for
   10 min (`docs/lan-bridge.md` observation rule: `fetch --timeout 600
   --poll 30` or `get.py --window 60 --poll 30 --max-windows 10`). A
   short-timeout expiry is **unknown** with the same-id resume action:
   never resubmit while unknown.
3. Run the Dell host step locally with the same target:
   `node scripts/two_host_rollout/apply-host.mjs --checkout <dell> ...`.
4. Collect the Lenovo receipt ONLY from the MAC-verified same-job fetch
   stdout (never hand-copy a receipt file):
   `node scripts/two_host_rollout/collect-lenovo-result.mjs --queue
   <queue> --key-file <key> --job-id <JOBID> --target-sha <sha> --is-latest
   true|false --out <lenovo.receipt.json> --bridge
   scripts/lan_bridge/lan_bridge.py --python <python> --fetch-timeout 600
   --poll 30`.
   The script binds the sealed job command to this rollout, checks
   transport (`timed_out`, `output_truncated`, status) then the stdout
   receipt (schema, target, flag, per-host rules), and writes `--out`.
   Exit `0` collected (a refused host verdict is still saved and combines
   to `partial`); exit `1` terminal mismatch/failure (NEW id only after a
   fix, never automatic); exit `3` unknown (slot stays occupied, re-fetch
   the SAME id).
5. Combine both receipts:
   `node scripts/two_host_rollout/verify-readback.mjs --dell-receipt
   <dell.receipt.json> --lenovo-receipt <lenovo.receipt.json
   (collect --out)> --target-sha <sha> --is-latest true|false`.
   Report `updated` only when both receipts verify against the same
   target.

## Per-host rules (enforced by apply-host)

- Latest (`--is-latest true`): clean checkout, `target == origin/main`,
  local `main` ancestry required; finish on branch `main` with
  `HEAD == target`, no detached HEAD, no force over divergent commits.
- Pinned older (`--is-latest false`): clean checkout, detached `HEAD` at
  target, `main` ref untouched, never declared current.
- Refusals name the cause and actual state: `dirty-checkout`,
  `diverged`, `wrong-branch`, `revision-mismatch`,
  `ownership-conflict` (foreign/stale skills, never overwritten),
  `partial-install`. Partial success is `partial`, never `updated`.
  Rerun with the same revision is safe; success leaves no `*.install-*`
  or `*.bak` backups and no secrets in argv or receipts.

## Verification (local only)

```text
node --test tests/two_host_rollout/rollout-lib.test.mjs tests/two_host_rollout/rollout-scenario.test.mjs tests/two_host_rollout/apply-host.test.mjs tests/two_host_rollout/collect-result.test.mjs
node scripts/check-file-tokens.mjs --all
```
