# Dell managed submit: one request, one reservation, one driver

`scripts/dell_lan/dell_managed_submit.py` is the Dell producer entry for the
managed-write routes (`transfer`, `publish`, `publish-existing`). It is the
section 6 step of `docs/dell-lan.md`; this page carries its exact contract.

```text
python scripts/dell_lan/dell_managed_submit.py \
  --queue <queue-dir> --key-file <key-path> \
  --request <requestPath> --request-sha256 <requestSha256> \
  --command-file <commandFile> --cwd <expectedCwd> --dialect <dialect> \
  --receipt-script-path <receiptScriptPath> \
  --shared <lan-share> --repo <source-copy> \
  --receipt <outside-every-checkout>\dell-managed-submit.json
```

## Footprint: the request, never the operator's directory

The claim set is derived only from the digest-bound request file: the Lenovo
task worktree *and* the Lenovo checkout whose ref store the route writes
(`lenovo_bundle.import_once` runs `update-ref`/`fetch`/`worktree add` there).
The job's working directory is that same checkout, so an admitted job's
reservation and its real write target cannot diverge.

`--cwd` must name the request's `lenovoCheckout`. A different directory
refuses with exit `2` (`cwd-unbound`): the operator's own directory is not a
provable target, and the request is. That value - not `--cwd` - is passed as
both the job `--cwd` and the signed expectation, so the worker's
`target-cwd-mismatch` check compares the same two paths the route writes. Do
not infer or translate the dialect after the command is built.

## Command binding: one driver invocation

The command file must parse (shlex, then the transport's explicit
`powershell -Command` launcher for the PowerShell dialect) to exactly one
invocation:

```text
<python> <driver> --request <requestPath> --request-sha256 <requestSha256>
```

- `<driver>` must be the one that owns the request schema:
  `lenovo_transfer.py`, `lenovo_publish.py`, or `lenovo_publish_existing.py`.
- `--request` must name this request file and `--request-sha256` its digest.
- Only the documented `--flow-dir <absolute>` and `--reconcile` may follow.

A substring marker, a second statement or operator, an extra argument, or
another request's command refuses with exit `1` (`command-unbound`) before any
queue write. This mirrors `verify_first_job` in `dell_lan_common.py`: a
substring match let a claim be paired with a command that wrote another
request's worktree.

## Refusals before publication

The entry refuses before any queue write when the request digest, the schema,
the worktree or checkout path, the pinned revisions, or the command/request
binding cannot be proven. The queue-local referee then refuses a conflicting
footprint - the same or a nested worktree, the same checkout ref store, or the
same working directory - with exit `2` before anything is published. A
different worktree inside a checkout already held is such a conflict: both
jobs would write the same refs.

## Completion, and the UNKNOWN outcome

On success the entry prints one `JOBID=<32hex>` the moment the job is
published and writes a `dell-lan-managed-submit/1` receipt with
`outcome: reserved` naming the job, the reservation generation and resource
digest, the exact claims, the pinned revisions, and the reservation owner.
That receipt is the reservation proof, not a pre-submit check.

If the reservation proof cannot be read after publication, the same `JOBID`
line is still printed, the exit code is `3`, and the receipt is
`outcome: unknown` with `reservation: null`, `resubmit: forbidden`, and the
same-id next action. The job and its reservation stay published: keep the slot
and re-read the SAME id. Never resubmit the worktree or its checkout after a
timeout or exit `3` - the footprint stays held until the worker publishes a
proof-bound release (`docs/lan-bridge.md`).

## What runs on Lenovo

The single command runs the schema's driver, which re-hashes the request,
re-verifies the staged bundle, imports the task commit once with a
compare-and-swap on `refs/heads/<task-branch>` (a repeat names the prior SHA;
mismatch, dirty tree, or rewrite is refused), runs the external freshness
gate, pushes the branch, creates or updates the Draft MR (never a second MR
for one branch), and reads the identity back. A `ready` request then runs the
Ready route in `docs/dell-lan.md`: the external `remove_draft.py` marks the
same MR Ready, and the driver re-captures the identity and proves the title
and description still match the request input.

A submit without a claim keeps the legacy sequential fallback and its
`exclusive:global` worker behaviour; the managed entry above is the supported
producer path for these three routes.
