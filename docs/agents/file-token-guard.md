# File token guard

`scripts/check-file-tokens.mjs` rejects any single staged file whose estimated size exceeds 5,500 tokens (`floor(bytes / 4)`; no tokenizer, model, network call, or extra dependency — only Node.js and Git).

## Run it

```powershell
node scripts/check-file-tokens.mjs                # staged files (pre-commit)
node scripts/check-file-tokens.mjs --all          # every tracked file (audit)
```

A clean tree prints nothing and exits `0`. Violations list every offending path and exit `1`. The limit is fixed at 5,500 tokens; there is no override flag.

## Integration in this repo

The pre-commit hook calls the checker on staged files and propagates its exit code. Never wrap it in `|| true`. Exclusions pass at the call site, not inside the script:

```text
node scripts/check-file-tokens.mjs --exclude 'package-lock.json'
```

`package-lock.json` is machine-owned and excluded. Keep any further exclusion list short, justified, and shrinking; exemptions are only for generated, vendored, append-only, or single-module data files.

## Diff scope (before review/publication)

Staged mode stays the strict per-commit gate: it rejects any staged file over the limit. Before review or publication, check the final contents of the whole range in one call with explicit SHAs:

```powershell
$base = git rev-parse origin/main
$head = git rev-parse HEAD
node scripts/check-file-tokens.mjs --base $base --head $head
```

Scope and report boundary:

- The range is the merge base of `<base>`/`<head>` through `<head>`; every added or changed path is sized at `<head>`, so a file that exceeds the limit in any commit of the range fails only while its final content still exceeds it, and a file fixed before `<head>` passes.
- New or grown (versus the base) over-limit files fail with exit `1`.
- Files already over the limit at the base that did not grow are listed separately (`NOTE`) and do not fail; fix them outside this change, not as part of it.
- Deleted files never fail; renames are checked at the new path against the old path's base size; paths with spaces work; binaries are skipped exactly like in staged mode; an unknown revision or any Git failure fails closed (exit `1`, never success).
- Only files in the range are listed — base-only exceedances outside the range are not reported here (use `--all` for the full audit). Call-site `--exclude` globs apply as usual; no default exclusions were added for new violations.

Covered by `tests/file_token_guard/check-file-tokens-diff.test.mjs` (temporary Git fixtures: new/grown/fixed violations, baseline distinction, deletion, rename with and without growth, spaced paths, binaries, unknown revision, exclusions, staged-mode contract).

## Attribution

Verbatim copy of `scripts/check-file-tokens.mjs` from `personal-agent-toolkit-installed` (SHA256 `1FCB0E698585F11F11B543244EFE1F20E1759A83F91CF2456FAB4FBF037D0481`), itself a behavior-preserving port of the checker pinned at `mattpocock/course-video-manager@d329576deeda2878b2902a2d3d09749bbd1af693/scripts/check-file-tokens.sh`. Only the binary-detection substitution differs from the original (NUL-byte or non-UTF-8 sample instead of `file --mime-encoding`); threshold, selection, report bytes, and exit codes are preserved. Local consumer doc: `docs/agents/test-design.md` explains why no `typecheck`/`test` hook lines exist yet. Local extension for issue #136: `--base/--head` diff-scope mode (final `<head>` contents, baseline `NOTE` section, rename-aware base sizes, binary/space/deletion handling); staged, `--all`, limit, and exclusion contracts are unchanged.
