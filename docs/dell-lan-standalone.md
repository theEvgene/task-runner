# Dell LAN standalone transfer and publication

Two narrower runs that reuse `docs/dell-lan.md` without changing its
combined `publish` route (base export, task work, bundle staging, signed
queue, SHA pins, and per-action consent all work exactly as documented
there). Canonical drivers: `scripts/dell_lan/` (stdlib only). Tests:
`<python> -m unittest discover -s tests/dell_lan -p "test_dell_transfer_only.py"`
plus `-p "test_dell_publish_existing.py"` (disposable fixtures: two local
Git repositories, one bare remote, the real signed queue, fixture doubles
for the external flow at its documented seams; no network).

## 1. Standalone transfer: branch import without publication (#42)

When only the branch move is needed, Dell writes a `transfer` request
instead of `publish` (same staged bundle and manifest, same
`--expected-base`/`--expected-commit`/`--expected-previous` pins, but no
MR metadata and `--authorize import[,reconcile]` only):

```text
python scripts/dell_lan/dell_request.py transfer \
  --shared <lan-share> --token <16hex> \
  --lenovo-skill-dir <abs-dir-on-lenovo> \
  --lenovo-driver-dir <abs-dell-lan-dir-on-lenovo> \
  --python <abs-python-on-lenovo> \
  --manifest <lan-share>\transfer-<16hex>.manifest.json \
  --bundle <lan-share>\transfer-<16hex>.bundle \
  --expected-base <base-sha> --expected-commit <task-commit> \
  [--expected-previous <prior-task-sha>] \
  --branch <task-branch> \
  --authorize import \
  --lenovo-checkout <abs-lenovo-checkout> \
  --lenovo-worktree <abs-lenovo-task-worktree> \
  --request-name transfer-request-<16hex>.json --command-file <command-file> \
  --repo <source-copy>
```

On Lenovo the single command runs `lenovo_transfer.py`, which reuses the
combined route's intake (`lenovo_bundle`: digest check, manifest
re-verification, one-time compare-and-swap `import_once`) and stops: the
result carries the worktree path, branch name, and verified SHA
(`import.commit`), with `push`/`mr` left empty as the visible proof that
nothing was published. A repeat names `--expected-previous`; mismatch,
dirty state, or a rewrite is refused without moving anyone else's
branch, and an unreadable post-move state is `unknown` (exit 3): keep
the slot and reconcile with the same command plus `--reconcile` (needs
`reconcile` in `--authorize`), which observes the branch/worktree heads
and writes nothing. Dell consumes either result through the existing
`dell_readback.py` path: the signed job and result envelopes must bind the
same job id, the driver command must be the documented transfer driver, and
the returned branch, commit, worktree ownership, and clean state must match
the requested pins. Keep the two worktree bindings distinct: pass the local
Dell task worktree as `--worktree` and the requested Lenovo task worktree as
`--lenovo-worktree`. The former must be registered in the Dell anchored copy
at the task commit; the transfer result must report the latter exactly. A dirty
or unreadable reconciliation remains `unknown`;
keep the same job slot and never submit a blind duplicate. The combined
`publish` route is unchanged.

## 2. Standalone publication of an existing Lenovo branch (#43)

When the branch already exists on Lenovo at exactly `--expected-commit`
(e.g. after a standalone transfer), Dell writes a `publish-existing`
request: no bundle, no re-import, no second GitLab client. Title fields
(`--change-type`, `--jira-key`, `--subject`, `--description-file`) are
optional at write time and required only when no open MR exists:

```text
python scripts/dell_lan/dell_request.py publish-existing \
  --shared <lan-share> --token <16hex> \
  --lenovo-skill-dir <abs-dir-on-lenovo> \
  --lenovo-driver-dir <abs-dell-lan-dir-on-lenovo> \
  --python <abs-python-on-lenovo> \
  --lenovo-flow-dir <abs-flow-scripts-dir-on-lenovo> \
  --expected-base <base-sha> --expected-commit <task-commit> \
  --branch <task-branch> --target-branch <base-branch> \
  --host <remote-host> --project <group/project> \
  [--change-type <feat|fix> --jira-key <KEY-1> --subject "<one line>" \
   --description-file <lan-share>\mr-description.md] \
  --state <draft|ready> \
  --authorize <push,mr[,undraft][,reconcile]> \
  --lenovo-checkout <abs-lenovo-checkout> \
  --lenovo-worktree <abs-lenovo-task-worktree> \
  --request-name publish-existing-request-<16hex>.json \
  --command-file <command-file> \
  --repo <source-copy>
```

On Lenovo `lenovo_publish_existing.py` verifies the branch ref and the
worktree HEAD both equal `--expected-commit`, runs the existing
freshness preflight and push, then finds the open MR by project and
source branch through the existing flow reader: an existing MR is never
duplicated and its Draft/Ready status is never changed -- the driver
only captures and verifies it (head SHA, project, branch, URL). Without
an MR, missing title data refuses the run with `missing-data` naming
the absent fields and creating nothing; Dell resubmits the same branch
with the data (a new job id) and the driver creates one Draft MR and
verifies its project, branches, status, and URL. An ambiguous MR list
(`mr-ambiguous`) or an unknown write (`mr-write-unknown`, exit 3) is
never reported as success and never retried blind: reconcile the same
job id through the read-only probe (`--reconcile`, needs `reconcile` in
`--authorize`) before any new attempt. SHA checks, per-action consent,
and the Draft/Ready semantics (Ready converges the description
byte-for-byte per #11, never lifted by accident per #10's area) are the
same gates the combined route enforces; nothing here re-implements the
GitLab client or the queue. Its sealed result is also consumed by
`dell_readback.py`: the authenticated `lenovo_publish_existing.py` envelope
is unwrapped through the existing publication identity checks, including the
task SHA, project, source/target branches, and MR URL. Timeout, truncation, an
exit-3 result, or a driver `unknown` outcome keeps the same job id for safe
recovery.

## Boundaries

Same as the combined loop: no second queue and no new transport;
secrets travel through `--key-file` only and every driver refuses a key
it cannot prove is outside the checkout, the queue, and the shared
folder; receipts follow the sharing rules of `docs/dell-lan.md`.
