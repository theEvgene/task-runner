# Lenovo job recovery tool sources (Go)

Evidence-gated recovery for one retained Lenovo queue job
(`LenovoJobRecovery.exe`). Operator doc: `docs/lenovo-job-recovery.md`.
Behavior tests: `tests/lenovo_worker/test_job_recovery_exe.py`.

Build with Go 1.24.7 exactly (stdlib only, no dependencies, no network):
run `build-go.bat`. It pins `GOTOOLCHAIN=local`, `GOPROXY=off`,
`GOOS=windows`, `GOARCH=amd64`, `GOAMD64=v1`, `CGO_ENABLED=0` and
`go build -trimpath -buildvcs=false`, refuses any other toolchain, and prints
the exe SHA-256. Same sources + same settings yield a byte-identical exe
(verified by rebuilding from a clean checkout). Record the exact
`go version` and SHA-256 in the task report. Sources are split so each file
stays under the file token guard; all compile into the single exe.
