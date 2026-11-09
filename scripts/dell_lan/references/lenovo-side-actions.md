# Lenovo-side actions and the factual readback

What runs on Lenovo, what it is allowed to write, and what a Dell-side run may
conclude from the result. Two commands exist, one per direction; both are
single invocations of this skill's drivers, carried inside the HMAC-signed job
payload of the existing `lan-task-bridge` queue.

## Binding: the request digest is inside the MAC

Free text, paths, branch names, and authorizations live in a request file on
the owner-provided LAN share. The job payload carries only the request's
SHA-256. Each driver re-hashes the file and refuses a request the job did not
name (`request-digest-mismatch`), so a request swapped on the share cannot
drive an action. The command file names an absolute interpreter path
(`--python`), so the worker cannot resolve a different `python` from its own
`PATH`.

The same rule covers the staged bundle: its SHA-256, byte count, tip commit,
and `git-bundle-manifest/1` digest travel inside the MAC-verified sealed result
of the base request, and the publish driver re-hashes the bundle against its
manifest before importing it. A bundle swapped on the share is refused
(`artifact-mismatch`); the share is transport, not trust.

## Direction one: `lenovo_base_export.py`

Read-only with respect to Git history. It resolves `refs/heads/<base-branch>`,
creates `<shared>/dell-base-<token>.bundle`, writes the issue-517 manifest,
and prints one JSON line with `tipSha`, `sha256`, `bytes`, `manifestDigest`,
and the checkout's remotes. The manifest is declared as a return file, so a
small MAC-bound artifact also arrives in-band.

It never pushes, never edits GitLab, never writes into the checkout, and
refuses to replace an existing bundle file.

## Direction two: `lenovo_publish.py`

Requires `import`, `push`, and `mr` in the request's `authorized` list,
separately, and refuses an action that is not listed (`consent-missing`,
exit 2) before it runs. A request whose `state` is not `draft` is refused as
`request-malformed` before any action, so a Ready transition cannot pass the
driver even if the request writer was bypassed. It then:

1. verifies the request digest and the staged bundle against the manifest;
2. requires the expected base commit to exist in the Lenovo checkout;
3. fetches the task commit under `refs/tmp/dell-import/<token>`, verifies it,
   and promotes it to `refs/heads/<task-branch>` with a compare-and-swap — an
   existing branch at another commit is refused (`branch-owned`). A repeat
   names `expectedPreviousCommit`: the branch moves only when it still points
   there, the owned worktree is registered there and clean, and the new commit
   descends from it (`previous-mismatch`, `worktree-dirty`, and
   `non-descendant` refuse). The move is one atomic ref compare-and-swap,
   `git update-ref refs/heads/<task-branch> <new> <prior>`, which writes only
   while the branch still holds the named prior SHA — a concurrent commit,
   including a same-tree commit that `git merge --ff-only` would accept as an
   ancestor, fails the swap and moves nothing. The index and files are then
   synced by `git read-tree -m -u <prior> <new>`, a refusing two-tree update
   that never overwrites locally changed entries and aborts on an untracked
   collision; as a separate step it is not atomic with the ref swap, so a
   refusal or any residue reports `import-unknown` (exit 3) with the edits
   intact and the branch at the new commit — recoverable, never a silent
   success. Rerunning completes an interrupted sync from intact files and
   refuses a dirty worktree already at the new commit instead of publishing it;
4. creates or reuses a dedicated task worktree at `lenovoWorktree`; the owner's
   checkout HEAD and other worktrees are never moved;
5. runs the existing `gitlab-mr-flow` freshness gate
   (`mr_preflight.py --mode refresh --remote origin`) and stops on a non-zero
   exit (`preflight-blocked`);
6. pushes with `git push origin <task-branch>` and observes the remote head with
   `git ls-remote --heads origin <task-branch>`;
7. finds an open MR for that source branch through `gitlab_api.api_get` and
   creates or updates the Draft MR through `publish_mr.publish` — a repeat run
   updates the existing MR instead of opening a second one;
8. reads the MR identity back through `mr_identity.capture_identity` and prints
   one `dell-lan-publish-result/1` JSON line whose `identity` field is that
   receipt verbatim.

A Ready transition is not implemented: `publish_mr` requires the
`mr_prepare.py` target-SHA evidence, and iteration 1 publishes Draft only.

## What the result proves

A Dell-side verdict of `published` requires all of:

- the sealed job for the same id verifies and carries this publication command;
- the sealed result verifies (HMAC over the exact payload bytes, kind/id
  binding), `status` `ok`, `exit_code` `0`, not timed out, not truncated;
- the driver envelope is `outcome: ok` (or `observed` for the read-only probe);
- the observed remote head equals the task commit;
- the nested `mr_identity` receipt is `schema_version` 1, its `head_sha` equals
  the task commit, its `local.source_branch_head` equals the task commit with
  `head_matches_receipt: true`, and its MR URL/IID match the driver envelope;
- any `--project`, `--source-branch`, or `--target-branch` pin matches;
- the receipt is not marked `simulated` and `--provenance live` was passed.

Anything less stays `simulated`, `stale`, `unknown`, `rejected`, or `mismatch`.

## What it cannot prove

- A push or MR exists only as GitLab reports it at capture time. A later push
  makes that receipt stale for the new head and needs a new capture.
- The `local` block is the Lenovo checkout's own view, not a second remote read.
- A key holder can seal any payload; the key stays with its owner, outside the
  repository, the queue, and the shared LAN folder — in every spelling the
  filesystem accepts — and a fixture is always labelled.
- Nothing here proves the shared path is reachable in a live run: no mapped
  drive or UNC path was exercised in this environment.

## Unknown outcomes and reconciliation

An unknown outcome is a slot that stays occupied, never an empty slot.

- **Push unknown** (exit 3, `stage: push`): the driver observed a non-zero push
  exit or a remote head that is not the task commit, and opened no MR. Re-fetch
  the same job id first. If the slot stays empty, submit the same command with
  `--reconcile` appended: it observes `git ls-remote` and the MR list and
  writes nothing. The probe is its own consented action: the request must list
  `reconcile` in `authorized`, or the driver refuses it (`consent-missing`,
  exit 2) before it reads anything. Re-publishing the same id is refused by the
  immutable queue.
- **MR unknown** (exit 3, `stage: mr`): the write may have landed. The probe
  reports the existing MR, and a later publish request updates it rather than
  creating a second one. The Dell readback stays `unknown` (`outcome-unknown`,
  exit 3): keep the slot, re-fetch the same job id, and never advise a new job
  id for an unreconciled write. A timed-out or truncated envelope and any
  exit-3 result are `unknown` for the same reason: a new job id needs proof of
  failure, and none of these carries it.
- **Rejected** (exit 1): the carried exit code and stderr name the failing
  stage; fix on the anchored base and submit a new job id.

## Simulation rules

A fixture run uses the same chain with a local queue, a fixture worker that
executes the command, a local bare repository as the GitLab remote, and a
fixture `glab` on a fixture-only PATH. The readback is called with
`--provenance fixture` (the default), which caps the verdict at `simulated`.
A receipt that declares itself `simulated` can never produce `published`, even
with `--provenance live`.
