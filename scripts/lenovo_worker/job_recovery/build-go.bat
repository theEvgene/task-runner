@echo off
REM Build LenovoJobRecovery.exe with Go (stdlib only, no dependencies).
REM GOPROXY=off proves the build uses no network: only the local toolchain.
setlocal
where go >nul 2>nul
if errorlevel 1 (
  echo missing Go toolchain: install Go from https://go.dev/dl/ and retry 1>&2
  exit /b 2
)
set GOPROXY=off
set GOFLAGS=-mod=mod
go build -trimpath -o "%~dp0LenovoJobRecovery.exe" "%~dp0."
endlocal
