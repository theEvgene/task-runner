#!/usr/bin/env python3
"""Dell side: build the signed-queue job for the Lenovo pipeline probe.

Writes two files and nothing else:

- a request file (schema ``pipeline-observer-request/1``) naming the
  canonical MR URL, the expected source SHA, and the required-check
  profile;
- a command file holding exactly one Lenovo command: one invocation of
  ``probe.py`` with absolute interpreter and script paths.

The caller submits the command file with the existing signed bridge
client and reads the result back with ``readback.py``. This writer
performs no network access, never touches the queue, and never handles
the queue key.

Fail-closed: the MR URL must parse, the SHA must be 40 lowercase hex,
and the Lenovo interpreter, flow directory, and checkout must be
absolute paths. Command parts are PowerShell single-quoted (embedded
apostrophes doubled), so `$`, backticks, and double quotes stay
literal; line breaks are refused. ``--lenovo-client-dir`` optionally
names the existing gitlab-mr-flow scripts directory on Lenovo and is
forwarded as probe ``--client-dir``.
Read-only: this route never authorizes a push or an
MR write.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from gate import GateInputError, canonical_url
    from probe import SHA_RE
except ImportError:  # package-style invocation from the repo root
    from pipeline_observer.gate import (  # type: ignore[no-redef]
        GateInputError, canonical_url)
    from pipeline_observer.probe import SHA_RE  # type: ignore[no-redef]

SCHEMA = "pipeline-observer-request/1"


def _absolute(value: str, label: str) -> str:
    if not value or not Path(value).is_absolute():
        raise GateInputError(f"{label} must be an absolute path")
    return value


def _shell_safe(value: str, label: str) -> str:
    # The command file holds exactly one line executed through
    # PowerShell on the worker side. Parts are single-quoted below
    # (apostrophes doubled), which keeps every character literal except
    # a line break, so only line breaks are refused here fail-closed.
    if "\n" in value or "\r" in value:
        raise GateInputError(f"{label} must not contain line breaks")
    return value


def _ps_quote(part: str) -> str:
    # PowerShell single-quoted string: fully literal, no $ or backtick
    # expansion; an embedded apostrophe is written doubled.
    return "'" + part.replace("'", "''") + "'"


def build_command(python: str, flow_dir: str, mr_url: str,
                  profile: str, sha: str,
                  client_dir: str | None = None) -> str:
    # Leading `&` is the PowerShell call operator: without it a line
    # starting with a quoted path is a syntax error instead of an
    # invocation. The line is executed with `powershell -Command`.
    script = str(Path(flow_dir) / "probe.py")
    parts = [python, script, "--url", mr_url, "--profile",
             profile, "--expected-sha", sha]
    if client_dir:
        parts += ["--client-dir", client_dir]
    return "& " + " ".join(_ps_quote(part) for part in parts)


def build_request(mr_url: str, expected_sha: str, profile: str,
                  python: str, flow_dir: str, checkout: str,
                  timeout_seconds: int = 300,
                  max_output_bytes: int = 65536,
                  client_dir: str | None = None) -> tuple[dict, str]:
    want = (expected_sha or "").lower()
    if not SHA_RE.fullmatch(want):
        raise GateInputError("expected-sha must be 40 lowercase hex")
    canonical = canonical_url(mr_url)
    _absolute(python, "--python")
    _absolute(flow_dir, "--lenovo-flow-dir")
    _absolute(checkout, "--lenovo-checkout")
    _shell_safe(python, "--python")
    _shell_safe(flow_dir, "--lenovo-flow-dir")
    _shell_safe(checkout, "--lenovo-checkout")
    _shell_safe(profile, "--profile")
    if client_dir is not None:
        _absolute(client_dir, "--lenovo-client-dir")
        _shell_safe(client_dir, "--lenovo-client-dir")
    if not profile:
        raise GateInputError("--profile is required")
    if not 1 <= timeout_seconds <= 3600:
        raise GateInputError("--job-timeout must be within 1..3600")
    if not 1 <= max_output_bytes <= 262144:
        raise GateInputError("--job-max-output must be within 1..262144")
    command = build_command(python, flow_dir, canonical, profile, want,
                            client_dir)
    request = {
        "schema": SCHEMA,
        "mr_url": canonical,
        "expected_sha": want,
        "profile": profile,
        "lenovo": {"python": python, "flow_dir": flow_dir,
                   "checkout": checkout,
                   "client_dir": client_dir},
        "job": {"timeout_seconds": timeout_seconds,
                "max_output_bytes": max_output_bytes},
        "command": command,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return request, command


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="request.py", description=__doc__)
    parser.add_argument("--mr-url", required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--python", required=True)
    parser.add_argument("--lenovo-flow-dir", required=True)
    parser.add_argument("--lenovo-checkout", required=True)
    parser.add_argument("--lenovo-client-dir", default=None,
                        help="existing gitlab-mr-flow scripts directory "
                             "on Lenovo (forwarded as probe --client-dir)")
    parser.add_argument("--job-timeout", type=int, default=300)
    parser.add_argument("--job-max-output", type=int, default=65536)
    parser.add_argument("--request-file", type=Path, required=True)
    parser.add_argument("--command-file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        request, command = build_request(
            args.mr_url, args.expected_sha, args.profile, args.python,
            args.lenovo_flow_dir, args.lenovo_checkout, args.job_timeout,
            args.job_max_output, args.lenovo_client_dir)
    except GateInputError as error:
        parser.error(str(error))
    args.request_file.parent.mkdir(parents=True, exist_ok=True)
    args.request_file.write_text(
        json.dumps(request, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    args.command_file.parent.mkdir(parents=True, exist_ok=True)
    args.command_file.write_text(command + "\n", encoding="utf-8")
    digest = hashlib.sha256(
        json.dumps(request, ensure_ascii=False,
                   separators=(",", ":")).encode("utf-8")).hexdigest()
    print(f"request_sha256={digest}")
    print(f"command={command}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
