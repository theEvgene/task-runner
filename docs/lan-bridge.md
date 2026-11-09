# Signed LAN command bridge

Agent-operable signed route for one command to a second host over a shared
queue directory. The command travels inside the HMAC-signed job payload (not
as a correlation id); the result is verified by MAC and bound to the requested
job id. The queue is immutable: a published id is never replaced.

Canonical client: `scripts/lan_bridge/lan_bridge.py` (stdlib only; protocol
v1, no crypto change from the reviewed MVP revision). Nothing here reimplements
the envelope, the MAC, or the queue; scenario docs (`docs/fresh-base.md`,
`docs/pipeline-observer.md`, `docs/dell-lan.md`) build their exact commands
and call this client.

## Required inputs (owner-provided, never invented)

Queue dir plus a 32-byte key file (raw bytes, or base64 text with
`--key-format base64`). Both live outside every repository, outside the queue
itself, and outside any shared LAN folder. The worker on the other host is
already running and was started by its owner.

Paths interpolated into a signed command must not contain `"`, `$`,
backtick, or `%` (PowerShell/cmd expansion); spaces are fine.

## Exact commands

All paths below are placeholders.

1. Submit one command (prints `JOBID=<32hex>`):

```text
python scripts/lan_bridge/lan_bridge.py submit \
  --queue <queue-dir> --key-file <key-path> \
  [--key-format raw|base64] \
  --command-file <cmd-file> --cwd <working-dir-on-worker> \
  --timeout <seconds> --max-output <bytes> \
  [--return-file <path-on-worker> ...]
```

Exit `2` refuses without executing: empty command, bad key, key inside the
queue or a repository, duplicate id, unreadable command file.

2. Fetch the bound result:

```text
python scripts/lan_bridge/lan_bridge.py fetch \
  --queue <queue-dir> --key-file <key-path> \
  [--key-format raw|base64] \
  --id <job-id> --timeout <seconds> [--poll <seconds>] \
  [--receipt-out <path>]
```

Prints the result payload JSON only after its MAC verifies **and** its
`kind`/`id` equal `result`/`<job-id>` (binding before trust: a valid envelope
for another id never verifies as this one). Exit `0` verified, `2`
verification/usage failure, `3` bounded-wait timeout with the slot left
untouched (fetch the SAME id again; a retry is a NEW id, never a re-publish).

## Boundaries

- One command per job, one working directory, one timeout, bounded
  stdout/stderr, declared return files. The bridge never uploads repository
  content by itself; jobs carry commands, and small result files ride back
  only when declared with `--return-file`.
- The key travels through `--key-file` only. It is never printed and never
  lands in any receipt.
- A missing key, a key inside the queue or a repository, an empty command, a
  malformed envelope, a MAC mismatch, or an id/kind mismatch all refuse
  without execution and without a second route.
- No live second host is proven by shipping this client: the fixture suite
  below is local only. A live run is a separate, explicitly authorized step.

## Local verification

```text
<python> -m unittest discover -s tests/lan_bridge -v
```

Covers the published interop vector byte-for-byte, MAC tamper and wrong-id
refusal, command-in-payload transmission, result id binding,
duplicate-publish refusal, key handling, and a fixture submit/worker/fetch
round trip (the fake worker seals a canned result; the managed-command bytes
in that test are opaque example data). No second host is ever reached.
