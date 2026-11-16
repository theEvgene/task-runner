---
name: release
description: Route Lenovo release requests («раскатай релиз», «сделай релиз через Lenovo») through the task-runner release driver. Use when the release must run on Lenovo via the signed LAN queue, to preview changes before versioning, or to resume observing a release JOBID.
---

# Release

Thin pointer. The task-runner checkout owns the flow (`docs/release.md`)
and the request builder (`scripts/release/request.py`); this skill adds no
release logic. `release-tag` on Lenovo owns preview text, versions, tags,
pipeline, and deploy.

Ported intent of the `lenovo-release` pointer skill (personal-agent-toolkit,
`skills/t-bank/lenovo-release/SKILL.md`) under the `release` name. A
junction that points into another checkout of THIS repo (origin remote
proof plus exact `skills/<name>` shape with `SKILL.md`) is a managed
link: the installer retargets it to this checkout after re-checking it
just before the swap, and reports it in `retargetedManagedLinks`. That
old name and the same-named PAT links for `dell-lan-local-dev` and
`lan-task-bridge` are migrated only with `-AllowOldLinkMigration`. Each
target must match its recorded PAT origin, exact repository-relative path,
frontmatter, and body marker (see `migrations.json`). The guarded swap removes
only the junction and retains the PAT source checkout. Every changed junction
is inventoried and restored with readback if any later stage fails. Anything
unproven stays unchanged. `UPDATE.md` documents the pinned bootstrap required
when the installed checkout still runs the pre-migration installer.

## Route: preview first, version only after the user chooses

All paths below are placeholders (owner-provided, never invented hostnames).
The three Lenovo paths (`--node`, `--release-tag`, `--lenovo-checkout`)
are passed per run through the existing `scripts/release/request.py` flags;
no settings file in this repo provides them. The verified Lenovo checkout
is `Documents\repositories\task-runner` under the Lenovo user profile:
resolve it on Lenovo with
`Join-Path ([Environment]::GetFolderPath('UserProfile')) 'Documents\repositories\task-runner'`
and pass the expanded absolute result (never a username, `%...%`, or
`$env:...` placeholder).

1. **Pin the task-runner copy.** Confirm the checkout in use holds
   `docs/release.md` and `scripts/release/request.py`; record its
   `git rev-parse HEAD`. A missing file or a stale copy refuses before
   anything is built.
2. **Preview.** Build the request with `scripts/release/request.py`
   without `--version`/`--confirm`, submit it with the standard bridge
   client, observe the SAME id to a verified result, then show the user
   the verified stdout and stop with the version still open.
3. **Version only after choice.** Run versioned only with the user's
   explicit version **and** exact confirmation text after the preview;
   a version without confirm refuses. Report the carried result as-is.

## Fail-closed rules

- Observe the SAME JOBID until a verified result or a concrete blocker;
  an unknown outcome never gets a resubmission under a new id. A preview
  may be retried with a new id; a versioned run is never resubmitted
  (the tag may already exist).
- Preview creates nothing: no version and no tag without the user's
  explicit version choice and confirmation text.
- No live Lenovo proof is claimed by shipping this skill; fixture runs
  are local only.

## Install and update

This skill is standalone-installable on Windows. `scripts/install.ps1`
links every `skills/*/SKILL.md` of the pinned revision as a managed
junction under `~/.agents/skills` (never overwriting foreign entries),
checks the checkout is clean, pins the revision, and prints a JSON
receipt after readback. `scripts/verify-install.ps1` re-checks without
changing anything. Future-agent instructions live in `UPDATE.md`.
