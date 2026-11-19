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
owner-provided LAN share that also hosts the queue; authenticity travels
inside the HMAC-signed job payload as SHA-256 values.

The commands below use the existing transport and one writer.

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
5. the undraft step (Lenovo runs the external `remove_draft.py` once against
   the same MR) — only for a `ready` request;
6. the read-only reconciliation probe (Lenovo reads the remote branch and the
   merge-request list).

`dell_request.py publish --authorize` lists exactly the actions that were
authorized, and `lenovo_publish.py` refuses any action that is not listed. A
request without `push` or `mr` cannot publish even if it is submitted,
a `--state ready` request without `undraft` is refused before any write
(exit 2, `consent-missing`), and a `--reconcile` run needs `reconcile`
in the list before any Lenovo read starts.

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

Submit it with the standard client (`docs/lan-bridge.md`), passing
`--expected-cwd <expectedCwd>` with the same `--cwd` and declaring the
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

## 3b. Restore Dell dependencies

See [Restore Dell dependencies](dell-lan-dependencies.md) for the batch restore workflow.

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
name: an existing bundle, manifest, or receipt file is never replaced (a
concurrent writer that wins the name also wins), and a raw SHA `--ref` is
refused (a bundle needs an advertised ref, so stage from a branch name or
`HEAD`). `--bundle-name` and `--manifest-name` are required and must differ;
a handled failure removes the temporaries and any artifact this attempt
published, so a refused stage leaves no partial orphan. `--receipt` must be
absolute and outside the task worktree, the shared folder, and every Git
checkout. Pass the staged `--bundle`/`--manifest` paths of this attempt to
the publish request below; never re-stage over another attempt's files.

Only this repository's staging script is accepted
(`scripts/dell_lan/stage_bundle.py`): it writes the same
`git-bundle-manifest/1` shape the Lenovo driver re-verifies
(`lenovo_bundle.verify_bundle`: manifest digest, every file hash and byte
count, then the bundle tip against `--expected-commit` before any import).
An external stager is not an accepted alternative — the driver trusts only
what it re-verifies.

## 5. Write the publish request with per-action consent

```text
python scripts/dell_lan/dell_request.py publish \
  --shared <lan-share> --token <16hex> \
  --dialect <cmd|powershell> \
  --lenovo-skill-dir <abs-dir-on-lenovo> \
  --lenovo-driver-dir <abs-dell-lan-dir-on-lenovo> \
  --lenovo-flow-dir <abs-flow-scripts-dir-on-lenovo> \
  --python <abs-python-on-lenovo> \
  --manifest <lan-share>\transfer-<16hex>.manifest.json \
  --bundle <lan-share>\transfer-<16hex>.bundle \
  --expected-base <base-sha> --expected-commit <task-commit> \
  [--expected-previous <prior-task-sha>] \
  --branch <task-branch> --target-branch <base-branch> \
  --host <remote-host> --project <group/project> \
  [--change-type <feat|fix> --jira-key <KEY-1> --subject "<one line>" \
   --description-file <lan-share>\mr-description.md] \
  --state <draft|ready> \
  --authorize <import,push,mr[,undraft][,reconcile]> \
  [--reconcile] \
  --lenovo-checkout <abs-lenovo-checkout> \
  --lenovo-worktree <abs-lenovo-task-worktree> \
  --request-name publish-request-<16hex>.json --command-file <command-file> \
  --repo <source-copy>
```

`--dialect` is mandatory and names the shell that will execute the generated
command file. The writer never infers it: an omitted or unsupported value
exits 2 before either file is written. The PowerShell dialect builder produces
a literal program beginning with the call operator (`&`); the writer then
applies the one supported explicit `powershell.exe -Command` launcher for
signed submit and worker execution.
`cmd` output uses the quoted Windows command form. Cmd arguments containing
`%` or `!` expansion markers, control characters, or an embedded double quote
are unrepresentable and refuse before either file is written. PowerShell
single-quoted arguments preserve `%` and `!`; the worker replaces the validated
launcher with an encoded wrapper before crossing `cmd.exe`. An embedded double
quote in a PowerShell argument still refuses at transport adaptation because it
would close the supported launcher's script quote. Diagnostics name only the
dialect and fixed reason.

Only the actions the user authorized go in `--authorize`; `reconcile` only
when the read-only probe was authorized too, and `undraft` only when the
user consented to marking the MR Ready. `--lenovo-flow-dir` names the
external flow checkout on Lenovo (or set `DELL_LAN_FLOW_DIR` there); without
either, the driver fails closed before any write. A `draft` request
publishes a Draft MR and never runs the undraft step; a repeat transfer
names `--expected-previous` so the branch moves only while it still holds
the prior SHA. A `ready` request follows the Ready route below instead of
stopping at Draft.

The four bracketed MR metadata options are required only when no open MR
exists. The combined driver first completes and records the transfer, pushes
the exact SHA, then searches by project and source branch. If creation is
needed and metadata is absent, it returns `missing-data` with `missing` plus
the completed `import` and `push` facts. Supply the named fields in a new
signed request for the same branch and commit; the transfer is safely reused,
and the find-before-create check prevents a duplicate. When an MR already
exists, omitted metadata is not requested and its Draft/Ready state is not
changed.

## 6. Submit and let Lenovo publish; read back the exact SHA and MR

Submit through `scripts/dell_lan/dell_managed_submit.py` with the request,
`requestSha256`, `commandFile`, `receiptScriptPath`, and `--cwd
<expectedCwd>` (contract: `docs/dell-managed-submit.md`).

The footprint is the request's task worktree *and* the checkout whose ref
store the route writes; job and reservation publish together. A digest,
schema, path, revision, or command-binding failure refuses before any queue
write (exit `1`, `command-unbound`); a conflicting footprint refuses with exit
`2`. `--cwd` must be the request's `lenovoCheckout` (exit `2`, `cwd-unbound`)
and becomes the job's `--cwd` and signed expectation.

Completion: one `JOBID=<32hex>` is printed as soon as the job is published,
with a `dell-lan-managed-submit/1` receipt (`outcome: reserved`) naming the
job and its reservation (generation, resource digest, claims, revisions,
owner). An unreadable proof keeps that `JOBID`, exits `3`, and writes
`outcome: unknown` with `resubmit: forbidden`: re-read that id and never
resubmit the footprint (the slot stays held until a proof-bound release). A
submit without a claim keeps the legacy sequential fallback.

For a metadata stop, do not treat the non-zero result as if transfer failed:
retain its `import.worktree`, `import.branch`, `import.commit`, and
`push.observedRemoteSha`, then resume only with the missing fields. For a
timeout or exit-3 unknown, use the same-id reconciliation below before any
new writer; never guess whether MR creation landed.

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

### Draft MR description: exact full-text round-trip

The request binds the exact UTF-8 description bytes, and live readback binds
only when the publication driver proves that the complete served description
matches them. A direct identity capture has no such proof and cannot establish
live publication. See
[`scripts/dell_lan/references/description-proof.md`](../scripts/dell_lan/references/description-proof.md)
for the writer, reconciliation, and failure contracts.

## 6b. Ready route: consented undraft with readback, no pipeline wait

A `ready` request is the same publish with one extra consented step.
A `draft` request is unchanged and never runs the undraft step, so Draft stays
the silent default and is never lifted by accident.

With consent, the driver runs, in order: push → Draft MR create/update
(never a second MR for one branch; every update converges to the complete
signed description with `replace_description=True`) →
`remove_draft.py --mr-url <url> --host <host> --project <project>` from
`--lenovo-flow-dir` → identity re-capture. The
result then proves three things: the re-captured head SHA still equals the
task commit, the MR is no longer Draft, and the served title and
description still match the request input byte-for-byte — the Ready title
built the same way the writer builds the Draft title (without the
`Draft: ` prefix), and the description file bytes (UTF-8). Exact equality
is required on the Ready path because the request supplies the intended
description under `mr`+`undraft` consent.
Any mismatch, or any undraft failure, is `unknown` (exit 3,
`readback-mismatch` / `undraft-unknown`): the write may have landed, so the
run reconciles through the read-only probe instead of retrying blind, and a
later publish updates the same MR.

The driver never waits for or polls a pipeline on any path: no pipeline
observer is imported or executed, and every publish result carries
`pipeline: {waited: false, polled: false}` as the visible fact. A pipeline
read, when the user explicitly asks for one, stays a separate
`docs/pipeline-observer.md` run and is never part of publication.

## 6c. Executor truth and receipt gate (issues #33/#36)

- Executor: `powershell_truth.py` runs the supported
  `powershell -Command <script>` dialect through a valid encoded outer script,
  parses the inner text as data inside `try`, and preserves native exit.
  Exit code alone decides the envelope; `stderr` never does.
- Other PowerShell shapes fail closed with non-zero `status: error`: file or
  encoded launch, unsafe quoting, and nested cmd forms such as `%ComSpec% /c`,
  `cmd /c start /wait`, `cmd /c if`, `call`, or `@powershell`. They are never
  run unwrapped; diagnostics omit command text. Ordinary cmd remains unchanged.
- Submit `--dialect` is signed; old jobs without it retain classification.
  Cmd inline quoting cannot turn
  a disguised PowerShell executable into an unwrapped launch, while an
  ordinary command merely mentioning PowerShell still runs normally.
- Receipt: `published` requires the structured driver receipt
  (`outcome: ok`, `stage: readback`, matching `identity.head_sha`), checked
  by `publish_receipt_gate.py` in the driver and Dell readback.
  Strict argv parsing binds it to the signed `receipt_script_path` by exact
  normalized path, not basename; `C:\attacker\lenovo_publish.py` and
  `C:\attacker\mr_identity.py` cannot forge accepted evidence.
  Mismatch or absent readback is not success; `unknown` stays exit 3
  with a same-id reconcile, never a blind retry and never a second MR.

## 7. Reconcile an unknown or rejected outcome before any retry

- **Unknown push** (`stage: push`, exit 3): keep the id and re-fetch it
  per `docs/lan-bridge.md` "Observation rule"; a
  late sealed result is accepted by MAC and id binding. If the slot stays
  empty, use the writer JSON's pre-rendered `reconcileCommand`, or rerun the
  same `dell_request.py publish` construction with the same explicit dialect
  and an additional `--reconcile`, then submit the regenerated command file.
  The builder places the flag inside the PowerShell script (or as one quoted
  cmd argument); never append text to an existing command file. The read-only
  probe observes the remote branch and MR list and writes nothing. It needs
  `reconcile` in the request's `--authorize`.
- **Unknown MR write** (`stage: mr`, exit 3): the write may have landed. The
  probe reports the existing MR only after its complete description exactly
  matches the signed request; mismatch is refused and unavailable evidence is
  unknown. The probe never writes or creates a second MR. Never re-drive blind.
  While an outcome is unknown, the slot stays occupied and no second writer
  starts.
- **Unknown undraft** (`stage: mr`, exit 3, `undraft-unknown` or
  `readback-mismatch`): the same rule as an unknown MR write — the undraft
  may have landed, so reconcile the same job id and let a later publish
  update the same MR. Never a second MR, never a blind retry.
- **Rejected** (exit 1): read the carried exit code and stderr, fix on the
  anchored base, and submit a new job id. The queue refuses to reuse an id.
- **Stale**: keep working offline on the anchored base and re-sync before the
  next transfer.

## Standalone transfer and publication

Two narrower runs reuse this loop: a transfer-only
import with no push or MR (#42), and the publication of a branch that
already exists on Lenovo with no bundle and no second GitLab client
(#43). The combined `publish` route composes the same import and publication
helpers and supports the metadata stop/resume described above (#45). All
three routes live in `docs/dell-lan-standalone.md`.

Preparing an already identified MR for Ready is independent of all three
publication routes. Use `docs/mr-ready.md`; that signed route accepts the MR
URL and existing checks, and does not transfer a branch or create an MR.

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
