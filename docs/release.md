# Release via Lenovo

Thin Dell → Lenovo release over the existing signed queue. Use this
scenario — and only this scenario — when the release must run on Lenovo
and Dell only builds the job, submits it, and reads back the verified
output. This document is the single canonical entry for the release
flow.

Two branches, one builder:

- preview: no `--version`, no `--confirm`; call bare `release-tag` and
  show the output, then stop with the version still open;
- versioned: `--version <chosen>` **plus** `--confirm <exact-user-text>`;
  a version without confirm refuses (it would hang on the interactive
  prompt). A tag is created only here, only with the chosen version,
  and only after the user's explicit instruction.

`docs/release-tag-preview.md` stays in the tree untouched but is legacy:
its `<example-version> --dry-run` shape is not used by this driver and
must not be followed as a second instruction.

## Required inputs (owner-provided, never invented)

Queue dir + 32-byte key file (raw bytes, or base64 text with
`--key-format base64`; both outside every repo, the queue, and the LAN
share), Lenovo checkout path, Lenovo Node interpreter path, and the
owner-provided `release-tag` entrypoint path on Lenovo. The worker on
Lenovo is already running. The version and confirmation text come
only from the user after the preview; they are never example values.

All interpolated values (paths, version, confirm) must not contain `"`,
`$`, backtick, or `%` (bridge path rule, `docs/lan-bridge.md`); the
driver refuses them before writing anything. Spaces are fine (paths are
double-quoted for `cmd`).

## Evidence actually checked (not assumed)

- Bare `release-tag` is the preview: `cli.js` source (via
  `lenovo-cli.result.json`, id `28bf2a946ab9a658189cd8564173962c`)
  routes no-arg runs to `runChangesCommand`, which logs
  `No tag will be created in this mode.` Live proof:
  `cmd /c "set NODE_OPTIONS=--use-system-ca && release-tag"` →
  `lenovo-preview-sysca.result.json` (id
  `9170bbc499e7e666585c6dc3144ead4b`, `exit_code` 0) prints
  `Starting changes preview` … `No tag will be created in this mode.`
- `release-tag <version> --dry-run` preview creates no tag either:
  `cmd /c "set NODE_OPTIONS=--use-system-ca && release-tag 1.38.0 --dry-run"`
  → `lenovo-dryrun.result.json` (id
  `e7564d3fbc5f0905b3fd12035a5e6961`, `exit_code` 0) ends with
  `Dry-run finished. No tag was created.`
- Interactive gate: `confirmation.js` (via
  `lenovo-confirmation.result.json`) prompts
  `Continue and create tag? [yes/no]:` and accepts only
  `yes/y/да` (negative: `exit/n/no/нет`), looping otherwise — hence the
  piped confirmation below.
- Owner-reported (NOT verified here): `release-tag 1.38.0` succeeded via
  `cmd /c "set NODE_OPTIONS=--use-system-ca && echo yes | release-tag 1.38.0"`
  (job `dd8bf3d81e9f6b1d4f5616e84b10b77f`). No receipt for that job id
  was found in this checkout, so it is not cited as evidence; the driver
  below only reuses its `echo <text> | <tool> <version>` cmd pipe shape.

## Exact commands

1. Build the request and the one-line command file (Dell, files only;
   no network, no key):

```text
python scripts/release/request.py \
  --node <abs-node-on-lenovo> \
  --release-tag <abs-entrypoint-on-lenovo> \
  --lenovo-checkout <abs-lenovo-checkout> \
  [--version <chosen-version> --confirm <exact-user-text>] \
  --request-file <request.json> --command-file <command.txt>
```

Omit both `--version` and `--confirm` for preview. A versioned run
requires BOTH, with `--confirm` set to `yes` or `y` after user approval
after seeing the preview. Completion: `request_sha256=…` printed;
command file holds exactly one line starting with `cmd /c "set
NODE_OPTIONS=--use-system-ca && ` (preview: bare tool call; versioned:
`echo <confirm> | "<node>" "<entrypoint>" <version>`).

2. Submit with the standard client (`docs/lan-bridge.md`); nothing here
   reimplements the envelope, MAC, or queue:

```text
python scripts/lan_bridge/lan_bridge.py submit \
  --queue <queue-dir> --key-file <key-path> \
  --command-file <command.txt> --cwd <abs-lenovo-checkout> \
  --timeout 300 --max-output 65536
```

Completion: submit printed one `JOBID=<32hex>`.

3. Observe the SAME id until a verified result or a concrete blocker
   (`docs/lan-bridge.md` §3–§4: `fetch` or `scripts/lan_bridge/get.py`,
   same key, same queue, same id; a new id is never a retry of an
   unknown outcome):

```text
python scripts/lan_bridge/get.py \
  --queue <queue-dir> --key-file <key-path> \
  --id <job-id> [--window <seconds>] [--poll <seconds>]
```

Completion: a MAC-verified envelope bound to this id. Outcome reading
follows `docs/lan-bridge.md` (Confirmed states vs unknown) and is never
invented: `exit_code` 0 without `timed_out`/`output_truncated` is
success; non-zero exit is worker failure; `timed_out` is worker timeout
and `output_truncated` is incomplete. A preview may be retried with a new
id (and a larger `--max-output` if needed). For a versioned run, do not
resubmit: the tag may already exist. Read back the tag and deployment state
before deciding what remains. Any other status is confirmed-yet-unknown
(never success); bounded-wait expiry with no envelope is unknown —
re-read the SAME id.

4. Show the user the verified stdout, then stop on preview or report
   the result on a versioned run. `release-tag` owns the preview text,
   the confirmation prompt, version handling, existing tags, pipeline,
   and deploy; this scenario adds no check of its own.

Completion: the user saw the verified output; on preview the version
decision is still open, on a versioned run the carried result is
reported as-is.

## Boundaries

- No version, no tag: preview creates nothing. A versioned run without
  BOTH the user's explicit version choice and explicit confirmation
  text never runs; `--confirm` without `--version`, and `--version`
  without `--confirm`, both refuse.
- Confirmation is a normalized `yes` or `y` token piped by `cmd`
  (`echo <text> | "<node>" "<entrypoint>" <version>`); no `--yes`/`--force`
  flag is invented. If `release-tag` prompts for anything else, it fails
  closed instead of matching.
- `NODE_OPTIONS=--use-system-ca` lives only in this Lenovo process
  environment, never as a machine-wide setting.
- The key travels through `--key-file` only; it never lands in a
  request, command file, or receipt. Unknown outcomes keep the SAME id.
- Fixture runs are local only and never live evidence; a live run is a
  separately authorized step. The legacy example-version `--dry-run`
  shape lives only in `docs/release-tag-preview.md` and is not redefined
  or used here.

## Local verification

```text
<python> -m unittest discover -s tests/release -v
node scripts/check-file-tokens.mjs --all
```

Covers: preview vs versioned `cmd /c` command shape, version opacity (no
semver check in the driver), version-requires-confirm (no hanging call),
exact-text piping in `cmd` form, bridge-char (`"`, `$`, backtick, `%`)
and line-break refusal, relative-path refusal, single-line no-BOM
command file, and no key material in the request.
