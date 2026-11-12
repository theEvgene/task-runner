# First verified import of a corporate repository on Dell via Lenovo

> Agent entry. Use ONLY this scenario when: the corporate Git remote is
> reachable only from Lenovo, and Dell needs the first local copy of a
> repository that has never been prepared on either side. It gives you one
> signed job that exports the named remote branch on Lenovo, and one verified
> import into a new directory on Dell. Do NOT use it for: base-only work from
> an existing Lenovo checkout (that is `docs/fresh-base.md`), the two-direction
> publish loop (that is `docs/dell-lan.md`), or any push, MR, dependency, or
> network setup.

Dell never contacts the corporate remote; only Lenovo reads it. Content
crosses on the owner-provided LAN share that already hosts the queue;
authenticity travels inside the HMAC-signed job payload as SHA-256 values.

Canonical drivers: `scripts/dell_lan/` (stdlib only). Tests:
`<python> -m unittest discover -s tests/dell_lan -p "test_dell_first*.py"`
(disposable fixtures: one local bare Git repository, the real signed queue,
the real Lenovo export driver; no network).

## When to choose this scenario

- `docs/fresh-base.md` needs a Lenovo checkout of the same repository.
  This scenario needs none: the remote URL and the branch are named
  explicitly and Lenovo observes them into an isolated temp bare repo.
- `docs/dell-lan.md` publishes a task commit back. This scenario never
  publishes: the imported revision is not a freshness guarantee for a
  future push (re-probe the tip before pushing, as that loop describes).

## Preconditions (owner-provided, never guessed)

- queue directory and a 32-byte key file, both outside every repository,
  the queue, and the shared LAN folder; the Lenovo worker is already running;
- one LAN share both machines read and write — the same share that hosts
  the queue, not a new mount;
- an existing Dell checkout used only as the key-custody anchor
  (`--repo`): it is never written;
- the current verified copy of the drivers on each machine
  (`scripts/dell_lan/` here, the Lenovo driver dir there);
- working remote credentials on Lenovo in the Lenovo user context.
  No credential ever travels the queue or the share: Dell holds only the
  queue key, and the signed jobs carry digests, never secrets.

If any item is missing, stop and name that item. Do not substitute another
channel, another machine, or a remembered path.

## 1. Write the first-import request

```text
python scripts/dell_lan/dell_request.py first \
  --shared <lan-share> --token <16hex> \
  --lenovo-skill-dir <abs-dir-on-lenovo> \
  --lenovo-driver-dir <abs-dell-lan-dir-on-lenovo> \
  --python <abs-python-on-lenovo> \
  --remote-url <remote-url-or-path-on-lenovo> --ref-full refs/heads/<branch> \
  --request-name first-request-<16hex>.json --command-file <command-file> \
  --repo <dell-anchor-checkout>
```

`--lenovo-driver-dir` is the `scripts/dell_lan` directory of this
repository on Lenovo; omit it only when
`--lenovo-skill-dir/scripts` already holds the drivers (legacy checkout
layout). `--repo` names an existing Dell checkout that anchors the key
custody check; the imported copy is created elsewhere.

## 2. Submit through the existing signed client

```text
python scripts/lan_bridge/lan_bridge.py submit \
  --queue <queue-dir> --key-file <key-path> \
  --command-file <command-file> --cwd <working-dir-on-lenovo> \
  --timeout 300 --max-output 65536 \
  --return-file <lan-share>\dell-first-<16hex>.manifest.json
```

Completion: submit printed one `JOBID=<32hex>`. Save the id before
waiting: while the outcome is unknown, the slot stays occupied and the
same id is re-fetched — a second submit would be a NEW id, never a retry
(the queue refuses duplicates with `JobExistsError`).
`--return-file` declares the small manifest so a MAC-bound copy also
comes back in-band (`docs/lan-bridge.md`).

## 3. Receive, verify, and import the first copy exactly once

```text
python scripts/dell_lan/dell_first_import.py \
  --queue <queue-dir> --key-file <key-path> --job-id <job-id> \
  --shared <lan-share> --repo <dell-anchor-checkout> \
  --repo-out <new-copy-dir> --remote-url <same-remote-url> \
  --branch <branch> [--expected-project <group/project>] \
  [--bridge-dir <lan_bridge-scripts-dir>] \
  --provenance <fixture|live> \
  --expected-lenovo-driver-sha256 <64hex> \
  --expected-dell-driver-sha256 <64hex> \
  --receipt <outside-every-checkout>\first-import.json
```

The importer verifies the sealed result (MAC, kind/id, exit, timeout,
truncation), requires the exact documented `lenovo_first_export.py` command
shape (no substring match, no extra arguments), requires the sealed
`driverVersion` to equal the Dell-side `FIRST_IMPORT_DRIVER_VERSION`, checks
the token-bound artifact names, re-hashes the bundle against the
SHA-256, byte count, and tip commit inside the result, re-reads the
`git-bundle-manifest/1` digest, confirms the sealed remote URL and branch
equal `--remote-url`/`--branch`, binds both executing drivers to the
operator's pinned digests (no pin means no import), and fetches the bundle
once into a path that must be empty. `--bridge-dir` defaults to this
repository's `scripts/lan_bridge` (override with `DELL_LAN_BRIDGE_DIR`).
Both `--expected-lenovo-driver-sha256` and
`--expected-dell-driver-sha256` are required trust inputs: the SHA-256 of
the trusted `lenovo_first_export.py` and `dell_first_import.py` copies.
A missing or malformed pin is refused before any copy (exit `2`, no
receipt-derived success); a sealed or executing digest that differs is
`driver-stale` (exit `1`, nothing created).

Completion: receipt `dell-first-import/1` with `verdict: imported`,
`baseSha`, and `repo.head` equal to `baseSha`. A second import into the
same path is refused (`repo-exists`): one first copy, never replaced.

## 4. Recover an unknown outcome before any retry

- Bounded wait expired (exit `3`) or an interrupted wait: re-fetch the
  SAME job id (`lan_bridge.py fetch ... --id <same-job-id>` or
  `scripts/lan_bridge/get.py ... --id <same-job-id>` with windows).
  A late result sealed under the original id verifies normally.
- Worker failure (exit `1`, `verdict: rejected`): read the carried exit
  code and stderr, fix on the Lenovo side, and submit a NEW job id with a
  fresh token. Never re-drive blind while the outcome is unknown.

## Outcomes

- `0 imported` (receipt `dell-first-import/1` binds remote URL, branch,
  verified SHA, and copy HEAD);
- `1` refused/mismatch (nothing created; a pre-existing or occupied
  destination is left untouched; a file or link at the destination is
  refused as `destination-is-file`, a non-empty directory as `repo-exists`);
- `2` usage;
- `3` unknown — slot stays occupied, re-fetch the SAME job id.

## Driver freshness (enforceable, fail-closed)

The sealed job must parse to exactly
`<python> lenovo_first_export.py --request <first-request-<token>.json> --request-sha256 <64hex>`
(anything else — including a command that merely contains the driver name as
a substring, or carries extra arguments — is `queue-job-mismatch` and proves
nothing). The sealed Lenovo result must name
`driverVersion: "1"` (`FIRST_IMPORT_DRIVER_VERSION` in
`scripts/dell_lan/dell_lan_common.py`); a missing or older revision is
`driver-stale` and no copy is created. The result also carries the SHA-256
of the Lenovo driver file that actually ran (`driverSha256`), computed by
that driver from its own `__file__` at execution time.

A self-reported version and digest do not prove either checkout is current:
they only fail closed against known-stale drivers. Freshness is established
only by comparing the actual executing bytes against trusted identities the
operator supplies out-of-band:

- Trust inputs: `--expected-lenovo-driver-sha256` is the SHA-256 of the
  trusted `lenovo_first_export.py` copy on Lenovo;
  `--expected-dell-driver-sha256` is the SHA-256 of the trusted
  `dell_first_import.py` copy on Dell. The operator computes both from the
  current verified checkout on each machine (for example with `sha256sum`
  over `scripts/dell_lan/`) and passes them on the Dell command line. They
  never travel the queue, the share, or a receipt as authority — they are
  the authority the sealed and executing digests are checked against.
- Binding actual execution: on Lenovo, `lenovo_first_export.py` hashes its
  own file at runtime into the sealed `driverSha256`; the sealed envelope
  (MAC, kind/id) plus the exact command-shape check above ties that digest
  to this job. On Dell, `dell_first_import.py` hashes its own file at
  runtime and compares it to `--expected-dell-driver-sha256` before any
  queue read or copy. Either side failing the comparison is `driver-stale`:
  nothing is created.
- Fail closed if missing: both pins are required CLI arguments. A missing
  or malformed pin is refused (exit `2` at usage, or `driver-stale` with a
  receipt when reached past parsing); a wrong or stale digest is
  `driver-stale` (exit `1`). There is no `unverified` success path: the
  `imported` verdict is emitted only when the sealed Lenovo digest equals
  its pin and the executing Dell digest equals its pin, recorded as
  `driver.freshness: "verified-pinned"` and
  `dellDriver.freshness: "verified-pinned"`.
- A receipt marked `provenance: fixture` is never live evidence either way,
  even when both digests verify.

## Boundaries

- One first copy, imported once at an empty path, never replaced; one
  writer; no second queue and no second transport.
- No push, MR, dependency install, or network setup; no modification of
  any existing checkout, foreign job slot, or file. Lenovo writes no
  checkout at all: the export runs in an isolated temp bare repo that is
  removed afterwards, so uncommitted changes nowhere are overwritten.
- The Lenovo exporter claims its token-bound bundle and manifest names
  exclusively: a pre-existing bundle or manifest file — including a foreign
  one — is refused with `artifact-exists` instead of replaced, and a
  concurrent writer that wins the name between the pre-check and the create
  loses the same way (atomic link-or-exclusive-open commit; a refused run
  removes only artifacts it created itself). A remote URL starting with
  `-` is refused, and every `ls-remote`/`fetch` passes `--` before the
  remote so it can never parse as a Git option.
- The key travels via `--key-file` only, and every driver refuses a key
  it cannot prove is outside the anchor checkout, the queue, and the
  shared folder. Remote credentials live only in the Lenovo user context;
  they are never written to a request, a command file, or a receipt.
- Driver currency: the importer only accepts a sealed result from the exact
  `lenovo_first_export.py` command above whose `driverVersion` equals the
  Dell-side `FIRST_IMPORT_DRIVER_VERSION`, whose sealed `driverSha256`
  equals `--expected-lenovo-driver-sha256`, and whose own executing
  `dell_first_import.py` digest equals `--expected-dell-driver-sha256`
  (see "Driver freshness"). Run both sides from the current verified
  checkout; an unconfirmed driver copy is never evidence of success, and a
  self-reported version or digest without a matching pin never reads as
  verified.
- A receipt marked `provenance: fixture` is never live evidence.

## Sharing receipts outside the loop

Receipts embed the remote URL (`remoteUrl`, `origin.url`) plus branch,
SHAs, and capture times. Before pasting a receipt into an issue, chat, or
public report, keep those URL fields only when the audience is entitled
to the remote. SHAs, verdicts, reasons, and digests carry no topology and
are safe to quote.

## Relation to the other scenarios

- `docs/fresh-base.md` stages the same bundle mechanics for base-only
  work when a Lenovo checkout already exists.
- `docs/dell-lan.md` is the full two-direction loop once local work is
  ready to publish.
- `docs/pipeline-observer.md` is the read-only pipeline check for one MR
  and one SHA.
