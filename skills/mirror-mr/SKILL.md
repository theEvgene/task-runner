---
name: mirror-mr
description: Open a supplied GitLab MR in private GitHub for review. Use when the user names one GitLab merge request URL and asks to open, copy, or mirror it as a private GitHub PR.
---

# Mirror MR

Thin pointer. The task-runner checkout owns the flow (`docs/mirror-mr.md`)
and the single entry (`scripts/mirror_mr/mirror_mr.py`); this skill adds no
transport, queue, publication, or PR logic.

## Invoke

1. Require one explicit `https://<host>/<project>/-/merge_requests/<iid>`
   URL from the user. An ambiguous request ("that MR", no URL, several
   URLs) stops: ask for the single URL. Never guess a URL, IID, project,
   or mirror.
2. Resolve the installed checkout: the `mirror-mr` junction target under
   `%USERPROFILE%\.agents\skills`. Read `docs/mirror-mr.md` ("Single
   entry") and `docs/lan-bridge-config.md` there. Do not use a stale
   checkout.
3. Run the canonical command from that checkout (one MR per run):

```text
python scripts/mirror_mr/mirror_mr.py --mr-url <url> \
  --mapping-file <mirror-mapping/1> --shared <lan-share> \
  --mirror-dir <private-mirror-clone> --queue <queue-dir> \
  --key-file <key-path> --lenovo-checkout <abs-lenovo-checkout> \
  --python <abs-lenovo-python> --state <outside-mirror-state> \
  --receipt <outside-mirror-receipt> [--mode auto|open|merged] \
  [--lenovo-flow-dir <dir>] [--github-dir <dir>] [--provenance live]
```

`--mode auto` (default) determines live status through the signed export:
an open MR completes the open route, a merged MR completes the historical
route. `--mode open|merged` pins one route and refuses the other status.
Fixture doubles ride on `--lenovo-flow-dir`/`--github-dir`; live runs use
`provenance live` with the operator's `gh` context.

4. Report missing mapping or inputs as a blocker: no mirror configured,
   non-absolute paths, state/receipt inside the mirror, or an unreadable
   settings file. Never invent paths, mirrors, or credentials.

## Follow the command's contracts

- Private destination only: a non-private mirror refuses before any write.
- Same signed job on unknown: rerun the identical command with the
  identical `--state`; never submit a replacement while the outcome is
  unknown.
- One PR per MR: a rerun adopts the verified PR, never duplicates it.
- Return the PR URL, the live MR status (`opened`/`merged`), the mode used,
  and material limits (deleted source, squash representation, evidence
  gaps). Never merge, never submit a review, never force-push, never move
  the default branch, and never issue a duplicate mutation on unknown.
