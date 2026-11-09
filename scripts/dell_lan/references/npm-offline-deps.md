# npm offline dependencies on Dell

How a Dell agent evaluates a changed `package-lock.json` against an owned npm
cache without touching the real worktree and without claiming more than the
probe proves. This reference is reached only from the npm branch of
[the workflow](../../docs/dell-lan.md): every other run never loads it.

## Owned cache and disposable probe

- The cache is one directory the Dell owner holds, outside every checkout, the
  queue, and the shared LAN folder, passed as an absolute `--cache-dir`. npm
  owns its layout; the agent only names the root and never hand-edits inside
  it. The cache is portable: a directory filled during one authorized online
  window is the same directory later reads run against. Containment is proven
  by canonical path identity (symlinks, junctions, `subst` drives, 8.3 names,
  case and separators, as in `dell_lan_common.containment`): the probe refuses
  when the cache is inside the project or the scratch root, when either is
  inside the cache, when the scratch root is inside the project (or vice
  versa), or when separation is unprovable — before creating the cache
  directory.
- The probe never installs in the real worktree. `dell_npm_probe.py` copies
  the whole project tree (excluding `node_modules`, `.git`, and any `.npmrc`
  at any level) into a fresh directory under an absolute `--scratch-root`
  (outside every checkout) and runs `npm ci --offline` there, then deletes
  the copy. Workspace members and files read by lifecycle scripts travel with
  the copy, so a local `workspaces` layout is evaluated as-is instead of
  failing on a missing member. Re-evaluation after a lock or manifest change
  is a re-run, and the receipt binds the evaluated content by both
  `lockSha256` and `manifestSha256`.

## The loop

1. Record the evaluated content: `lockSha256` of `package-lock.json` and
   `manifestSha256` of `package.json` in the worktree. A changed lock or a
   changed manifest invalidates the previous receipt.
2. Probe offline only:

   ```powershell
   python scripts/dell_lan/dell_npm_probe.py `
     --project <abs-task-worktree> --cache-dir <abs-owned-cache> `
     --scratch-root <abs-scratch> --receipt <outside-every-checkout>\npm-probe.json
   ```

   Completion: receipt `dell-npm-probe/1` exists with one verdict. Exit 0
   means `ready`; exit 1 carries `cache-miss`, `sourcing-limited`, or
   `failed` with a next action.

3. On `cache-miss`, fill online exactly once, in a separate explicitly
   networked step on Dell (never inside the probe, never claimed offline):

   ```powershell
   npm ci --prefer-offline --no-audit --no-fund --cache <abs-owned-cache>
   ```

   run in a disposable copy of the same lock content, then re-probe offline.
   A `--prefer-offline` success is not offline evidence: it may have used
   the network. Only the `--offline` re-probe verdict `ready` proves the
   cache covers the lock.

4. On `sourcing-limited`, fix the source first (below), then re-probe. On
   `failed`, fix the npm error and re-probe; never claim offline-readiness
   from a red probe.

## Reading the verdicts

- `ready`: the owned cache covers this exact lock and manifest. The receipt
  names the npm version, the full `npmArgv` (which always carries an explicit
  `--ignore-scripts=false` or `--ignore-scripts`), and both digests
  (`lockSha256`, `manifestSha256`).
- `cache-miss`: the cache has no entry for a locked tarball (`ENOTCACHED`,
  "only-if-cached"). The receipt lists the missing URLs or package names in
  `missing`. This is a cache gap, not a source problem: fill, then re-probe.
- `sourcing-limited`: the lock names something the tarball cache cannot
  supply on its own — a `git+ssh` dependency, an authenticated private
  registry entry (`ENEEDAUTH`, 401/403), or a platform-gated optional
  dependency (`EBADPLATFORM`). Filling cannot fix the category: vendor the
  credential or the platform outside the cache, document which, and re-probe.
- `failed`: anything else. The receipt carries the exit code and a capped
  `stderrExcerpt`; the run proves nothing about the cache.

## What the probe cannot prove

- A `ready` receipt binds one lock digest, one manifest digest, one npm
  version, and one cache directory path at probe time. It does not bind cache
  content or the Node version: any cache write or eviction after the probe, or
  any Node upgrade, invalidates the receipt and needs a new probe. A lock
  change, a manifest change, an npm upgrade, or a different cache path also
  needs a new probe; an old receipt never covers new content.
- The cache is a tarball store, not a registry mirror. It cannot vouch for a
  package the registry has since removed, a credential the agent does not
  hold, or a lifecycle script that downloads at install time.
- No Lenovo, LAN, or GitLab step is involved. The probe runs on Dell only
  with `npm ci --offline`, which constrains npm registry access only; it does
  not prevent network I/O from lifecycle scripts, which run under the agent
  sandbox network policy. A strict air-gap evaluation must run the probe
  without network.
- The scratch copy excludes every `.npmrc`, which changes effective install
  conditions versus the real worktree. For default-registry projects behavior
  is identical; for custom-registry, auth, or scope projects the probe may
  report `cache-miss` or `failed` where the configured install would succeed,
  and a `ready` then vouches for the no-rc configuration, not the configured
  worktree one.
- The copy preserves symlinks as-is and trusts the task worktree contents: a
  symlink naming a path outside the scratch copy is not contained by the
  directory checks, so install or script writes through it can escape scratch.
  Cleanup removes the link itself, but write-through during install is
  untested.

## Lifecycle scripts

Scripts run unless the caller explicitly disables them. The probe always
passes an explicit scripts flag (`--ignore-scripts=false` by default,
`--ignore-scripts` only with `--ignore-scripts-reason`) and strips any
inherited `npm_config_ignore_scripts` value from the child environment, while
project `.npmrc` files are never copied into the scratch copy — so neither
an environment setting nor a project rc file can silently disable scripts
(see the `.npmrc` fidelity limit above: the exclusion also changes
registry/auth/scope conditions versus the configured worktree).
The receipt's `npmArgv` plus `ignoreScripts` is the audit point: a `ready`
receipt without the disable flag asserts scripts ran, because the effective
configuration could not have come from anywhere else. A `ready` receipt with
`ignoreScripts: true` proves only that the tarballs unpack without scripts,
never that the project installs with them.
