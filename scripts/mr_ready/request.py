#!/usr/bin/env python3
"""Dell side: build one signed-queue command for an existing MR Ready run.

The generated command invokes the existing personal-agent-toolkit
``mr_ready_run.py`` on Lenovo.  This adapter performs no GitLab access, branch
transfer, Draft creation, or queue write; the standard signed bridge submits
the command file separately.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from pipeline_observer.gate import GateInputError, canonical_url  # noqa: E402
from pipeline_observer.request import (  # noqa: E402
    _absolute,
    _ps_quote,
    _shell_safe,
)

SCHEMA = "mr-ready-request/1"
POWERSHELL_DIALECT = "powershell"
POWERSHELL_LAUNCHER = "powershell.exe -NoProfile -NonInteractive -Command"


def _launcher_safe(value: str, label: str) -> str:
    """Validate one literal argv value for the supported launcher shape."""

    _shell_safe(value, label)
    if '"' in value:
        raise GateInputError(
            f"{label} must not contain a double quote in the PowerShell launcher")
    return value


def build_command(
    python: str,
    client_dir: str,
    checkout: str,
    mr_url: str,
    change_type: str,
    key: str,
    subject: str,
    scopes: list[str],
    *,
    description_file: str | None = None,
    replace_description: bool = False,
    allow_test_results: bool = False,
    profile: str | None = None,
    previous_head: str | None = None,
    max_retries: int = 2,
    wait_seconds: float = 0.0,
) -> str:
    runner = str(Path(client_dir) / "mr_ready_run.py")
    parts = [python, runner, "--url", mr_url, "--repo", checkout,
             "--type", change_type, "--key", key, "--subject", subject]
    for scope in scopes:
        parts += ["--scope", scope]
    if description_file:
        parts += ["--description-file", description_file]
    if replace_description:
        parts.append("--replace-description")
    parts.append("--allow-test-results" if allow_test_results
                 else "--personal-no-test-results")
    if profile:
        parts += ["--profile", profile]
    if previous_head:
        parts += ["--previous-head", previous_head]
    parts += ["--max-retries", str(max_retries),
              "--wait-seconds", str(wait_seconds)]
    inner = "& " + " ".join(_ps_quote(part) for part in parts)
    return f'{POWERSHELL_LAUNCHER} "{inner}"'


def build_request(
    mr_url: str,
    python: str,
    client_dir: str,
    checkout: str,
    change_type: str,
    key: str,
    subject: str,
    scopes: list[str],
    *,
    description_file: str | None = None,
    replace_description: bool = False,
    allow_test_results: bool = False,
    profile: str | None = None,
    previous_head: str | None = None,
    max_retries: int = 2,
    wait_seconds: float = 0.0,
    timeout_seconds: int = 1800,
    max_output_bytes: int = 262144,
) -> tuple[dict, str]:
    canonical = canonical_url(mr_url)
    if change_type not in ("feat", "fix"):
        raise GateInputError("--type must be feat or fix")
    if not key or not subject:
        raise GateInputError("--key and --subject are required")
    if not scopes or any(not scope for scope in scopes):
        raise GateInputError("at least one non-empty --scope is required")
    if not 0 <= max_retries <= 2:
        raise GateInputError("--max-retries must be within 0..2")
    if not 0 <= wait_seconds <= 3600:
        raise GateInputError("--wait-seconds must be within 0..3600")
    if not 1 <= timeout_seconds <= 3600:
        raise GateInputError("--job-timeout must be within 1..3600")
    if not 1 <= max_output_bytes <= 262144:
        raise GateInputError("--job-max-output must be within 1..262144")

    for value, label in ((python, "--python"),
                         (client_dir, "--lenovo-client-dir"),
                         (checkout, "--lenovo-checkout")):
        _absolute(value, label)
        _launcher_safe(value, label)
    runner = Path(client_dir) / "mr_ready_run.py"
    _launcher_safe(str(runner), "mr_ready_run.py path")
    for value, label in ((key, "--key"), (subject, "--subject")):
        _launcher_safe(value, label)
    for scope in scopes:
        _launcher_safe(scope, "--scope")
    for value, label in ((description_file, "--description-file"),
                         (profile, "--profile")):
        if value is not None:
            _absolute(value, label)
            _launcher_safe(value, label)
    if previous_head is not None:
        if (len(previous_head) != 40 or
                any(char not in "0123456789abcdef" for char in previous_head)):
            raise GateInputError("--previous-head must be 40 lowercase hex")

    command = build_command(
        python, client_dir, checkout, canonical, change_type, key, subject,
        scopes, description_file=description_file,
        replace_description=replace_description,
        allow_test_results=allow_test_results, profile=profile,
        previous_head=previous_head, max_retries=max_retries,
        wait_seconds=wait_seconds)
    request = {
        "schema": SCHEMA,
        "mr_url": canonical,
        "action": "prepare-existing-mr-ready",
        "authorized": {"prepare_history": True, "ready_write": True,
                       "max_retries": max_retries},
        "forbidden": ["branch_transfer", "create_merge_request", "merge"],
        "metadata": {"type": change_type, "key": key, "subject": subject,
                     "scope": list(scopes)},
        "lenovo": {"python": python, "client_dir": client_dir,
                   "checkout": checkout, "profile": profile,
                   "description_file": description_file},
        "receipt_script_path": str(runner),
        "job": {"dialect": POWERSHELL_DIALECT,
                "timeout_seconds": timeout_seconds,
                "max_output_bytes": max_output_bytes},
        "command": command,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return request, command


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="request.py", description=__doc__)
    parser.add_argument("--mr-url", required=True)
    parser.add_argument("--python", required=True)
    parser.add_argument("--lenovo-client-dir", required=True)
    parser.add_argument("--lenovo-checkout", required=True)
    parser.add_argument("--type", choices=("feat", "fix"), required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--scope", action="append", required=True)
    parser.add_argument("--description-file")
    parser.add_argument("--replace-description", action="store_true")
    parser.add_argument("--allow-test-results", action="store_true")
    parser.add_argument("--profile")
    parser.add_argument("--previous-head")
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--wait-seconds", type=float, default=0.0)
    parser.add_argument("--job-timeout", type=int, default=1800)
    parser.add_argument("--job-max-output", type=int, default=262144)
    parser.add_argument("--request-file", type=Path, required=True)
    parser.add_argument("--command-file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        request, command = build_request(
            args.mr_url, args.python, args.lenovo_client_dir,
            args.lenovo_checkout, getattr(args, "type"), args.key,
            args.subject, args.scope, description_file=args.description_file,
            replace_description=args.replace_description,
            allow_test_results=args.allow_test_results, profile=args.profile,
            previous_head=args.previous_head, max_retries=args.max_retries,
            wait_seconds=args.wait_seconds, timeout_seconds=args.job_timeout,
            max_output_bytes=args.job_max_output)
    except GateInputError as error:
        parser.error(str(error))
    args.request_file.parent.mkdir(parents=True, exist_ok=True)
    args.request_file.write_text(
        json.dumps(request, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    args.command_file.parent.mkdir(parents=True, exist_ok=True)
    args.command_file.write_text(command + "\n", encoding="utf-8")
    digest = hashlib.sha256(json.dumps(
        request, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    print(f"request_sha256={digest}")
    print(f"command={command}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
