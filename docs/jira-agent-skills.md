# Jira protocol agent skills

Nine model-invoked, `cli-`-prefixed skills package the Jira protocol CLI by
operation:

- `cli-create-lel-service-backlog-issue`
- `cli-assign-jira-issue`
- `cli-transition-jira-issue`
- `cli-transition-jira-status`
- `cli-extract-jira-content`
- `cli-find-sibling-issues`
- `cli-flag-jira-issue`
- `cli-create-jira-subtask`
- `cli-create-jira-subtasks`

Each skill runs the installed CLI on the host that holds the authorized
session: locally, or on Lenovo through the generic signed queue as
`docs/lenovo-hosted-scripts.md` states (same command, file-backed free text
over the LAN share, credentials never on the queue).

They intentionally coexist with the Personal Agent Toolkit Jira skills. Their
prefix makes the transport choice visible; they do not replace or silently
fall back to `dp jira`, Nessy, browser automation, or an Atlassian connector.

## Installation

Install the suite after `scripts/jira-protocol.mjs`, its
`scripts/jira_protocol/` modules, and `docs/jira-protocol.md` are present in the
same task-runner revision:

```powershell
node scripts/install-cli-jira-skills.mjs `
  --target-root $env:CODEX_HOME\skills
```

The installer refuses an existing destination and rolls back a partial suite.
It copies the same protocol runtime and documentation into every installed
skill, so commands resolve relative to the loaded `SKILL.md` instead of the
caller's working directory. `runtime-manifest.json` records SHA-256 for every
injected runtime file. To package from another task-runner checkout during
integration, pass `--runtime-root <checkout>`; this selects a source tree, not
a different Jira protocol.

The CLI accepts session material only through `JIRA_SESSION_COOKIE` and, for
assignment, `JIRA_ATL_TOKEN`. Installation neither obtains nor stores them.

## Actual command and guarantee boundary

The skills use only the documented commands:

| Skill      | Commands                              | Proven successful readback                                                                                                 |
| ---------- | ------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| create     | `create-epic`, optional `detail-view` | returned key is a field-verified LEL Epic on rapid view 298290's board or backlog                                          |
| subtask    | `create-subtask`                      | returned key is a field-verified LEL subtask whose parent key equals the requested parent                                  |
| subtasks   | `create-subtasks`                     | every batch entry is `Created` (field-verified, exact parent) or a proven `AlreadyExists`; no duplicate confirmed instance |
| assign     | `assign`                              | direct issue read reports the exact assignee login                                                                         |
| transition | `transitions`, `transition`           | direct issue read reports the planned target status id                                                                     |
| route      | `transition-to-status`                | direct issue read reports the target status id after every verified step                                                   |
| extract    | `extract`                             | REST-visible issue, comments, metadata inventories, and immediate parent                                                   |
| siblings   | `siblings`                            | same-parent subtasks assigned to the exact login, unreadable children reported                                             |
| flag       | `flag`                                | direct issue read reports the requested flag state plus a new comment id                                                   |

Every write is a read-only plan followed by a separate invocation carrying the
exact live-model-bound confirmation digest. `UnknownOutcome` is never
permission to retry automatically, and HTTP 401/403 is terminal.

Important limitations:

- There is no JQL or duplicate-search command. Creation discloses that gap and
  stops on any already-known likely duplicate.
- Creation readback requires exact persisted Summary and Description plus a
  matching persisted value/id/key/name for every submitted custom field, as
  well as project and issue type. Epics additionally require confirmed board
  or backlog placement; subtasks additionally require exact parent linkage.
- Transition is one currently available transition, not automatic pathfinding.
  The CLI validates but does not expose discovery of target board column ids.
  `transition-to-status` covers live-discovered multi-step routes to an
  explicit status id instead: every edge is re-discovered, and ambiguous or
  form-bound paths stop with their reason.
- `extract` proves completeness only for its declared caller-visible REST
  scope. Server-rendered Jira sections, Wiki pages, remote links, attachment
  bodies, and attachment text remain explicit unproven/not-retrieved gaps.
- `siblings` examines only the listed subtasks of the immediate parent by
  exact assignee login. It is not a JQL or duplicate search: unlisted,
  unobserved, or unreadable children are reported, never silently dropped,
  and an incomplete receipt never proves that no sibling exists.
- `detail-view` remains a separate explicitly incomplete GreenHopper UI-model
  command and is not an extraction fallback.
- The CLI has no safe reconciliation command after an ambiguous write. Keep the
  receipt and stop instead of replaying the mutation.
