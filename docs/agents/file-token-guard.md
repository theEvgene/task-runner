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

## Attribution

Verbatim copy of `scripts/check-file-tokens.mjs` from `personal-agent-toolkit-installed` (SHA256 `1FCB0E698585F11F11B543244EFE1F20E1759A83F91CF2456FAB4FBF037D0481`), itself a behavior-preserving port of the checker pinned at `mattpocock/course-video-manager@d329576deeda2878b2902a2d3d09749bbd1af693/scripts/check-file-tokens.sh`. Only the binary-detection substitution differs from the original (NUL-byte or non-UTF-8 sample instead of `file --mime-encoding`); threshold, selection, report bytes, and exit codes are preserved. Local consumer doc: `docs/agents/test-design.md` explains why no `typecheck`/`test` hook lines exist yet.
