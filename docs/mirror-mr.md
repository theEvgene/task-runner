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

What it proves, in order: the sealed result is MAC-bound to this job id
(same-id resume after any unknown outcome); the bundle bytes, SHA-256,
manifest entry, and manifest digest match the seal; both SHAs resolve in
the mirror; the destination is currently `private`; the target ref is
created or fast-forwarded and the source ref is created or already equal
(anything else is `target-not-fast-forward` / `source-ref-conflict`,
never a force-push; a dirty checked-out branch refuses as
`worktree-dirty`); both refs push cleanly; the open PR for the head/base
pair is found or created exactly once (a same-name PR pointing elsewhere
is `pr-conflict`); the PR head SHA, the mirror base head, and the
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

## Local verification

```text
python -m unittest discover -s tests/mirror_mr -v
```

Disposable fixtures only: two local bare remotes (GitLab, GitHub), the
real signed queue, fixture reader/doubles at the documented seams
(`gitlab_mr_reader`, `github_ops`); no network, no live services. Covers
the open-MR path, identity/digest verification, repeated invocation,
stale/moved refs, public destination, non-fast-forward/conflicting refs,
interrupted writes, and unknown same-ID jobs.
