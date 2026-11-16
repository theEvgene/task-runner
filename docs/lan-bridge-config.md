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
  `dell-lan-local-dev`) to its installed target. Its containing checkout and
  `git rev-parse HEAD` identify the installed checkout and revision; compare
  them with the configured pin below.
- An owner-provided `lan-readiness-config/1` file has no fixed path. Locate it
  from a recorded invocation or ask for its path. Its `queue`, `keyFile`,
  `keyFormat`, and `lenovo.python` correspond to `queue_dir`, `key_file`,
  `key_format`, and `lenovo_python`. Its `lenovo.cwd` is the selected worker
  cwd; use it as `lenovo_jira_working_dir` only if verified as the Jira skill
  directory. `local.components`, `local.tools`, `lenovo.components`,
  `lenovo.tools`, and optional `lenovo.revision` are scenario-specific
  readiness checks, not shared settings or a Dell checkout pin. See
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

1. Resolve the installed skill junction under `%USERPROFILE%\.agents\skills` to its actual target. The release installer links skill directories into a pinned task-runner checkout. Locate this instruction and template from that checkout. Read the settings file and discover existing values before requesting missing information. Do not invent paths, revisions, or Jira instances.
2. Require schema `task-runner/lan-bridge-config/1`, nonempty absolute paths for the task-runner checkout, queue, and key file, key format `raw` or `base64`, and a full pinned commit SHA. For managed Jira reads additionally require nonempty absolute paths for `lenovo_python` and `lenovo_jira_working_dir`, plus an explicit `jira_instance`; do not require these Jira-only fields for an independent GitLab MR scenario. Name each absent or invalid component in the blocker. The key file is a path only: never read or print its contents during discovery, put key bytes in JSON, or place it in a repository, queue, or shared LAN folder.
3. Validate freshness before use: the resolved installed skill target must be in the configured checkout; Git HEAD must equal the configured full commit, and the checkout must have no changes to the selected skill, this instruction, template, or scripts and docs used by the selected route. Verify the selected route's required files exist. Name each missing file or mismatched/dirty component; stop rather than calling an older or unverified installation current. The configured commit is an operator pin, not proof of the newest remote revision; compare against the intended locally available revision when an update is requested. Do not fetch or update another checkout implicitly.
4. Check queue availability and key-file existence without displaying key material. Follow `docs/lan-bridge.md` and the selected scenario for key validation, signing, authorization, and result verification. Pass configured paths and format as existing CLI arguments; configuration does not alter the protocol. If a submitted result is unknown or observation is interrupted, recover the same JOBID; never submit a replacement while its outcome is unknown.
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

Submit it with the same configured cwd and bridge inputs, then fetch or observe its JOBID. For both probe and read, accept success only for a MAC-verified result bound to that JOBID with status `completed` or `ok`, integer `exit_code == 0`, no `timed_out`, and no `output_truncated`, as classified in `docs/lan-bridge.md`. An unrecognized status is unknown; a verified timeout, failure, or truncated output is not success. Recover an unknown or interrupted observation using the same JOBID. Preserve `parent.presence` and `gateway_unavailable` exactly. If queue, key, workflow, or result is unavailable, report the affected Jira categories as unverified without switching credential routes.
