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

## Freshness (enforced in the executable path)

`isLatest true` still requires the caller to pass the verified
`origin/main` tip, but the executable never trusts that flag alone:
`apply-host.mjs` runs `git fetch -q origin` itself with the host git
credentials and then requires `target == origin/main` (fetched readback).
A stale local `origin/main` that happens to equal an old target therefore
refuses as `revision-mismatch` once the fetch moves the ref. A pinned older
revision always uses `isLatest false`: it skips the freshness fetch, checks
out detached `HEAD` at the target, leaves `main` untouched, and is never
declared current. Final verification on both hosts is branch `main` with
`HEAD == origin/main == target` for latest; `verify-readback.mjs` and
`lib.verifyLenovoPayload` enforce the same `originMain` binding.

## Steps

1. Verify the published target on Lenovo (`git ls-remote` there or a
   prior verified readback). Never guess it on Dell.
2. Build the Lenovo job (Dell, files only):
   `node scripts/two_host_rollout/request-rollout.mjs --node <abs> ...`.
   Submit through the signed client with the exact flags:

   ```text
   python scripts/lan_bridge/lan_bridge.py submit \
     --queue <queue-dir> --key-file <key-path> \
     [--key-format raw|base64] \
     --command-file <cmd-file> --cwd <lenovo-checkout> \
     --timeout <remote-seconds> --max-output <bytes> \
     --dialect powershell --expected-cwd <lenovo-checkout>
   ```

   `--cwd` is the worker's working directory; `--expected-cwd` carries the
   same absolute Lenovo checkout inside the MAC and the worker refuses
   (`target-cwd-mismatch`, sealed terminal error) before claiming or
   spawning anything when they differ. Omit `--expected-cwd` only when the
   target cannot be pinned: the job then stays `unverified-target`. Observe
   the SAME id every 30 s for 10 min (`docs/lan-bridge.md` observation
   rule: `fetch --timeout 600 --poll 30` or `get.py --window 60 --poll 30
   --max-windows 10`). A short-timeout expiry is **unknown** with the
   same-id resume action: re-fetch the SAME id, never submit a new id while
   unknown. Report `no response` only after the full 600 s watch.
3. Run the Dell host step locally with the same target:
   `node scripts/two_host_rollout/apply-host.mjs --checkout <dell> ...`.
4. Collect the Lenovo receipt ONLY from the MAC-verified same-job fetch
   stdout (never hand-copy a receipt file) with the exact flags:
   `node scripts/two_host_rollout/collect-lenovo-result.mjs --queue
   <queue> --key-file <key> [--key-format raw|base64] --job-id <JOBID>
   --target-sha <sha> --is-latest true|false --out <lenovo.receipt.json>
   --bridge scripts/lan_bridge/lan_bridge.py --python <python>
   --fetch-timeout 600 --poll 30`.
   The fetch inside is the signed `lan_bridge.py fetch --queue <queue-dir>
   --key-file <key-path> [--key-format raw|base64] --id <same-job-id>
   --timeout 600 --poll 30`. The script binds the sealed job command to
   this rollout, checks transport (`timed_out`, `output_truncated`,
   status) then the stdout receipt (schema, target, flag, per-host rules
   including `branch/main` and `originMain` for latest), and writes
   `--out`. Exit `0` collected (a refused host verdict is still saved and
   combines to `partial`); exit `1` terminal mismatch/failure (NEW id only
   after a fix, never automatic); exit `3` unknown (slot stays occupied,
   re-fetch the SAME id with the same `fetch --timeout/--poll`).
5. Combine both receipts:
   `node scripts/two_host_rollout/verify-readback.mjs --dell-receipt
   <dell.receipt.json> --lenovo-receipt <lenovo.receipt.json
   (collect --out)> --target-sha <sha> --is-latest true|false`.
   Report `updated` only when both receipts verify against the same
   target.

## Per-host rules (enforced by apply-host)

- Latest (`--is-latest true`): `git fetch -q origin` first, then clean
  checkout, `target == origin/main` (fetched), local `main` ancestry
  required; finish on branch `main` with `HEAD == origin/main == target`,
  no detached HEAD, no force over divergent commits.
- Pinned older (`--is-latest false`): clean checkout, detached `HEAD` at
  target, `main` ref untouched, never declared current.
- Ownership is preflighted against the working tree before any checkout
  mutation (same bytes the installer hashes) with the target blobs proven
  present first, and revalidated immediately after mutation:
  `ownership-conflict` (foreign/stale skills, never overwritten) refuses
  with the checkout untouched when detected pre-mutation, and with the new
  `HEAD` readback when detected post-mutation. No destructive `git reset
  --hard` rollback is attempted; post-update `install-failed` reports the
  moved `HEAD` instead of hiding it.
- Refusals name the cause and actual state: `dirty-checkout`,
  `diverged`, `wrong-branch`, `revision-mismatch`,
  `ownership-conflict` (foreign/stale skills, never overwritten),
  `partial-install`, `install-failed`, `git-failed`. Partial success is
  `partial`, never `updated`. Rerun with the same revision is safe;
  success leaves no `*.install-*` or `*.bak` backups and no secrets in
  argv or receipts.

## Verification (local only)

```text
node --test tests/two_host_rollout/rollout-lib.test.mjs tests/two_host_rollout/rollout-scenario.test.mjs tests/two_host_rollout/apply-host.test.mjs tests/two_host_rollout/collect-result.test.mjs
node scripts/check-file-tokens.mjs --all
```
