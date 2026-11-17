# Shared LAN bridge settings

Both LAN skills read `%USERPROFILE%\.config\task-runner\lan-bridge.json` before a live operation. Its versioned template is `config/lan-bridge.example.json` in the task-runner checkout. This local file is operator configuration, not a secret store; running a skill does not create or change it.

`dell_python` is the absolute interpreter path on Dell for local client and
driver commands. `lenovo_python` is a separate absolute interpreter path on
Lenovo for commands executed there. Discover each on its own host; do not use
one as a substitute for the other. Require the relevant path when the selected
route needs it, and report an unknown or invalid path as a blocker.

## Discover existing settings

Read only the named fields; do not enumerate secret files or print entire
configurations. Start with the shared settings file above. If it or a field is
missing, use these sources as evidence, not fixture defaults:

- Resolve `%USERPROFILE%\.agents\skills\lan-task-bridge` (or
  `dell-lan-local-dev`) to its installed target. Its containing checkout
  identifies the installed checkout; a checkout revision is informational only
  and never a LAN operation gate.
- An owner-provided `lan-readiness-config/1` file has no fixed path. Locate it
  from a recorded invocation or ask for its path. Its `queue`, `keyFile`,
  `keyFormat`, and `lenovo.python` correspond to `queue_dir`, `key_file`,
  `key_format`, and `lenovo_python`. Its `lenovo.cwd` is the selected worker
  cwd; use it as `lenovo_jira_working_dir` only if verified as the Jira skill
  directory. `local.components`, `local.tools`, `lenovo.components`, and
  `lenovo.tools` are scenario-specific
  readiness checks, not shared settings. See
  `docs/readiness.md`.
- On Lenovo, the documented launcher config is
  `%LOCALAPPDATA%\task-runner\lenovo-worker.json`. Its `queueDir` and
  `keyFile` are Lenovo-side paths, not necessarily Dell paths; `keyFormat` is
  the worker format. Its `python` is a candidate Lenovo interpreter when
  present; `workerCommand` can override the default invocation. Inspect only
  relevant fields, never `smb` or password/key contents. Verify the candidate
  on Lenovo before use. Missing `python` leaves it unverified: the launcher
  falls back to its own runtime, which this config does not identify. Do not
  copy the example's placeholder.
- Discover `dell_python` on Dell and `lenovo_python` on Lenovo from an
  existing verified interpreter or owner-provided configuration. Discover the
  Jira working directory and explicit instance from the installed workflow or
  owner-provided settings; examples do not establish the active instance. If
  a needed source or value cannot be verified, report it as unverified and
  stop. Do not modify these sources during discovery.

1. Resolve the installed skill junction under `%USERPROFILE%\.agents\skills` to its actual target. The release installer links skill directories into a task-runner checkout. Locate this instruction and template from that checkout. Read the settings file and discover existing values before requesting missing information. Do not invent paths or Jira instances.
2. Require schema `task-runner/lan-bridge-config/1` and nonempty absolute paths for the queue and key file, with key format `raw` or `base64`. For managed Jira reads additionally require nonempty absolute paths for `lenovo_python` and `lenovo_jira_working_dir`, plus an explicit `jira_instance`; do not require these Jira-only fields for an independent GitLab MR scenario. Name each absent or invalid component in the blocker. The key file is a path only: never read or print its contents during discovery, put key bytes in JSON, or place it in a repository, queue, or shared LAN folder.
3. Verify the selected route's required files exist before use. Name each missing file; stop rather than calling an incomplete installation current. A checkout revision is never a gate: do not compare Git HEAD against configured values and do not refuse a dirty tree. Do not fetch or update another checkout implicitly.
4. Check queue availability and key-file existence without displaying key material. Follow `docs/lan-bridge.md` and the selected scenario for key validation, signing, authorization, and result verification. Pass configured paths and format as existing CLI arguments; configuration does not alter the protocol. Observe a submitted JOBID every 30 s for 10 minutes per `docs/lan-bridge.md` ("Observation rule"): a short-timeout expiry is unknown, never completion. If a submitted result is unknown or observation is interrupted, recover the same JOBID; never submit a replacement while its outcome is unknown.
5. For managed Jira reads, use `lenovo_jira_working_dir` as the worker cwd and `lenovo_python` as the executable. First verify the installed workflow on Lenovo: write the following single line to a local UTF-8 command file (no secrets; substitute the configured Python path):

```text
"<lenovo_python>" "..\nessy-jira-workflow\scripts\jira_task.py" read --help
```

Submit and fetch it using `docs/lan-bridge.md`, the configured queue/key/format, that command file and `--cwd <lenovo_jira_working_dir>`. Inspect the verified, complete successful result's help output for `--jira-instance`. An explicit help output without the option establishes unsupported; an unavailable workflow, failed or incomplete probe, or ambiguous output leaves support unverified. Stop in either case; do not infer support from the Dell checkout or label an older workflow current. Apply the path interpolation restrictions in `docs/lan-bridge.md` to every substituted value.

Before constructing the read command, validate the entire raw values with
ASCII full matches: `jira_instance` must match `[A-Za-z][A-Za-z0-9_-]*`
and `KEY` must match `[A-Z][A-Z0-9]*-[0-9]+`. Reject empty values,
whitespace, quotes, control characters, shell metacharacters, and all other
nonmatching input; do not trim, escape, repair, or substitute it. Stop before
writing or submitting the read command on a mismatch. These are deliberately
narrow bridge restrictions, not a claim about all accepted Jira identifiers:
the inspected workflow parser accepts strings and does not enforce these
patterns. Confirm the instance and key from authoritative task context; a
matching string alone does not establish either is correct. The syntax gate
is not a sanitizer.

Once support is established, write this single line to another local UTF-8 command file, substituting the configured values and issue key:

```text
"<lenovo_python>" "..\nessy-jira-workflow\scripts\jira_task.py" read --jira-instance "<jira_instance>" --issue "<KEY>" --full
```

Submit it with the same configured cwd and bridge inputs, then observe its JOBID every 30 s for 10 minutes per `docs/lan-bridge.md` ("Observation rule"). For both probe and read, accept success only for a MAC-verified result bound to that JOBID with status `completed` or `ok`, integer `exit_code == 0`, no `timed_out`, and no `output_truncated`, as classified in `docs/lan-bridge.md`. An unrecognized status is unknown; a verified timeout, failure, or truncated output is not success. Recover an unknown or interrupted observation using the same JOBID. Preserve `parent.presence` and `gateway_unavailable` exactly. If queue, key, workflow, or result is unavailable, report the affected Jira categories as unverified without switching credential routes.

## Lenovo task-runner checkout (verified, username-free)

The verified Lenovo clone lives at `Documents\repositories\task-runner`
under the Lenovo user profile. Public files never embed the Lenovo
username. Resolve it on Lenovo only, for example with PowerShell:

```powershell
Join-Path ([Environment]::GetFolderPath('UserProfile')) 'Documents\repositories\task-runner'
```

Run that on Lenovo and use the expanded absolute result. Do not send
`%USERPROFILE%...`, `%USERPROFILE%` alone, or `$env:...` as a path:
single-quoted transport keeps them literal, so they would not expand
on the worker (see `docs/lan-bridge.md` for the interpolation rule).
Keep the expanded absolute path machine-local (operator notes or
command history) and pass it per run through the existing flags below;
no new config field or flag is introduced here.

What each binding actually is (do not equate them):

- `--lenovo-checkout` is a Dell request-builder input (the Lenovo
  checkout that driver reads). It exists on `dell_request.py`
  `base`/`publish`/`transfer`/`publish-existing`, on
  `mr_ready/request.py`, on `pipeline_observer/request.py`, on
  `release/request.py` (`--lenovo-checkout`), and on
  `fresh_base/request-base.mjs` / `request-tip.mjs`. `first` has no
  `--lenovo-checkout`: the exporter observes the remote into an
  isolated temp bare repo and writes no checkout.
- `--lenovo-worktree` exists only where the Lenovo driver writes a
  task worktree: `dell_request.py` `publish`/`transfer`/
  `publish-existing`. `base` takes `--lenovo-checkout` but has no
  `--lenovo-worktree` (it exports a bundle from the checkout and writes
  no worktree); `first` has neither checkout nor worktree.
- Bridge `submit --cwd` is a separate argument: the worker working
  directory for that job. Read it from the selected route document for
  that run (for example `docs/mr-ready.md` uses the Lenovo checkout;
  other routes name their own working dir). `submit --expected-cwd` is the
  producer's signed declaration of that same directory, never a second
  source: `dell_request.py` emits it as `expectedCwd` (the declared
  `--lenovo-checkout`, or an explicit `--expected-cwd`), and the worker
  refuses a provable difference before spawning. Pass it with the identical
  path the route names for `--cwd`, and never a path the route does not
  name; a route that declares no expectation stays `unverified-target`, not
  verified.
- Readiness `lenovo.cwd` is only the probe's working directory, not a
  store for driver, flow, client, node, or entrypoint paths.

Per-route Lenovo flags (only what each driver accepts):

- `dell_request.py publish`: `--lenovo-skill-dir` (required),
  `--lenovo-driver-dir` (optional, default `<skill-dir>/scripts`),
  `--python`, `--lenovo-checkout`, `--lenovo-worktree`, plus
  `--lenovo-flow-dir` (or `DELL_LAN_FLOW_DIR`) for the external flow
  checkout.
- `dell_request.py transfer`: `--lenovo-skill-dir` (required),
  `--lenovo-driver-dir` (optional, default `<skill-dir>/scripts`),
  `--python`, `--lenovo-checkout`, `--lenovo-worktree`; no
  `--lenovo-flow-dir`.
- `dell_request.py base`: `--lenovo-skill-dir` (required),
  `--lenovo-driver-dir` (optional, default `<skill-dir>/scripts`),
  `--python`, `--lenovo-checkout` only; no `--lenovo-worktree`, no
  `--lenovo-flow-dir`.
- `dell_request.py first`: `--lenovo-skill-dir` (required),
  `--lenovo-driver-dir` (optional, default `<skill-dir>/scripts`),
  `--python` only; no `--lenovo-checkout`, no `--lenovo-worktree`,
  no `--lenovo-flow-dir`.
- `dell_request.py publish-existing`: `--lenovo-skill-dir` (required),
  `--lenovo-driver-dir` (optional, default `<skill-dir>/scripts`),
  `--python`, `--lenovo-checkout`, `--lenovo-worktree`, plus
  `--lenovo-flow-dir` (or `DELL_LAN_FLOW_DIR`).
- There is no `--lenovo-client-dir` on any `dell_request.py`
  subcommand.
- `fresh_base/request-base.mjs` / `request-tip.mjs`: `--lenovo-skill-dir`,
  `--node`, `--lenovo-checkout`; neither takes `--lenovo-worktree`,
  `--lenovo-flow-dir`, or `--lenovo-client-dir`.
- `mr_ready/request.py`: `--python`, `--lenovo-client-dir` (the
  existing `gitlab-mr-flow/scripts` dir), `--lenovo-checkout`.
- `pipeline_observer/request.py`: `--python`, `--lenovo-flow-dir`
  (the `pipeline_observer` dir holding `probe.py`),
  `--lenovo-checkout`, plus optional `--lenovo-client-dir` (forwarded
  as probe `--client-dir`).
- `release/request.py`: `--node`, `--release-tag`, `--lenovo-checkout`
  (submit `--cwd` is that same checkout per `docs/release.md`).

Per-route target expectation (`submit --expected-cwd`), one declaration per
writer: `dell_request.py` `base`, `publish`, `transfer`, and
`publish-existing` declare the declared `--lenovo-checkout` (or an explicit
`--expected-cwd`); `dell_request.py first` and the `fresh_base`, `mr_ready`,
`pipeline_observer`, and `release` writers declare none, so those jobs stay
`unverified-target`. `scripts/readiness/check.py` never seals one either.

Only the managed Jira read has a config fallback: `lenovo_python`,
`lenovo_jira_working_dir`, and `jira_instance` from this file's
`lan-bridge.json` (schema `task-runner/lan-bridge-config/1`). Every
other Lenovo task-runner, driver, flow, client, node, or entrypoint
path above is owner-provided per run through its existing flag; that
file has no field for them, and the readiness file holds only the
named checklist (`lenovo.python`/`cwd`/`components`/`tools`).

## Task-runner Jira CLI versus the managed Jira read (not the same route)

The managed Jira read in step 5 above is the installed external
`nessy-jira-workflow` workflow (`..\nessy-jira-workflow\scripts\jira_task.py
read ...`, driven by the existing `dp jira` Jira skills). It is not in
this repository and it is the only Jira path with a LAN-bridge route and
a config fallback here.

The task-runner Jira protocol CLI is a different transport at
`scripts/jira-protocol.mjs` inside the verified Lenovo clone
(`docs/jira-protocol.md`, agent packaging in
`docs/jira-agent-skills.md`). Agent skills that need to locate Lenovo
scripts resolve it on Lenovo from the expanded checkout above, for
example with PowerShell:

```powershell
Join-Path (Join-Path ([Environment]::GetFolderPath('UserProfile')) 'Documents\repositories\task-runner') 'scripts\jira-protocol.mjs'
```

Run it only where its session already exists, exactly as
`docs/jira-protocol.md` states: `node scripts/jira-protocol.mjs
<command> ...` with `JIRA_SESSION_COOKIE` (plus `JIRA_ATL_TOKEN` for
`assign`) already present in that host's environment. There is no LAN
request builder, bridge command shape, or settings-file field in this
repository that invokes `jira-protocol.mjs` on Lenovo, and running a LAN
skill neither creates nor provisions those credentials. Missing
integration is a blocker: do not claim automatic LAN invocation, do not
treat the `dp jira` workflow and `jira-protocol.mjs` as interchangeable,
and never route an existing `dp` workflow silently through the other
transport.

When the Lenovo clone advances, update the clone on Lenovo; the held
absolute path is reused unchanged unless the clone itself moved.
