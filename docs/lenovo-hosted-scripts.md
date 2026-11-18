# Lenovo-hosted scripts over the signed LAN bridge

Dell-started agents run the existing Lenovo-hosted CLIs through the generic
signed queue. No Jira- or DCO-specific wrapper exists; every run below uses
`scripts/lan_bridge/lan_bridge.py` (`submit`/`fetch`) and `scripts/lan_bridge/get.py`
exactly as `docs/lan-bridge.md` states. Local execution stays intact: use a
skill's local command when its credentials already live on the calling host.

Scope: the five `cli-` Jira operations (`extract`, `transitions`,
`transition`, `assign`, `flag`, `create-epic` in
`scripts/jira-protocol.mjs`) and the DCO protocol CLI
(`scripts/dco-feature-flags.mjs` `plan`/`apply`). The managed Nessy
`dp jira` read keeps its own route in `docs/lan-bridge-config.md` and is not
this route.

## Lenovo-side identities (owner-provided, never invented)

- Lenovo task-runner checkout: `Documents\repositories\task-runner` under the
  Lenovo user profile. Resolve on Lenovo only:
  `Join-Path ([Environment]::GetFolderPath('UserProfile')) 'Documents\repositories\task-runner'`.
  Never send `%USERPROFILE%`, `$env:...`, or a username; pass the expanded
  absolute path per run. The Jira CLI is
  `<checkout>\scripts\jira-protocol.mjs`; the DCO CLI is
  `<checkout>\scripts\dco-feature-flags.mjs` (thin entry to the installed
  skill runtime). Verify both files exist on Lenovo before submitting.
- Lenovo `node` executable: absolute path on Lenovo, discovered on Lenovo.
  The bridge `submit --cwd` for every run in this doc is the Lenovo checkout
  (or the installed skill directory that holds the same runtime); DCO
  `--plan`/`--state` paths are separate Lenovo-local file arguments, not the
  cwd.
- Credentials live only in the Lenovo host environment: `JIRA_SESSION_COOKIE`
  (plus same-session `JIRA_ATL_TOKEN` for `assign`) and `DCO_TEST_ACCESS_TOKEN` /
  `DCO_PRODUCTION_ACCESS_TOKEN` (or shared `DCO_ACCESS_TOKEN` fallback). The
  CLIs accept no credential flags and write no credential files. Never place a
  token in argv, a command file, a plan, a receipt, or the queue.
- Every Jira CLI invocation requires `NODE_USE_SYSTEM_CA=1` in the Lenovo
  worker process environment inherited by Node. Verify it is present there
  before submitting; setting it only in the Dell process does not propagate
  it through the queue. Keep TLS certificate verification enabled.

## Dell-side command construction (generic transport only)

Build one Lenovo argv, render it once for the `powershell` dialect, write the
UTF-8 command file locally, and submit that file. The quoting follows the
checked-in generic renderer `scripts/dell_lan/shell_command.py`
(`build_for_transport`): each part single-quoted (`'` doubled), joined as
`& <parts>`, wrapped once as
`powershell.exe -NoProfile -NonInteractive -Command "<program>"`.

- Safe inline argv: issue keys (`LEL-N`), `add`/`remove`, numeric ids,
  64-hex digests, `l-loans`/`finance-front`, and file paths without `"`, `$`,
  backtick, or `%` (the `docs/lan-bridge.md` interpolation rule). Unicode
  without `"`, control characters, or newlines is safe inside single quotes.
- Refusals (fail closed before submit): NUL/CR/LF in any part, `"` in any
  part under the `powershell` transport, `%`/`!`/`"` under `cmd`. The
  `powershell` dialect is mandatory here; there is no `cmd` form in this doc.
- Free text (`flag --comment`, `create-epic --summary/--description`,
  `--field` values, DCO `--description`) with `"`, newlines, or other
  unsafe shapes never travels inline. Write the exact UTF-8 bytes to a file
  on the existing owner-provided LAN share (the same share that already hosts
  the queue, readable and writable from both hosts; not a new mount) and pass
  the Lenovo-side absolute share path through the new
  file-backed options: `flag --comment-file`, `create-epic --summary-file` /
  `--description-file`. Staging uses only that share: write the UTF-8 file
  locally, copy it to the share over the existing SMB mount (Explorer or copy),
  and pass the Lenovo-side absolute path of that same file; the queue carries
  only the path and digests, never the text. Inline and file forms of one field
  are mutually exclusive; the CLI refuses both together and reads the file as
  UTF-8.
  `--field` has no file form: an unsafe custom-field value stays local.

Jira examples (Lenovo argv before transport rendering; read plus file-backed writes):

```text
<node-on-lenovo> <checkout>\scripts\jira-protocol.mjs extract --issue LEL-1234
<node-on-lenovo> <checkout>\scripts\jira-protocol.mjs flag --issue LEL-1234 --action add --comment-file <lenovo-share-path>\comment.md
<node-on-lenovo> <checkout>\scripts\jira-protocol.mjs create-epic --summary-file <lenovo-share-path>\summary.txt --description-file <lenovo-share-path>\description.md
```

DCO example (plan bytes staged to the share first, then referenced on Lenovo;
state stays Lenovo-local, see below):

```text
<node-on-lenovo> <checkout>\scripts\dco-feature-flags.mjs plan --application l-loans --key <key> --description-file <lenovo-share-path>\dco-desc.txt --out <lenovo-plan-path>
<node-on-lenovo> <checkout>\scripts\dco-feature-flags.mjs apply --plan <lenovo-plan-path> --state <lenovo-state-path>
```

Submit and observe with the standard client (`<queue>`, `<key-file>`,
and `--dialect powershell` placeholders):

```text
python scripts/lan_bridge/lan_bridge.py submit --queue <queue-dir> --key-file <key-path> \
  --command-file <command-file> --cwd <lenovo-checkout> --timeout <seconds> --max-output <bytes> \
  --dialect powershell
python scripts/lan_bridge/lan_bridge.py fetch --queue <queue-dir> --key-file <key-path> --id <JOBID>
python scripts/lan_bridge/get.py --queue <queue-dir> --key-file <key-path> --id <JOBID>
```

Recovery is same-id only (`fetch`/`get.py` with the same JOBID); a second
submit while the outcome is unknown is a new id, never a retry.

## Acceptance (transport first, receipt authoritative)

1. Transport: require a MAC-verified result bound to the JOBID with no
   `timed_out`, no `output_truncated`, and a complete authenticated
   stdout receipt available (`classify_result` in `docs/lan-bridge.md`).
   Both `completed`/`ok` and `error` statuses proceed to step-2 receipt
   parsing when stdout is complete: the worker seals `status: error`
   for any nonzero process exit, so CLI plan exits such as `3` arrive
   as `error` envelopes. An `error` status is never success by itself;
   only a `Completed` receipt (step 2) is success. `timed_out` is
   terminal worker timeout (NEW id); `output_truncated` is terminal
   incomplete (NEW id with larger `--max-output`). An unrecognized
   `status`, missing stdout, or truncated output is unknown, never
   success. The envelope `exit_code` is a transport outcome, not the
   CLI verdict: never re-interpret a nonzero envelope code as a
   different CLI state and never infer the exact CLI code survived —
   the exact CLI state always comes from the stdout receipt JSON,
   never from the envelope code alone.
2. CLI: the stdout receipt `state` is authoritative for Jira/DCO runs.
   Parse the receipt JSON first. `Completed` (CLI exit `0`) is the only
   success; `AwaitingConfirmation` (CLI exit `3`: Jira write plans and
   DCO `apply` preflight, including `ReadyToCreate`/`TestVerified`
   preflight states) is an expected non-success plan state, not a
   transport failure — continue the two-phase flow with `--confirm`
   instead of treating the envelope as failed. Split exit `4` by CLI:
   Jira exit `4` is `MissingRequiredFields` only (`create-epic`; Jira
   has no `Duplicate` state) while DCO exit `4` is `Duplicate`. Jira
   incomplete extraction is exit `6`; DCO `FailedTest` is exit `6`,
   `Partial` is exit `7`. Exit-`2` receipts (`Failed` for Jira, `Error`
   for DCO) are terminal for the attempted inputs: fix inputs, then a
   NEW JOBID, never a mutation retry. Any unlisted or unparsable
   receipt `state` is unknown, never success. Exit tables live in
   `docs/jira-protocol.md` (Exit states) and `docs/dco-feature-flags.md`.
   Jira writes are two-phase: a read-only plan returning
   `AwaitingConfirmation` (or `MissingRequiredFields`), then a later
   invocation repeating identical inputs with `--confirm <confirmation>`.
   Report `Completed` only with the documented readback (returned key plus
   field-verified LEL Epic on rapid view `298290` for create; exact assignee
   login for assign; planned target status id for transition; flag state plus
   new comment id for flag). DCO `apply` without confirmation performs
   preflight only; a later invocation may carry `--confirm <planDigest>`,
   and `--renew-confirm` only after proven absence. Reuse the same plan and
   state paths for recovery.
3. Unknown writes: receipt `state` `UnknownOutcome` /
   `AwaitingRenewedConfirmation` (CLI exit `5`; Jira `UnknownOutcome`) is
   never permission to retry a mutation, regardless of the envelope
   `exit_code`. DCO `Error` (CLI exit `2`) is a catch-all that can hide
   an unknown mutation outcome: an escaping `AmbiguousOutcome` or a
   `Completed` re-verification readback failure surfaces as `Error`/`2`,
   not `5`. Treat any DCO `Error`/`2` after a mutation-capable `apply`
   as possibly-unknown and fail closed with the same handling as exit
   `5`. Re-read the same JOBID first; then re-run the read-only step
   on Lenovo (Jira plan
   without `--confirm`; DCO `apply` without confirmation for readback-only
   recovery) and verify flag state, comment presence, or exact-path toggle
   readback before deciding. HTTP 401/403 (Jira) is terminal for that route.
   Envelope caution (single unverified observation, no JOBID, receipt,
   log, or stdout hash attached): one Lenovo smoke run reportedly
   surfaced a CLI exit `2` as envelope `1` with byte-identical stdout,
   so a nonzero envelope code must never be re-interpreted as a
   different CLI state and never authorizes a blind replay — always
   decide from the receipt.

## DCO same-host files

`--plan` and `--state` are Lenovo-local absolute paths on the same Lenovo
host: the state lock is host-local and the plan digest binds the run. Dell
never passes Dell-local paths. Stage plan bytes via the LAN share (or
regenerate with `plan` on Lenovo), keep plan and state in one Lenovo
directory outside any checkout, and pass the already approved digest with
`--confirm` only in the invocation that may create. The state file is
operational history, not authorization; a foreign-host or malformed lock
fails closed and is never deleted manually.

## Boundaries

- One command per job, one cwd, bounded output; small return files only via
  `--return-file`. The queue carries commands and digests, never tokens,
  cookies, captures, or trust material.
- No live Jira or DCO mutation is performed by the fixture suite. Live runs
  need explicit per-action authorization on top of the bridge's queue/key
  ownership.
- Username-free: public files carry no Lenovo username, only the
  `Documents\repositories\task-runner` suffix resolved on Lenovo.
