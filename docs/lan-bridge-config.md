# Shared LAN bridge settings

Both LAN skills read `%USERPROFILE%\.config\task-runner\lan-bridge.json` before a live operation. Its versioned template is `config/lan-bridge.example.json` in the task-runner checkout. This local file is operator configuration, not a secret store; running a skill does not create or change it.

1. Resolve the installed skill junction under `%USERPROFILE%\.agents\skills` to its actual target. The release installer links skill directories into a pinned task-runner checkout. Locate this instruction and template from that checkout. Read the settings file and discover existing values before requesting missing information. Do not invent paths, revisions, or Jira instances.
2. Require schema `task-runner/lan-bridge-config/1`, nonempty absolute paths for the task-runner checkout, queue, and key file, key format `raw` or `base64`, and a full pinned commit SHA. For managed Jira reads additionally require nonempty absolute paths for `lenovo_python` and `lenovo_jira_working_dir`, plus an explicit `jira_instance`; do not require these Jira-only fields for an independent GitLab MR scenario. Name each absent or invalid component in the blocker. The key file is a path only: never read or print its contents during discovery, put key bytes in JSON, or place it in a repository, queue, or shared LAN folder.
3. Validate freshness before use: the resolved installed skill target must be in the configured checkout; Git HEAD must equal the configured full commit, and the checkout must have no changes to the selected skill, this instruction, template, or scripts and docs used by the selected route. Verify the selected route's required files exist. Name each missing file or mismatched/dirty component; stop rather than calling an older or unverified installation current. The configured commit is an operator pin, not proof of the newest remote revision; compare against the intended locally available revision when an update is requested. Do not fetch or update another checkout implicitly.
4. Check queue availability and key-file existence without displaying key material. Follow `docs/lan-bridge.md` and the selected scenario for key validation, signing, authorization, and result verification. Pass configured paths and format as existing CLI arguments; configuration does not alter the protocol. If a submitted result is unknown or observation is interrupted, recover the same JOBID; never submit a replacement while its outcome is unknown.
5. For managed Jira reads, use `lenovo_jira_working_dir` as the worker cwd and `lenovo_python` as the executable. First verify the installed workflow on Lenovo: write the following single line to a local UTF-8 command file (no secrets; substitute the configured Python path):

```text
"<lenovo_python>" "..\nessy-jira-workflow\scripts\jira_task.py" read --help
```

Submit and fetch it using `docs/lan-bridge.md`, the configured queue/key/format, that command file and `--cwd <lenovo_jira_working_dir>`. Inspect the verified, complete successful result's help output for `--jira-instance`. An explicit help output without the option establishes unsupported; an unavailable workflow, failed or incomplete probe, or ambiguous output leaves support unverified. Stop in either case; do not infer support from the Dell checkout or label an older workflow current. Apply the path interpolation restrictions in `docs/lan-bridge.md` to every substituted value.

Once support is established, write this single line to another local UTF-8 command file, substituting the configured values and issue key:

```text
"<lenovo_python>" "..\nessy-jira-workflow\scripts\jira_task.py" read --jira-instance "<jira_instance>" --issue "<KEY>" --full
```

Submit it with the same configured cwd and bridge inputs, then fetch or observe its JOBID. For both probe and read, accept success only for a MAC-verified result bound to that JOBID with status `completed` or `ok`, integer `exit_code == 0`, no `timed_out`, and no `output_truncated`, as classified in `docs/lan-bridge.md`. An unrecognized status is unknown; a verified timeout, failure, or truncated output is not success. Recover an unknown or interrupted observation using the same JOBID. Preserve `parent.presence` and `gateway_unavailable` exactly. If queue, key, workflow, or result is unavailable, report the affected Jira categories as unverified without switching credential routes.
