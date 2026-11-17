# LAN bridge delay diagnosis

Owner default #12/#13 (240 s / 30 s) stays the code default for fixture and
short-probe use. The 600 s / 30 s live watch in `docs/lan-bridge.md`
("Observation rule") is task-level live policy explicitly scoped below that
owner default, not a replacement: no script default changes here. The
numbers below describe the fixture mechanism only.

Dominant delay is worker execution plus queue wait, both outside this
client: an already-present result costs exactly one queue read and returns
sub-second (asserted in `LatencyDiagnosisTests`). Per empty poll the client
spends one directory probe. The owner default (240 s / 30 s) therefore cuts
empty-poll probes ~3x versus the previous 660 s / 10 s default for the same
wait (at most 10 reads per full window versus 67), at the price of a 30 s
detection-latency bound instead of 10 s -- an explicit trade-off; a shorter
wait can still be chosen per call with `--poll`. No deeper client-side cut
was made: slicing the 30 s sleep into hidden 1 s probes would silently
redefine the owner-chosen check interval, so it stays out of scope under
#12/#13. Protocol, MAC, id binding, immutability and key handling are
unchanged.

Measured this round (local fixture only: window 0.4 s / poll 0.05 s, late
result sealed after two empty windows; reproduce with
`python -m unittest discover -s tests/lan_bridge -p
test_lan_bridge_get_result.py`, probe counting in `BeforeAfterTests`):

| metric                              | before: re-invoked `fetch` per window (2 calls) | after: one `get_result`  |
| ----------------------------------- | ----------------------------------------------- | ------------------------ |
| queue-dir resolves                  | 2 (one per call)                                | 1 (validated once)       |
| queue reads to the same late result | 10                                              | 19                       |
| elapsed                             | 0.42 s                                          | 0.82 s                   |
| already-present result              | 1 read, sub-second                              | 1 read, 0.008 s observed |

Reads and elapsed scale with when the late result arrives in both paths, so
the before/after gap there is fixture arrival time, not mechanism speed, and
is reported as such rather than as an optimization win. The deterministic
saving is the per-window setup: one validation + one queue resolve per
`get_result` however many windows it observes, versus one per `fetch` call.
Dominant delay stays worker-side (execution + queue wait): an
already-present result costs exactly one read in both paths. Stage splits
(`stage_timings`) still use only confirmed numeric worker marks -- opaque
protocol strings yield `unknown` for every stage, and `queue_s`/`delivery_s`
have no confirmed marks at all, so they stay `unknown` here. No live second
host is proven by any of these numbers.
