# Release skill: install and update

Instructions for future agents working in this checkout. Nothing here
touches the network: every step resolves locally and refuses instead of
reaching out.

## Install (first time, or a new machine)

From a clean checkout of `theEvgene/task-runner` at the revision you want
to install:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File skills\release\scripts\install.ps1 `
  -Revision origin/main `
  -SkillsDirectory "$HOME\.agents\skills"
```

What it does: refuses on a dirty checkout, resolves `-Revision` to a SHA
locally (no fetch), advances this checkout detached to that SHA when
needed, links every `skills/*/SKILL.md` of the PINNED revision as a
junction under the skills directory, then reads everything back and
prints a JSON receipt (`schema: task-runner/skill-rollout@1`).

Rules that are never relaxed:

- A foreign entry under the target name (another repo's checkout, a
  plain directory or file, anything that is not our junction and not a
  proven same-repo managed link) is a conflict: it is reported in the
  receipt and left unchanged, and the install fails.
- A junction into another checkout of this same repo (origin remote
  slug matches `theEvgene/task-runner`, exact `skills/<name>` shape
  with `SKILL.md`) is retargeted to this checkout: the installer
  re-checks the link immediately before the swap and records the name
  in `retargetedManagedLinks`. A changed link refuses instead of
  swapping.
- Recorded PAT links migrate only with `-AllowOldLinkMigration`. The target
  must be a junction into the exact recorded path of a checkout whose origin is
  `personal-agent-toolkit`, and its frontmatter and body marker must match
  `migrations.json`. The guarded swap removes only the junction; the old source
  is retained. Without the switch, or on any doubt, the entry stays untouched.
- No global guidance files are written. This repo assumes no global
  auto-discovery; global files may belong to another checkout.

## Update (move the installed links to a newer revision)

1. Inspect first: `git status --porcelain` must be empty and
   `git rev-parse HEAD` recorded.
2. If the installed checkout is already migration-capable, re-run its installer
   with the newer revision pinned explicitly. To cross the installed
   `bcd721586f24fe9f22be8e67da26ceb253fc8679` boundary, extract the bootstrap
   from the target Git object and run it against the installed checkout (the
   complete command sequence is below). Do not invoke the old installer: its
   code was loaded before checkout advance and cannot recognize the moved PAT
   links.
3. Read the receipt: `ok` must be `true`, `pinnedRevision` must equal the
   intended SHA, `conflicts` must be empty, `mutation` tells whether the
   checkout moved and `rollback` tells whether a failed run restored it.
   On failure, `junctionRollback` and `junctionRollbackReadback.restored` must
   also report `restored` whenever a destination mutation began.
   `retargetedManagedLinks` lists same-repo junctions moved to this
   checkout. A changed input invalidates the receipt: re-run the
   readback (`verify-install.ps1`) after any relevant change.

Order inside one install run: preflight (foreign entries fail before
the checkout moves; proven same-repo links pass through), advance (the
checkout moves to the pinned SHA), install (each same-repo link is
re-checked and then swapped to this checkout; anything else foreign
fails), readback (HEAD, cleanliness, and every junction verified).

Consequences: the swap only deletes the junction reparse point, never
target contents. Before each mutation, the installer records whether the
destination was absent or the exact junction target. A failed run validates
its owned post-state, restores every changed junction in reverse order, reads
that inventory back, and only then restores the recorded HEAD/branch. A link
whose target changed between preflight and swap refuses with
`migration-unsafe` / `target-changed` instead of swapping. Nothing here touches
the network and no live rollout happens: install output is a local receipt only.

### Bootstrap from the installed bcd721 revision

After fetching the desired commit into the installed checkout, use its exact
SHA as `$revision`. The bootstrap and installer perform no fetch:

```powershell
$target = @((Get-Item "$HOME\.agents\skills\release" -Force).Target)[0]
$checkout = Split-Path (Split-Path $target -Parent) -Parent
$revision = '<pinned-target-sha>'
$bootstrap = Join-Path $env:TEMP "task-runner-bootstrap-$revision.ps1"
$lines = @(& git -C $checkout show "${revision}:skills/release/scripts/bootstrap.ps1")
if ($LASTEXITCODE -ne 0) { throw 'Target bootstrap is not available locally.' }
[IO.File]::WriteAllText($bootstrap, ($lines -join "`n") + "`n", [Text.UTF8Encoding]::new($false))
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File $bootstrap -CheckoutPath $checkout -Revision $revision `
  -SkillsDirectory "$HOME\.agents\skills" -AllowOldLinkMigration `
  -ReceiptPath (Join-Path $env:TEMP "task-runner-rollout-$revision.json")
if ($LASTEXITCODE -ne 0) { throw 'Rollout failed; inspect the retained receipt.' }
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File (Join-Path $checkout 'skills\release\scripts\verify-install.ps1') `
  -Revision $revision -SkillsDirectory "$HOME\.agents\skills"
Remove-Item -LiteralPath $bootstrap -Force
```

The bootstrap extracts `install.ps1`, `migration.ps1`, and the junction
transaction helper from the pinned commit into a disposable directory. This
ensures the target implementation performs preflight, migration, rollback and
readback even though the installed checkout starts at the old revision.

## From another repo (find this checkout via the installed junction)

Do not guess the path. The `release` junction points at this checkout,
so resolve it first and only then fetch and pin. If it is the old installed
revision, use the extracted-bootstrap procedure above. If it is already
migration-capable, run the in-checkout installer and verify:

```powershell
$target = @((Get-Item "$HOME\.agents\skills\release" -Force).Target)[0]
$checkout = Split-Path (Split-Path $target -Parent) -Parent
Set-Location $checkout
git fetch origin
git rev-parse origin/main
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File skills\release\scripts\install.ps1 `
  -Revision <pinned-sha> `
  -SkillsDirectory "$HOME\.agents\skills" -AllowOldLinkMigration
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File skills\release\scripts\verify-install.ps1 `
  -Revision <same-sha> `
  -SkillsDirectory "$HOME\.agents\skills"
```

Only the explicit `git fetch` touches the network; both scripts resolve
locally and refuse on unknown revisions. PAT-owned links are never replaced
without `-AllowOldLinkMigration` and every recorded proof; otherwise they stay
unchanged for the PAT owner.

## Verify only (changes nothing)

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File skills\release\scripts\verify-install.ps1 `
  -Revision <pinned-sha> `
  -SkillsDirectory "$HOME\.agents\skills"
```

Exit `0` means the checkout HEAD matches the pinned revision, the tree is
clean, and every discovered skill resolves to its junction. Anything else
prints the mismatches and exits `1`.

## Why cross-repo discovery works

Link names are derived from `git ls-tree -r <pinned> skills/` inside THIS
checkout, not from the worktree files and not from any other repository.
Junction targets point into this checkout at absolute paths, so other
checkouts (including personal-agent-toolkit or its `lenovo-release`
pointer) are never scanned, never modified, and never shadowed: a
same-named foreign entry becomes a reported conflict, not an overwrite.
