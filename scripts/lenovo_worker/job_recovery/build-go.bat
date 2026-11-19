@echo off
REM Deterministic build for LenovoJobRecovery.exe (stdlib only, no deps).
REM Provenance: Go 1.24.7 exactly, windows/amd64, -trimpath -buildvcs=false,
REM CGO_ENABLED=0, GOTOOLCHAIN=local, GOPROXY=off. Same sources + same
REM settings yield a byte-identical exe (verified via clean-checkout rebuild).
REM Recipe and expected SHA-256: docs/lenovo-job-recovery.md. Rebuilding with
REM any other toolchain or flag set yields a different exe: do not commit it
REM as the blessed binary.
setlocal
REM Build from the script directory: go directory arguments resolve in the
REM current module, so a caller cwd anywhere else would fail module lookup.
cd /d "%~dp0"
where go >nul 2>nul
if errorlevel 1 (
  echo missing Go toolchain: install Go 1.24.7 from https://go.dev/dl/ and retry 1>&2
  exit /b 2
)
set GOTOOLCHAIN=local
set GOPROXY=off
set GOFLAGS=-mod=mod
set GOOS=windows
set GOARCH=amd64
set GOAMD64=v1
set CGO_ENABLED=0
for /f "tokens=3" %%v in ('go version') do set GOVERSION=%%v
if not "%GOVERSION%"=="go1.24.7" (
  echo refusing: require Go go1.24.7, found %GOVERSION% 1>&2
  exit /b 2
)
go build -trimpath -buildvcs=false -o "%~dp0LenovoJobRecovery.exe" "%~dp0."
if errorlevel 1 exit /b 1
go version
certutil -hashfile "%~dp0LenovoJobRecovery.exe" SHA256
endlocal
