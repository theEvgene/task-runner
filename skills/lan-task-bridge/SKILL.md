---
name: lan-task-bridge
description: Agent-operable signed LAN command route to the Lenovo worker. Use for task-runner scenarios and, when local Nessy is absent, the managed Jira read required by existing Jira skills; submit and fetch the MAC-bound result without an intermediary.
---

# LAN task bridge

The canonical client and protocol contract are
`scripts/lan_bridge/lan_bridge.py` and `docs/lan-bridge.md`. Run them from the
pinned task-runner checkout. This skill adds no queue, cryptography, worker, or
retry behavior.

For ordinary task-runner scenarios, follow the scenario document that built the
command. Submit its exact command file, then fetch or continuously observe the
same job id. Accept only a MAC-verified result bound to that id. Unknown means
read the same id again; it never authorizes a second submission.

## Managed Jira read compatibility

When an existing Jira skill routes here because local Nessy is unavailable,
preserve its managed-read boundary:

1. Write a UTF-8 command file containing only:

   `python ..\nessy-jira-workflow\scripts\jira_task.py read --issue <KEY> --full`

2. Submit it with `scripts/lan_bridge/lan_bridge.py submit`; the owner-provided
   Lenovo working directory is the installed `extract-jira-content` skill
   directory.
3. Fetch the same id with `scripts/lan_bridge/lan_bridge.py fetch`. Accept Jira
   data only when the verified result has exit code `0`, then preserve the
   returned `parent.presence` and `gateway_unavailable` values exactly.

If the queue or key is unavailable, or the bounded fetch remains unknown,
report the Jira categories as unverified and stop. Do not substitute direct
Jira, HTTP, browser, or another credential route.

The local fixture suite is `python -m unittest discover -s tests/lan_bridge -v`;
it performs no managed Jira or other corporate operation.
