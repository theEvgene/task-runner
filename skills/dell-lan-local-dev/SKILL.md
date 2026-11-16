---
name: dell-lan-local-dev
description: Dell-side development through the task-runner signed LAN queue. Use for the full Dell-to-Lenovo transfer and publish loop, transfer-only, publication of an existing Lenovo branch, readiness checks, or preparing an existing merge request for Ready.
---

# Dell LAN local dev

Thin pointer. This repository owns the drivers and operational contracts; the
skill adds no transport, publication, or Ready logic.

Before a live operation, follow `docs/lan-bridge-config.md` in the installed
pinned task-runner checkout. Both LAN skills use
`%USERPROFILE%\.config\task-runner\lan-bridge.json`.

Choose one canonical route:

- Check the selected scenario before a live run: `docs/readiness.md` and
  `scripts/readiness/check.py`.
- Transfer and publish in one consent-bound run: `docs/dell-lan.md` and
  `scripts/dell_lan/dell_request.py publish`.
- Transfer without publication, or publish an already transferred Lenovo
  branch: `docs/dell-lan-standalone.md` and the `transfer` or
  `publish-existing` request subcommand.
- Prepare a specific existing merge request for Ready without transferring or
  creating a branch: `docs/mr-ready.md` and `scripts/mr_ready/`.
- Operate or recover the signed queue itself: `docs/lan-bridge.md` and
  `scripts/lan_bridge/`.

Use paths from the pinned task-runner checkout on both hosts. Keep development,
build, lint, and tests on Dell; use Lenovo only for the corporate and
closed-dependency steps named by the selected route. Each remote read or write
needs the route's explicit authorization. An unknown result is recovered under
the same job id; it is never permission to submit a replacement.

Run only the fixture suite relevant to a change. The documented tests do not
contact Lenovo, Jira, GitLab, or another network service.
