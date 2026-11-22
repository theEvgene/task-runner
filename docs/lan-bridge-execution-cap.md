# Execution cap (600 s from spawn)

One Lenovo command runs at most 600 seconds measured from actual spawn,
plus the bounded tree-stop settle (`OwnedProcess.SETTLE_SECONDS`, 3 s).
A smaller `submit --timeout` is preserved exactly; anything larger is
refused for new submissions (`timeout_seconds` valid range is 1..600).
A producer that derives a job budget from a smaller declared budget caps the
derived value at 600 too: the selected check submits its `timeoutSeconds` plus
a 60 s stop window, so 540 s and 541..600 s both submit exactly 600 s.
Already-signed jobs carrying up to the legacy 3600 are not refused and
never run longer: the worker clamps them to 600 at prepare time and the
lifecycle clamps again at wait time, so the bound holds even for jobs
signed before this cap. Queue waiting consumes none of the budget: the
timer starts at spawn, and `wait.py` reports the enforced 600 s
(`deadline.seconds`, with `requested_seconds` kept for transparency).
A confirmed tree kill seals terminal `timed_out` (`exit_code: 124`,
`termination: timeout`); a kill that cannot be proven seals nothing and
keeps the claim (`owned`/`unknown`, same-id resume, never a second
submit). Parallelism is unchanged: at most two actives, admitted only by
reservation proof.

Same-id recovery binds the exact signed job, never a resubmission. A job
signed before this cap carries the pre-cap timeout (for the selected check,
`timeoutSeconds + 60`, i.e. 601..660 for a 541..600 s check); the worker never
runs it past 600 s and the reader accepts it as that exact job. Any other
timeout, or a different command, working directory, output bound or dialect,
is refused before anything is published.
