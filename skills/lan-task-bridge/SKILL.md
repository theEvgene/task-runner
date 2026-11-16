---
name: lan-task-bridge
description: Agent-operable signed LAN command route to the Lenovo worker. Use for task-runner scenarios and, when local Nessy is absent, the managed Jira read required by existing Jira skills; submit and fetch the MAC-bound result without an intermediary.
---

# LAN task bridge

Before a live operation, follow `docs/lan-bridge-config.md` in the installed
pinned task-runner checkout. Both LAN skills use the same
`%USERPROFILE%\.config\task-runner\lan-bridge.json` settings. The instruction
covers discovery, freshness, missing components, and managed Jira reads.

Follow the selected scenario and `docs/lan-bridge.md` using
`scripts/lan_bridge/lan_bridge.py`. Submit its exact command file and fetch or
observe the same JOBID; accept only a MAC-verified result bound to that id.

For the managed Jira read, `lenovo_python`, `lenovo_jira_working_dir`, and
`jira_instance` come from the shared settings file per
`docs/lan-bridge-config.md`. That read is the installed external
`nessy-jira-workflow` workflow (`jira_task.py read ...`), not the
task-runner CLI. The task-runner Jira protocol CLI lives at
`scripts/jira-protocol.mjs` inside the verified Lenovo clone (see
`docs/lan-bridge-config.md` for the username-free checkout path and the
exact supported access route); this skill does not invoke it over the
bridge and never substitutes one transport for the other. Any task-runner checkout path used by another
route is that route's own per-run `--lenovo-checkout` flag: the verified
Lenovo clone is `Documents\repositories\task-runner` under the Lenovo user
profile (resolve on Lenovo with
`Join-Path ([Environment]::GetFolderPath('UserProfile')) 'Documents\repositories\task-runner'`,
pass the expanded absolute path per run, never a username or a `%...%` /
`$env:...` placeholder).
