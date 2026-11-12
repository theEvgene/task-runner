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
- The old `lenovo-release` pointer link is migrated only with
  `-AllowOldLinkMigration` and only when its target provably holds the
  old pointer content (frontmatter `name: lenovo-release` plus a body
  reference to `scripts/release/request.py`, see `migrations.json`).
  Without the switch, or on any doubt, the old link stays untouched.
- No global guidance files are written. This repo assumes no global
  auto-discovery; global files may belong to another checkout.

## Update (move the installed links to a newer revision)

1. Inspect first: `git status --porcelain` must be empty and
   `git rev-parse HEAD` recorded.
2. Re-run the installer with the newer revision pinned explicitly, e.g.
   `-Revision origin/main` after a local `git fetch origin`, or a SHA.
3. Read the receipt: `ok` must be `true`, `pinnedRevision` must equal the
   intended SHA, `conflicts` must be empty, `mutation` tells whether the
   checkout moved and `rollback` tells whether a failed run restored it.
   `retargetedManagedLinks` lists same-repo junctions moved to this
   checkout. A changed input invalidates the receipt: re-run the
   readback (`verify-install.ps1`) after any relevant change.

Order inside one install run: preflight (foreign entries fail before
the checkout moves; proven same-repo links pass through), advance (the
checkout moves to the pinned SHA), install (each same-repo link is
re-checked and then swapped to this checkout; anything else foreign
fails), readback (HEAD, cleanliness, and every junction verified).

Consequences: the swap only deletes the junction reparse point, never
target contents. A failed run restores the recorded HEAD/branch, but
junctions already retargeted keep pointing at this checkout (rerunning
the installer converges: they are already correct). A link whose target
changed between preflight and swap refuses with `migration-unsafe` /
`target-changed` instead of swapping. Nothing here touches the network
and no live rollout happens: install output is a local receipt only.

## From another repo (find this checkout via the installed junction)

Do not guess the path. The `release` junction points at this checkout,
so resolve it first and only then fetch, pin, and verify:

```powershell
$target = @((Get-Item "$HOME\.agents\skills\release" -Force).Target)[0]
$checkout = Split-Path (Split-Path $target -Parent) -Parent
Set-Location $checkout
git fetch origin
git rev-parse origin/main
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File skills\release\scripts\install.ps1 `
  -Revision <pinned-sha> `
  -SkillsDirectory "$HOME\.agents\skills"
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File skills\release\scripts\verify-install.ps1 `
  -Revision <same-sha> `
  -SkillsDirectory "$HOME\.agents\skills"
```

Only the explicit `git fetch` touches the network; both scripts resolve
locally and refuse on unknown revisions. A PAT-owned `lenovo-release`
link is never removed here without `-AllowOldLinkMigration` and proven
PAT origin remote: otherwise it stays in `leftUnchanged` for the PAT
owner to delete manually.

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
