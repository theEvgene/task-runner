# Queue-local reservations and capacity quotas

> Agent entry. Read this when a job declares an explicit managed-write
> footprint (`submit --claim`) or a shared `capacity:<name>` quota, or when
> a footprint is refused. Scenario docs stay authoritative for their own
> commands: `docs/lan-bridge.md` for the signed client and
> `docs/lenovo-worker-shortcut.md` for the worker and its guarantees.

One contract, one authority: the producer reserves an explicit footprint
before publication, and a worker admits a job in parallel only from that
verified reservation.

## One authoritative reservation per job

Parallel admission requires a signed `lenovo-job-reservation/1` envelope in
`queue/reservations/<job-id>.json`, bound to the exact job envelope
SHA-256, one owner and generation, an explicitly normalized resource set,
and a resource digest. The filename is never authority: a read re-derives
every binding and refuses a mismatch.

Resource identities are normalized paths plus one exclusive
`tcp:*:<port>` token per port, so a wildcard bind, `localhost`, and a
specific address naming that port are the same claim. A relative,
unparsable, or unknown-type claim refuses; an unprovable footprint is never
admitted. The worker re-derives every binding and adds its own normalized
working directory, so a payload cannot omit the directory it runs in.

The supported managed-write producer entry is
`lan_bridge.submit_job(..., resource_claims=[...])` (`submit --claim` on the
bridge CLI). A submit without claims keeps its legacy one-publish,
sequential behavior and creates no reservation infrastructure.

```text
python scripts/lan_bridge/lan_bridge.py submit \
  --queue <queue-dir> --key-file <key-path> \
  --command-file <cmd-file> --cwd <working-dir-on-worker> \
  --claim 'file:C:\out\report.csv' --claim 'tcp:127.0.0.1:8080'
```

Producer `resource_claims` / `resource_scope` inside the job payload remain
self-attestation: without a verified reservation the job receives the
`exclusive:global` fallback and runs sequentially. Unmodelled side effects
outside the declared footprint remain the producer's documented boundary
(#88): only explicit managed write footprints are admitted in parallel.

## The producer referee

The referee (`scripts/lenovo_worker/job_reservations.py`) holds one
queue-local operating-system lock while it refuses the same or nested
footprint from a different job and publishes the reservation plus the job,
so two producers cannot both admit one footprint. The operating system owns
that lock and drops it when its holder exits, so elapsed age never breaks a
live lock and a contender never steals one.

A refused attempt publishes nothing, and a failed job publish removes only
its own provisional reservation. An already published job stays `owned` on
re-submit even after the quota file changed, because that decision is made
before the quota authority is read: a retry publishes nothing and is never
re-admitted. A new footprint still fails closed on a broken authority.

## Capacity quotas

Explicitly configured `capacity:<name>` claims are shared quotas, never
exclusive owners. One queue-local file, `queue/capacity-quotas.json`
(`{"cpu": 2}`, at most 8 names, each quota 1-256), is the only authority.
It is read under the same referee lock by every producer, so no producer
can raise a ceiling another producer admitted under. A capacity claim with
no entry, an exhausted quota, or an unreadable, oversized, or malformed
file refuses before publication, and the refusal names the limiting
resource and its limit.

A caller-supplied `capacities=` mapping may only confirm the file, name by
name; a different value refuses. Configured names go through the same case
fold as claims, so one name cannot be bounded under two spellings.

The quota is publish-time admission, not a runtime cap: the worker never
re-reads it, so lowering or deleting the file does not reduce the
concurrency of already-published jobs. Both the quota and the footprint
exclusion are scoped to one queue directory.

## Lifetime: only a proof-bound release frees a footprint

Admission alone does not free anything. A reservation is live until
`queue/releases/<job-id>.json` proves this exact job, resource set, claim
generation, and result bytes reached a terminal result. A signed result
alone, a missing job (the producer aborted under the lock, so the record is
inert), age, or a timeout never frees a footprint.

## Admission limits

The production-compatible default is still one active job;
`--max-active 2` is an upper bound that only the reservation proof can fill,
so two reserved independent jobs run together and a third waits.
