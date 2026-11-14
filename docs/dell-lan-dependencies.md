# Restore Dell dependencies

## 3b. Restore Dell dependencies in one batch (mixed public/closed lock)

When the probe reports `cache-miss` for a lock that mixes public and closed
packages, run one batch restoration instead of per-package requests:

```text
python scripts/dell_lan/dell_deps_restore.py \
  --project <abs-task-worktree> --cache-dir <abs-owned-cache> \
  --scratch-root <abs-scratch> --receipt <outside-every-checkout>\deps.json \
  --public-registry https://registry.npmjs.org \
  --queue <queue-dir> --key-file <key-path> --shared <lan-share> \
  --lenovo-python <abs-python-on-lenovo> \
  --lenovo-driver-dir <abs-dell-lan-dir-on-lenovo> \
  --lenovo-source-dir <abs-lenovo-tarball-dir>
```

What it does, in order: checks Node/npm against `engines` and the
lockfile version; classifies every locked entry on verifiable data only
(public means an `http(s)` URL on exactly the named `--public-registry`
host plus a well-formed integrity — any other host, including a
corporate proxy that serves both, is private; missing integrity or an
unknown scheme is never public; `git`/bundled entries stop the run);
probes the owned cache offline in a disposable copy; pulls the missing
closed packages in ONE batch through the existing signed queue
(`scripts/dell_lan/lenovo_deps_fill.py` runs as the single Lenovo
command, ships tarballs to the share, Dell re-verifies every byte
against the lock before use); fills the owned cache with one explicit
`npm ci --prefer-offline` in a disposable copy; finishes with a true
`npm ci --offline` plus the project's own `npm test` in a fresh
disposable copy. The source project and its lockfile stay byte-identical;
`node_modules` is never created in the worktree. Scoped closed packages
travel too: stage `@scope/pkg@1.0.0` on the Lenovo side as the single
contained file `@scope-pkg-1.0.0.tgz` (the one slash becomes a dash);
anything that is not stageable as one contained name refuses the batch.

Completion: receipt `dell-deps-restore/1` with `verdict: ready` (or
`ready-tests-skipped` with `--skip-tests` plus a reason). Every other
verdict stops the run before install: `unknown-classification`,
`sourcing-limited`, `transfer-unconfigured`, `transfer-refused`,
`integrity-mismatch` (a transferred tarball fails the lock check or the
batch is incomplete), `corporate-fetch-refused`, `fill-failed`,
`install-failed`, `tests-failed`, `failed`, `source-changed`, `unknown`
(exit 3 — reconcile the job before any retry). `corporate-fetch-refused`
is a defense-in-depth tripwire: the classifier already files every
off-host entry as private, so it cannot fire today, but the guard stays
to catch a future classifier regression. There is no resume from
an old receipt: every run deletes any prior receipt at start and
re-probes, `lockSha256` and `manifestSha256`
are re-hashed at the end of the run (a mid-run edit yields
`source-changed`), the cache fingerprints hash file contents (a
same-size byte swap with a preserved mtime still changes them), every
refusal path deletes the receipt first, and an interrupted run that never
reaches a terminal verdict leaves no receipt at all (absent, never a
stale `ready`).

Network posture, stated exactly: every npm child environment strips
proxy variables and registry auth material, so npm talks directly to the
named public registry host or the `127.0.0.1` stub — a corporate proxy
is never an implicit fetch path, and Dell holds no corporate
credentials. Closed bytes travel only the signed queue. Residual limits:
the fill step is explicitly networked, lifecycle scripts run with the
network up during fill, and npm `--offline` constrains registry fetches
only, not script sockets — a hostile lifecycle script could use the
network during fill. Mitigations are the exact-pin allow-list
(`--strict-allow-scripts` over precisely the locked packages, or
`--ignore-scripts` with a recorded reason), never a sandbox claim.
Share artifacts accumulate (needs/export/tarballs, no TTL): a token
collision fails closed — re-run with a fresh token. No new transport,
no new secrets: the queue key travels via `--key-file` under the same
containment rule as every other driver.
