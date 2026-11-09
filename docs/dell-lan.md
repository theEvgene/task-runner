# Dell LAN dev loop

> Agent entry. Use ONLY this scenario when: you edit on a host that cannot
> reach the remote (Dell), and a second host (Lenovo) must export the
> published base, then import your task commit and publish it through the
> external publication flow. It gives you a MAC-verified base SHA, a
> one-writer task worktree, per-action consent for every Lenovo-side write,
> and a readback that binds the exact SHA and merge request. Do NOT use it
> for: base-only work without publication (that is `docs/fresh-base.md`),
> a read-only pipeline check (that is `docs/pipeline-observer.md`), or any
> push, retry, or MR edit outside the per-action consent below.

Dell is where the agent edits; Lenovo is the host that reaches the remote.
The loop runs in both directions over the one channel that already exists
(`docs/lan-bridge.md`): Lenovo exports the published base, Dell imports it
once, works and checks locally, then Lenovo imports the task commit and
publishes it through the owner-provided external flow. Content crosses on the
owner-provided LAN share that already hosts the queue; authenticity travels
inside the HMAC-signed job payload as SHA-256 values.

Every step below is one exact command. Nothing here adds a transport, a
cryptography variant, a publication receipt, or a second writer.

Canonical drivers: `scripts/dell_lan/` (stdlib only). Tests:
`<python> -m unittest discover -s tests/dell_lan -v` (disposable fixtures:
two local Git repositories, one bare remote, the real signed queue, fixture
doubles for the external flow at its documented seams; no network).

## Consent boundary

Each Lenovo action needs the user's prior consent for that run, asked once per
action and recorded before the job is written:

1. the base request job (Lenovo reads its checkout and writes a bundle);
2. the task import (Lenovo writes a task worktree and a branch ref);
3. the push (Lenovo writes the remote branch);
4. the merge-request create or update (Lenovo writes remote metadata);
5. the read-only reconciliation probe (Lenovo reads the remote branch and the
   merge-request list).

`dell_request.py publish --authorize` lists exactly the actions that were
authorized, and `lenovo_publish.py` refuses any action that is not listed. A
request without `push` or `mr` cannot publish even if it is submitted, and a
`--reconcile` run needs `reconcile` in the list before any Lenovo read starts.

## Preconditions (owner-provided, never guessed)

- queue directory and a 32-byte key file, both outside every repository, the
  queue, and the shared LAN folder; the Lenovo worker is already running;
- one LAN share both machines read and write — the same share that hosts the
  queue, not a new mount;
- a Lenovo checkout of this repository plus Python and Git on both sides;
- the external publication flow checkout on Lenovo (preflight, writer,
  readback), named per run via `--lenovo-flow-dir` or `DELL_LAN_FLOW_DIR`;
  without it the driver fails closed with `flow-unavailable` and publishes
  nothing;
- working remote credentials on Lenovo for that external flow (its own
  credential setup, e.g. an authenticated CLI or token in the Lenovo user
  context). No credential ever travels the queue or the share: Dell holds
  only the queue key, and the signed jobs carry digests, never secrets.

If any item is missing, stop and name that item. Do not substitute another
channel, another machine, or a remembered path.

## 1. Request the published base from Lenovo

Write the request and its command file. The request lives on the LAN share;
only its SHA-256 travels inside the signed job.

```text
python scripts/dell_lan/dell_request.py base \
  --shared <lan-share> --token <16hex> \
  --lenovo-skill-dir <abs-dir-on-lenovo> \
  --lenovo-driver-dir <abs-dell-lan-dir-on-lenovo> \
  --python <abs-python-on-lenovo> \
  --lenovo-checkout <abs-lenovo-checkout> --repo <source-copy> \
  --ref-full refs/heads/<base-branch> \
  --request-name base-request-<16hex>.json --command-file <command-file>
```

`--lenovo-driver-dir` is the `scripts/dell_lan` directory of this repository
on Lenovo; omit it only when `--lenovo-skill-dir/scripts` already holds the
drivers (legacy checkout layout). `--repo` is the local Dell checkout; the
writer refuses a shared path inside it.

Submit it with the standard client (`docs/lan-bridge.md`), declaring the
manifest as a return file so a small MAC-bound artifact also comes back
in-band. Completion: submit printed one `JOBID=<32hex>`.

## 2. Receive, verify, and import the base exactly once

```text
python scripts/dell_lan/dell_base_import.py \
  --queue <queue-dir> --key-file <key-path> --job-id <job-id> \
  --shared <lan-share> --repo <source-checkout> --repo-out <new-source-copy> \
  --branch <base-branch> --expected-project <group/project> \
  [--bridge-dir <lan_bridge-scripts-dir>] \
  --provenance <fixture|live> \
  --receipt <outside-every-checkout>\base-import.json
```

The importer verifies the sealed result (MAC, kind/id, exit, timeout,
truncation), requires the documented export command, re-hashes the bundle
against the SHA-256, byte count, and tip commit inside that result, re-reads
the `git-bundle-manifest/1` digest, then verifies and fetches the bundle once
into a path that must be empty. `--bridge-dir` defaults to this repository's
`scripts/lan_bridge` (override with `DELL_LAN_BRIDGE_DIR`).

Completion: receipt `dell-base-import/1` with `verdict: imported`, `baseSha`,
and `repo.head` equal to `baseSha`. A second import into the same path is
refused (`repo-exists`): one source copy, never replaced.

## 3. Cut one task worktree, work and check locally

```text
git -C <source-copy> worktree add -b <task-branch> <task-worktree> <base-sha>
```

One task, one worktree, one writer. Edit only inside the task worktree and
run the task's own checks there; a failing check stops the loop before any
transfer. For a changed `package-lock.json`, evaluate it against the owned
npm cache with `scripts/dell_lan/dell_npm_probe.py` (disposable scratch copy,
`npm ci --offline`, never the worktree); see
`scripts/dell_lan/references/npm-offline-deps.md`.

## 4. Stage the task bundle

Stage the worktree tip with the repository's staging script (bundle create
plus verify, per-file hashes, atomic manifest write):

```text
python scripts/dell_lan/stage_bundle.py \
  --worktree <task-worktree> --shared <lan-share> \
  --bundle-name transfer-<16hex>.bundle \
  --manifest-name transfer-<16hex>.manifest.json \
  --expected-tip <task-commit> \
  --receipt <outside-every-checkout>\bundle-receipt.json
```

Completion: receipt `dell-bundle-stage/1` with `outcome: staged` and the
bundle tip equal to the task commit. Every attempt stages a fresh unique
name: an existing bundle file is never replaced, and a raw SHA `--ref` is
refused (a bundle needs an advertised ref, so stage from a branch name or
`HEAD`). Pass the staged `--bundle`/`--manifest` paths of this attempt to
the publish request below; never re-stage over another attempt's files.

The external issue-517 bundle stager from the personal-agent-toolkit stays
an accepted alternative when its prerequisite holds (a toolkit checkout
with Node on Dell); it writes the same `git-bundle-manifest/1` shape the
Lenovo driver verifies. Either way, the driver re-verifies the manifest
digest, every file hash, and the bundle tip before importing — staging is
transport, never trust.

## 5. Write the publish request with per-action consent

```text
python scripts/dell_lan/dell_request.py publish \
  --shared <lan-share> --token <16hex> \
  --lenovo-skill-dir <abs-dir-on-lenovo> \
  --lenovo-driver-dir <abs-dell-lan-dir-on-lenovo> \
  --lenovo-flow-dir <abs-flow-scripts-dir-on-lenovo> \
  --python <abs-python-on-lenovo> \
  --manifest <lan-share>\transfer-<16hex>.manifest.json \
  --bundle <lan-share>\transfer-<16hex>.bundle \
  --description-file <lan-share>\mr-description.md \
  --expected-base <base-sha> --expected-commit <task-commit> \
  [--expected-previous <prior-task-sha>] \
  --branch <task-branch> --target-branch <base-branch> \
  --host <remote-host> --project <group/project> \
  --change-type <feat|fix> --jira-key <KEY-1> --subject "<one line>" \
  --state draft \
  --authorize <import,push,mr[,reconcile]> \
  --lenovo-checkout <abs-lenovo-checkout> \
  --lenovo-worktree <abs-lenovo-task-worktree> \
  --request-name publish-request-<16hex>.json --command-file <command-file> \
  --repo <source-copy>
```

Only the actions the user authorized go in `--authorize`; `reconcile` only
when the read-only probe was authorized too. `--lenovo-flow-dir` names the
external flow checkout on Lenovo (or set `DELL_LAN_FLOW_DIR` there); without
either, the driver fails closed before any write. Only `draft` publishes; a
repeat transfer names `--expected-previous` so the branch moves only while it
still holds the prior SHA.

## 6. Submit and let Lenovo publish; read back the exact SHA and MR

Submit the command file with the standard client. On Lenovo the single
command runs `lenovo_publish.py`, which re-hashes the request, re-verifies
the staged bundle, imports the task commit once with a compare-and-swap on
`refs/heads/<task-branch>` (a repeat names the prior SHA; mismatch, dirty
tree, or rewrite is refused), runs the external freshness gate, pushes the
branch, creates or updates the Draft MR (never a second MR for one branch),
and reads the identity back. Completion: submit printed one `JOBID=<32hex>`.

Back on Dell, verify the bound result:

```text
python scripts/dell_lan/dell_readback.py \
  --repo <source-copy> --expected-base <base-sha> --task-commit <task-sha> \
  --worktree <task-worktree> --bundle-manifest <lan-share>\transfer.manifest.json \
  --bundle-dir <lan-share> --shared <lan-share> \
  --queue <queue-dir> --key-file <key-path> \
  --job-id <job-id> --project <group/project> --source-branch <task-branch> \
  --target-branch <base-branch> --provenance <fixture|live> \
  --receipt <outside-every-checkout>\dell-readback.json
```

Verdicts: `ready` (transfer-ready only), `published` (a live, unmarked
result whose MR head, Lenovo branch head, and observed remote head all equal
the task commit), `simulated` (the same chain labelled a fixture), `stale`,
`unknown` (exit 3), `rejected` (exit 1), `mismatch` (exit 1). Only
`published` asserts remote publication.

## 7. Reconcile an unknown or rejected outcome before any retry

- **Unknown push** (`stage: push`, exit 3): keep the job id and re-fetch it; a
  late sealed result is accepted by MAC and id binding. If the slot stays
  empty, submit the read-only probe — the same command with `--reconcile`
  appended — which observes the remote branch and the MR list and writes
  nothing. The probe needs `reconcile` in the request's `--authorize`.
- **Unknown MR write** (`stage: mr`, exit 3): the write may have landed. The
  probe reports the existing MR; a later publish request updates it, so no
  second MR is opened. Never re-drive blind. While an outcome is unknown, the
  slot stays occupied and no second writer starts.
- **Rejected** (exit 1): read the carried exit code and stderr, fix on the
  anchored base, and submit a new job id. The queue refuses to reuse an id.
- **Stale**: keep working offline on the anchored base and re-sync before the
  next transfer.

## Boundaries

- One source copy, imported once at an empty path, never replaced; one
  writer; no second queue and no second transport.
- Secrets: the key travels through `--key-file` only, and every driver
  refuses a key it cannot prove is outside the checkout, the queue, and the
  shared folder. Those roots are mandatory arguments, never optional hints.
  Remote credentials live only in the Lenovo user context for the external
  flow; they are never written to a request, a command file, or a receipt.
- Receipt labels keep the task-runner path spellings (`scripts/dell_lan/…`,
  `scripts/lan_bridge/…`) so receipts from this home stay comparable.
- A receipt marked `provenance: fixture` is never live evidence.

## Sharing receipts outside the loop

Receipts embed remote topology: the base-export result carries `remotes`
(the checkout's remote URLs), and readback/publication receipts carry
`mrUrl`, `gitlabHost`/`projectPath`, branch names, SHAs, and capture times.
Before pasting any receipt into an issue, chat, or public report, strip the
`remotes` object and weigh every URL-bearing field (`mrUrl`, remote hosts,
project paths): keep them only when the audience is entitled to the remote.
SHAs, verdicts, reasons, and digests carry no topology and are safe to
quote.

## Relation to the other scenarios

- `docs/fresh-base.md` stages the same base-export/import mechanics in
  JavaScript for base-only work; it never pushes or opens a merge request.
- `docs/pipeline-observer.md` is the read-only pipeline check for one MR and
  one SHA; it supersedes the older corporate Dell pipeline probe scripts,
  which stay in the toolkit and are not moved here because they vendor
  corporate flow modules.
