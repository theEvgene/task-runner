# Fresh base from a verified Lenovo copy (issue #4)

> Agent entry. Use ONLY this scenario when: you must cut a task worktree and
> the base branch may have moved since you last looked. It gives you a
> MAC-verified base SHA from Lenovo over the existing signed queue, imports
> the copy once, and re-checks the tip before push. Do NOT use it for: pushing
> or opening a merge request (that is the publish flow), moving the owner's
> checkout, or reading Jira (that is the task bridge). Dell never contacts
> the corporate remote; only Lenovo reads it.

## Trigger

Target branch may have advanced between base selection and work start
(parent issue: stale base forced a rebase before publication).

## Required inputs (owner-provided, never invented)

Queue dir + 32-byte key file (both outside every repo, the queue, and the
LAN share), LAN share dir, Lenovo checkout path, Lenovo skill dir, Node on
Lenovo, Python on Dell (runs the existing `lan_bridge.py` client), project
path for `--expected-project`.

## Exact commands

All paths below are placeholders.

1. Request the base (Dell, files only):

```text
node scripts/fresh_base/request-base.mjs --shared <share> --token <16hex>
  --lenovo-skill-dir <dir> --node <abs-node-on-lenovo>
  --lenovo-checkout <abs-lenovo-checkout> --ref-full refs/heads/<branch>
  --request-name base-request-<16hex>.json --command-file <cmd-file>
  --repo <dell-home>
```

2. Submit through the existing standard client (Dell):

```text
python <lan_bridge.py> submit --queue <queue> --key-file <key>
  --command-file <cmd-file> --cwd <abs-lenovo-checkout>
  --timeout 300 --max-output 65536
  --return-file <share>\fresh-base-<16hex>.manifest.json
```

Prints `JOBID=<32hex>`. Only this client seals jobs; nothing here
reimplements the envelope, MAC, or queue.

3. Fixture worker (Lenovo; in production the owner-started worker):

```text
node scripts/fresh_base/lenovo-base-export.mjs
  --request <share>\base-request-<16hex>.json --request-sha256 <digest>
```

4. Import once (Dell; consumes the MAC-verified result via `fetch`):

```text
node scripts/fresh_base/import-base.mjs --queue <queue> --key-file <key>
  --job-id <job-id> --shared <share> --repo <dell-home>
  --repo-out <new-source-copy> --branch <branch>
  --expected-project <group/project> --receipt <receipt>.json
  --bridge <lan_bridge.py> --python <python> --provenance fixture|live
```

5. Cut one detached task worktree (Dell):

```text
node scripts/fresh_base/create-worktree.mjs --repo <new-source-copy>
  --receipt <receipt>.json --worktree <task-worktree>
```

6. Before push, re-probe the tip (Dell + Lenovo worker) and gate on it:

```text
node scripts/fresh_base/request-tip.mjs ... (same shape as step 1)
python <lan_bridge.py> submit ... (same shape as step 2)
node scripts/fresh_base/preflight.mjs --queue <queue> --key-file <key>
  --job-id <tip-job-id> --receipt <receipt>.json
  --bridge <lan_bridge.py> --python <python>
```

## Outcomes

- `import-base`: `0 imported` (receipt `fresh-base-import/1` binds repo,
  branch, verified SHA, copy HEAD); `1` refused/mismatch (nothing created);
  `2` usage; `3` unknown — slot stays occupied, re-fetch the SAME job id,
  never a new one.
- `preflight`: `0` fresh; `2` moved/stale (rebase required); `1` verify
  failure; `3` unknown (re-fetch the SAME job id).
- `create-worktree`: `0` with `HEAD == baseSha`; `1` otherwise.

## Boundaries

- One source copy, imported once at an empty path, never replaced; one
  detached task worktree; one writer.
- The user checkout, other worktrees, and existing queue slots are never
  modified (only new slots are appended).
- The key travels via `--key-file` only and is refused inside the queue, a
  checkout, or the share; it never enters a receipt.
- An early verified SHA is NOT a push-time guarantee: branch motion after
  the check is expected, and the signed `preflight` before push is
  mandatory. A receipt marked `provenance: fixture` is never live evidence.

## Verification (local fixtures only)

```text
node --test tests/fresh_base/
```

Covers: signed happy path with HEAD binding, unknown branch, swapped
bundle bytes, timeout-then-same-id recovery, probe-fresh vs probe-moved,
and preservation of copy, sibling worktree, key, and sealed slots.
Fixtures use a local bare remote plus a fixture Lenovo checkout; they
never reach Lenovo, Jira, or the network.
