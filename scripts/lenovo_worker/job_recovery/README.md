# Lenovo job recovery tool sources (Go)

Evidence-gated recovery for one retained Lenovo queue job
(`LenovoJobRecovery.exe`). Operator doc: `docs/lenovo-job-recovery.md`.
Behavior tests: `tests/lenovo_worker/test_job_recovery_exe.py`.

Build with Go (stdlib only, no dependencies, no network): run
`build-go.bat` (`GOPROXY=off`, `-trimpath`). Record the exact
`go version` in the task report. Sources are split so each file stays
under the file token guard; all compile into the single exe.
Rebuild-from-source is exact; byte-identical output across machines is
not guaranteed (the PE embeds a build id/timestamp).
