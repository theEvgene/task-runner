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
  on the LAN share and pass the Lenovo-side share path through the new
  file-backed options: `flag --comment-file`, `create-epic --summary-file` /
  `--description-file`. Inline and file forms of one field are mutually
  exclusive; the CLI refuses both together and reads the file as UTF-8.
  `--field` has no file form: an unsafe custom-field value stays local.

Jira read example (Lenovo argv before transport rendering):

```text
<node-on-lenovo> <checkout>\scripts\jira-protocol.mjs extract --issue LEL-1234
<node-on-lenovo> <checkout>\scripts\jira-protocol.mjs flag --issue LEL-1234 --action add --comment-file <lenovo-share-path>\comment.md
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

## Acceptance (bridge first, CLI second)

1. Bridge: accept only a MAC-verified result bound to the JOBID with
   `completed`/`ok`, integer `exit_code == 0`, no `timed_out`, no
   `output_truncated` (`classify_result` in `docs/lan-bridge.md`). Anything
   else is worker failure, timeout, truncation, or unknown; an unrecognized
   `status` is unknown, never success.
2. CLI: apply the operation's own receipt on top of the verified envelope.
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
3. Unknown writes: exit `5` (`UnknownOutcome` / `AwaitingRenewedConfirmation`
   / Jira `UnknownOutcome`) is never permission to retry a mutation. Re-read
   the same JOBID first; then re-run the read-only step on Lenovo (Jira plan
   without `--confirm`; DCO `apply` without confirmation for readback-only
   recovery) and verify flag state, comment presence, or exact-path toggle
   readback before deciding. HTTP 401/403 (Jira) is terminal for that route.

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
