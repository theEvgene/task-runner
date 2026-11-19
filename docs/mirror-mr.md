# Mirror an open GitLab MR into a private GitHub PR (issue #142)

> Agent entry. Use ONLY this scenario when: you must review an **open**
> GitLab MR on this host, and the corporate-connected host (Lenovo) must
> observe its exact revisions and transfer the objects over the existing
> signed LAN route, so this host (Dell) can publish them to an explicitly
> configured **private** GitHub mirror and open the equivalent PR. Do NOT
> use it for: a merged MR's historical comparison (issue #143), the
> natural-language trigger (issue #144), a base-only transfer
> (`docs/fresh-base.md`), or a Dell-to-Lenovo publication
> (`docs/dell-lan.md`). No GitLab mutation, no merge, no review submission.

Dell reads GitLab metadata and objects on Lenovo; the import, the mirror
publication, and the PR all happen on Dell. Credentials never cross the
queue, the share, or any receipt: Lenovo authenticates to GitLab in its own
user context (the `gitlab_mr_reader` seam or `glab`), Dell authenticates to
GitHub in its own `gh` context, and the signed jobs carry SHAs and digests
only.

## 0. Configure the mirror mapping (owner-provided, never invented)

Write `mirror-mapping/1` JSON outside every checkout:

```json
{
  "version": "mirror-mapping/1",
  "mirrors": { "gitlab.example.com/group/project": "owner/private-mirror" }
}
```

Keys are `<gitlab-host>/<project path>`; values are `owner/repo`. An
ambiguous key, a missing project, or a credential-bearing value refuses
before anything is observed. The destination's **current private**
visibility is read back on Dell before every publication; a public mirror
refuses with `destination-not-private`.

## 1. Write the mirror request (Dell, files only)

```text
python scripts/mirror_mr/dell_mirror_request.py mirror ... (see --help)
```

Canonical form:

```text
python scripts/mirror_mr/dell_mirror_request.py \
  --mr-url https://<host>/<group/project>/-/merge_requests/<iid> \
  --mapping-file <abs-mapping.json> --token <16hex> \
  --shared <lan-share> --lenovo-checkout <abs-lenovo-checkout> \
  --python <abs-python-on-lenovo> \
  [--lenovo-flow-dir <abs-reader-seam-on-lenovo>] \
  --request-name mirror-request-<16hex>.json --command-file <command-file> \
  [--repo <dell-checkout>]
```

Validates the URL shape (https, no userinfo, `/<project>/-/
merge_requests/<iid>`), the mapping entry, and the shared-path layout.
Prints the sealed request facts plus the `--return-file` (the small export
manifest that also rides back in-band).

## 2. Submit through the existing signed client (Dell)

```text
python scripts/lan_bridge/lan_bridge.py submit \
  --queue <queue-dir> --key-file <key-path> \
  --command-file <command-file> --cwd <working-dir-on-worker> \
  --timeout <seconds> --max-output <bytes> \
  --return-file <shared>\mirror-mr-<16hex>.manifest.json
```

Prints `JOBID=<32hex>`. Only this client seals jobs; nothing here
reimplements the envelope, the MAC, or the queue (`docs/lan-bridge.md`).

## 3. Lenovo observes and exports (one signed job)

The submitted command runs `scripts/mirror_mr/lenovo_mr_export.py`:

1. re-hashes the request against the SHA-256 inside the sealed job;
2. reads the MR (status, title, description, source/target branches and
   exact SHAs) through `gitlab_mr_reader.read_open_mr` from
   `--flow-dir`, else the `glab` CLI; a non-open MR, a missing branch, or
   an unreadable MR refuses with an actionable reason;
3. binds both branches to remote revisions (`ls-remote` vs metadata; a
   move refuses as `mr-moved`);
4. fetches both revisions into token-scoped temp refs, bundles them with a
   `git-bundle-manifest/1` manifest, then re-reads the MR and refuses when
   it moved between observation and export;
5. prints the `mirror-mr-export/1` result (identity, SHAs, merge-base,
   ancestry, bundle digest).

No push, no MR write, no checkout move. A refusal removes the bundle and
manifest this run staged.

## 4. Import, publish, and read back (Dell)

```text
python scripts/mirror_mr/dell_mirror_import.py \
  --queue <queue-dir> --key-file <key-path> --job-id <job-id> \
  --shared <lan-share> --mirror-dir <abs-mirror-clone> \
  --mapping-file <abs-mapping.json> \
  --mr-url https://<host>/<group/project>/-/merge_requests/<iid> \
  --state <outside-mirror>\mirror-state-<16hex>.json \
  --receipt <outside-mirror>\mirror-receipt-<16hex>.json \
  [--github-dir <fixture-double-dir>] [--provenance fixture|live]
```

What it proves, in order: the sealed job parses to the exact
`lenovo_mr_export.py --request mirror-request-<16hex>.json --request-sha256
<64hex>` command with absolute paths (a substring marker never suffices;
same-id resume after any unknown outcome) and the sealed token and artifact
names match that request file; the bundle bytes, SHA-256,
manifest entry, and manifest digest match the seal; both SHAs resolve in
the mirror; the destination is currently `private`; the target ref is
created or fast-forwarded and the source ref is created or already equal
(anything else is `target-not-fast-forward` / `source-ref-conflict`,
never a force-push; a branch checked out in any worktree refuses as
`worktree-checked-out`, a dirty one as `worktree-dirty`); both refs push cleanly; the open PR for the head/base
pair is found or created exactly once (a same-name PR pointing elsewhere
is `pr-conflict`); the PR head SHA and PR base SHA, the mirror base head, and the
observed SHAs are all equal (`comparison: exact`, else `evidence-gap`
instead of success). The non-secret `--state` resumes an interrupted run
at `fetched` / `verified` / `refs-published` / `pr-created`.

Completion: receipt `mirror-mr-receipt/1` with `verdict: mirrored`
(or `reconciled` for a resumed run that adopted the existing PR), the PR
URL and number, and both SHAs. Exit `0` mirrored, `1` refused, `2` usage,
`3` unknown (keep the slot, re-run the same command with the same id).

## Recovery

- Unknown LAN outcome (exit 3, timeout, lost result): re-run the same
  import command with the same `--job-id` and `--state`. Never submit a
  second job for one request.
- `push-failed` after a partial push: re-run; the import reconciles the
  remote refs before pushing again (fast-forward SHAs only, never force).
- `pr-write-unknown`: re-run; the existing PR is adopted and verified,
  never duplicated.
- `mr-moved` on Lenovo: re-read the MR and submit a NEW request (new
  token, new job id); the queue never reuses an id.
- `target-not-fast-forward` / `source-ref-conflict` / `pr-conflict`:
  resolve the conflicting ref or PR by hand first.

## Trust boundary

- The queue key travels via `--key-file` only and is refused inside the
  queue, any checkout, or the share. No credential enters a request, a
  command file, a receipt, or the state file.
- The bundle is untrusted bytes until its SHA-256, size, manifest entry,
  and manifest digest all match the sealed result, and both SHAs resolve
  to commits in the mirror.
- The mirror publication is proven by `ls-remote` readback, the PR by
  `pr view` readback. A receipt marked `provenance: fixture` is never
  live evidence.
- Receipts embed the MR URL, project, mirror name, branch names, and SHAs.
  Strip mirror/project names before pasting into an audience not entitled
  to the remote; SHAs, verdicts, and digests carry no topology.

## Merged-MR historical comparison (issue #143)

The open route above refuses a merged MR (`mr-not-open` on Lenovo). For an
already merged MR whose change the default branch already contains, run the
historical route instead: it replays the original base..head change on
uniquely named review-only refs and opens one PR labelled as a review
copy. Transport, privacy, idempotency, and recovery guarantees are the
same as the open route; the destination's default branch is never moved
and this PR must never be merged.

Request with `--mode merged` (default stays `open`, which preserves the
open-route contract byte for byte):

```text
python scripts/mirror_mr/dell_mirror_request.py \
  --mr-url https://<host>/<group/project>/-/merge_requests/<iid> \
  --mapping-file <abs-mapping.json> --token <16hex> \
  --shared <lan-share> --lenovo-checkout <abs-lenovo-checkout> \
  --python <abs-python-on-lenovo> --mode merged \
  [--lenovo-flow-dir <abs-reader-seam>] \
  --request-name mirror-request-<16hex>.json --command-file <command-file>
```

The sealed job runs `scripts/mirror_mr/lenovo_mr_history_export.py`: it
reads the merged MR (`read_merged_mr` from `--flow-dir`, else the `glab`
fallback; an open MR refuses as `mr-not-merged` so the two routes cannot
be confused), retrieves the historical base/head objects plus any
recorded squash commit even when the source branch was deleted, and
exports them in one hashed bundle. The authoritative comparison is the
unified diff of the original head against its base plus the file list,
hashed as `authoritativeDigest`. A missing base, a missing head with no
retrievable object, or an empty comparison refuses as
`history-unavailable`. A refusal deletes the staged bundle and manifest.

Import with `scripts/mirror_mr/dell_mr_history_import.py` (same flags as
the open-route import, same `--state` resume and `--github-dir` seam):

- It binds the sealed job to the exact `lenovo_mr_history_export.py`
  command (an open-route job refuses as `queue-job-mismatch`), verifies
  bundle bytes, SHA-256, manifest entry, and manifest digest, and fetches
  every wanted SHA into the mirror.
- It recomputes `git diff base..head` (paths plus patch bytes) in the
  mirror and compares the digest and file list to `authoritativeDigest`.
  A recorded squash commit is recomputed the same way and is never
  assumed identical: the published representation is `original-head` when
  the head matches, `squash` only when the head does not match and the
  squash does, otherwise `evidence-gap` instead of success.
- It publishes `mirror-mr/<iid>/base-<short>` and
  `mirror-mr/<iid>/head-<short>` (deterministic per MR and history, so a
  rerun reuses them). An existing ref at another SHA is
  `history-ref-conflict`, never an overwrite; a checked-out branch
  refuses as `worktree-checked-out`. The default branch is untouched.
- The PR title is `[Review copy] MR !<iid> (merged): <subject>` and the
  body names the original MR URL, merged status, historical base/head
  SHAs, the squash SHA when recorded, the representation used, the review
  refs, the authoritative digest and file list, and the do-not-merge
  notice. Adoption and readback (PR state, head/base ref names and OIDs,
  mirror refs, recomputed diff) mirror the open route; a mismatch is
  `pr-conflict` or `evidence-gap`, never a second PR.

Completion: receipt `mirror-mr-history-receipt/1` with `verdict: mirrored`
(or `reconciled`), `representation: original-head|squash`,
`historicalBase`, `historicalHead`, `authoritativeDigest`,
`comparison: exact`, PR URL/number, head/base refs, and `ancestry`.
Exit `0` mirrored, `1` refused, `2` usage, `3` unknown (same id resumes;
`pr-write-unknown` after a partial write reconciles the persisted PR).

Limitations: history the connected host can no longer retrieve (pruned
objects, an unreadable MR, or no retrievable equivalent) stops without
publishing; a squash that cannot be proven equivalent is reported, not
used; the route never moves the default branch, never force-pushes, and
never merges the review PR.

## Single entry (issue #144)

`python scripts/mirror_mr/mirror_mr.py` is the one command for both routes.
It validates the MR URL and mapping, submits one signed job per attempt,
polls the SAME id through the existing import driver, and prints the
verified PR link. It adds no transport or PR logic.

```text
python scripts/mirror_mr/mirror_mr.py --mr-url <url> \
  --mapping-file <abs-mapping> --shared <lan-share> \
  --mirror-dir <abs-mirror-clone> --queue <queue-dir> \
  --key-file <key-path> --lenovo-checkout <abs-lenovo-checkout> \
  --python <abs-lenovo-python> --state <outside-mirror-state> \
  --receipt <outside-mirror-receipt> [--mode auto|open|merged]
```

`--mode auto` (default) determines live status through the signed export:
open completes the open route, merged retries once as history with a NEW
job id. `--mode open|merged` pins one route. Discover queue/key/Python
per `docs/lan-bridge-config.md`; never invent paths or credentials. A
non-private mirror, ambiguous mapping, or unreadable input refuses before
any job; an unknown outcome (exit `3`) reruns the identical command with
the identical `--state` and never submits a replacement. Success prints
the receipt JSON (PR URL, MR status, SHAs/digest) and exits `0`; the PR is
for review only and is never merged.

## Local verification

```text
python -m unittest discover -s tests/mirror_mr -v
```

Disposable fixtures only: two local bare remotes (GitLab, GitHub), the
real signed queue, fixture reader/doubles at the documented seams
(`gitlab_mr_reader`, `github_ops`); no network, no live services. Covers
the open-MR path, identity/digest verification, repeated invocation,
stale/moved refs, public destination, non-fast-forward/conflicting refs,
interrupted writes, and unknown same-ID jobs, plus the merged-MR history
path: deleted source ref, squash/non-squash histories, a differing
squash, missing objects, idempotent rerun, conflicting history refs,
interrupted writes, unknown same-ID jobs, and open-route job isolation.
