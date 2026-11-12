"""Dell side: build the signed-queue job for the Lenovo release scenario.

Writes two files and nothing else:

- a request file (schema ``release-request/1``) naming the mode
  (``preview`` without a version, ``versioned`` with one), the Lenovo
  interpreter, entrypoint, and checkout;
- a command file holding exactly one Lenovo line in the proven cmd
  pipe shape (``cmd /c "set NODE_OPTIONS=--use-system-ca && ...``).

The caller submits the command file with the existing signed bridge
client and observes the SAME job id with ``get.py``. This writer
performs no network access, never touches the queue, and never handles
the queue key.

Thin by design: ``release-tag`` itself owns preview text,
confirmation, version handling, existing-tag, pipeline, and deploy
decisions. This driver adds no version semantics, no ``--yes``/``--force``
invention, and no second state machine.

Fail-closed: Lenovo paths must be absolute; version/confirm must be
single-line; ``--version`` always requires ``--confirm`` carrying the
exact user-typed text shown after the preview (a versioned line without
a pipe would hang on the interactive prompt). All interpolated values
must not contain ``"``, ``$``, backtick, or ``%`` (bridge path rule,
``docs/lan-bridge.md``) and must not contain cmd metacharacters
``|``, ``&``, ``<``, ``>``, ``^``, ``!`` (they would execute inside
``cmd /c``); the driver double-quotes paths and the version for ``cmd``.
``--confirm`` additionally accepts only an explicitly confirmed safe
answer (``yes``/``y``, case-insensitive); anything else refuses
and no release line is built.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


class ReleaseInputError(ValueError):
    """Bad input; fail closed before any file is written."""


SCHEMA = "release-request/1"


def _absolute(value: str, label: str) -> str:
    if not value or not Path(value).is_absolute():
        raise ReleaseInputError(f"{label} must be an absolute path")
    return value


def _single_line(value: str, label: str) -> str:
    if not value or not value.strip():
        raise ReleaseInputError(f"{label} must not be empty")
    if "\n" in value or "\r" in value:
        raise ReleaseInputError(f"{label} must not contain line breaks")
    return value


_BRIDGE_FORBIDDEN = ('"', "$", "`", "%")

# cmd control characters: unquoted (version tail, echo text) they split
# or chain commands; quoted (paths, version) "|" "&" "<" ">" stay
# literal inside double quotes but are still refused fail-closed, and
# "^" "!" keep an effect even inside quotes (escape / delayed
# expansion). "(" ")" are NOT refused: legal in Windows paths such as
# "Program Files (x86)" and literal inside double quotes.
_CMD_FORBIDDEN = ("|", "&", "<", ">", "^", "!")


def _no_bridge_chars(value: str, label: str) -> str:
    for char in _BRIDGE_FORBIDDEN:
        if char in value:
            raise ReleaseInputError(
                f"{label} must not contain {char!r} (bridge path rule)")
    return value


def _no_cmd_chars(value: str, label: str) -> str:
    for char in _CMD_FORBIDDEN:
        if char in value:
            raise ReleaseInputError(
                f"{label} must not contain {char!r} (cmd metacharacter)")
    return value


# Creating-release gate: only an explicitly confirmed safe answer may
# build a versioned line. Anything else (including the negative answers
# release-tag itself understands, e.g. "no"/"n"/"exit") refuses here so
# no release is launched. No version semantics is added: the version
# itself stays opaque to this driver.
_CONFIRM_ALLOW = frozenset({"yes", "y"})


def _checked_confirm(value: str) -> str:
    _single_line(value, "--confirm")
    _no_bridge_chars(value, "--confirm")
    _no_cmd_chars(value, "--confirm")
    canonical = value.strip().lower()
    if canonical not in _CONFIRM_ALLOW:
        raise ReleaseInputError(
            "--confirm must be an explicit confirmation (yes/y)")
    return canonical


def _cmd_quote(part: str) -> str:
    # cmd double-quoted token; '"' is refused before quoting so the
    # wrapping stays intact. Spaces are fine.
    return '"' + part + '"'


def build_command(node: str, entrypoint: str,
                  version: str | None = None,
                  confirm: str | None = None) -> str:
    _absolute(node, "--node")
    _absolute(entrypoint, "--release-tag")
    for label, val in (("--node", node), ("--release-tag", entrypoint)):
        if "\n" in val or "\r" in val:
            raise ReleaseInputError(f"{label} must not contain line breaks")
        _no_bridge_chars(val, label)
        _no_cmd_chars(val, label)
    if version is None and confirm is None:
        base = " && ".join([
            "set NODE_OPTIONS=--use-system-ca",
            f"{_cmd_quote(node)} {_cmd_quote(entrypoint)}",
        ])
        return f'cmd /c "{base}"'
    if version is None:
        raise ReleaseInputError("--confirm requires --version")
    _single_line(version, "--version")
    _no_bridge_chars(version, "--version")
    _no_cmd_chars(version, "--version")
    if confirm is None:
        # A versioned line without piped confirmation would hang on
        # release-tag's interactive prompt until the worker timeout.
        raise ReleaseInputError("--version requires --confirm")
    canonical = _checked_confirm(confirm)
    # Proven pipe shape: cmd echo piped into the interactive tool
    # (cf. owner-reported `echo yes | release-tag 1.38.0`; receipt absent
    # locally, see docs/release.md). No flag is invented: release-tag
    # reads its own prompt; anything else fails closed instead of
    # matching. The version is double-quoted so spaces stay one argv
    # token; '"' is refused before quoting so the wrapping stays intact.
    # confirm is an allowlisted alpha-only token, safe unquoted for echo.
    base = " && ".join([
        "set NODE_OPTIONS=--use-system-ca",
        f"echo {canonical} | {_cmd_quote(node)} "
        f"{_cmd_quote(entrypoint)} {_cmd_quote(version)}",
    ])
    return f'cmd /c "{base}"'


def build_request(node: str, entrypoint: str, checkout: str,
                  version: str | None = None,
                  confirm: str | None = None,
                  timeout_seconds: int = 300,
                  max_output_bytes: int = 65536) -> tuple[dict, str]:
    _absolute(checkout, "--lenovo-checkout")
    if "\n" in checkout or "\r" in checkout:
        raise ReleaseInputError("--lenovo-checkout must not contain line breaks")
    _no_bridge_chars(checkout, "--lenovo-checkout")
    _no_cmd_chars(checkout, "--lenovo-checkout")
    if not 1 <= timeout_seconds <= 3600:
        raise ReleaseInputError("--job-timeout must be within 1..3600")
    if not 1 <= max_output_bytes <= 262144:
        raise ReleaseInputError("--job-max-output must be within 1..262144")
    command = build_command(node, entrypoint, version, confirm)
    mode = "versioned" if version is not None else "preview"
    request = {
        "schema": SCHEMA,
        "mode": mode,
        "version": version,
        "confirmed": confirm is not None,
        "lenovo": {"node": node, "release_tag": entrypoint,
                   "checkout": checkout},
        "job": {"timeout_seconds": timeout_seconds,
                "max_output_bytes": max_output_bytes},
        "command": command,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return request, command


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="request.py", description=__doc__)
    parser.add_argument("--node", required=True,
                        help="absolute Lenovo Node interpreter path")
    parser.add_argument("--release-tag", required=True,
                        help="absolute owner-provided release-tag entrypoint on Lenovo")
    parser.add_argument("--lenovo-checkout", required=True,
                        help="absolute Lenovo checkout (submit --cwd)")
    parser.add_argument("--version", default=None,
                        help="release version; omit for preview, requires --confirm")
    parser.add_argument("--confirm", default=None,
                        help="exact user-typed confirmation; required with --version")
    parser.add_argument("--job-timeout", type=int, default=300)
    parser.add_argument("--job-max-output", type=int, default=65536)
    parser.add_argument("--request-file", type=Path, required=True)
    parser.add_argument("--command-file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        request, command = build_request(
            args.node, args.release_tag, args.lenovo_checkout,
            args.version, args.confirm,
            args.job_timeout, args.job_max_output)
    except ReleaseInputError as error:
        parser.error(str(error))
    args.request_file.parent.mkdir(parents=True, exist_ok=True)
    args.request_file.write_text(
        json.dumps(request, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    args.command_file.parent.mkdir(parents=True, exist_ok=True)
    # Single line, UTF-8, no BOM, one trailing newline.
    args.command_file.write_text(command + "\n", encoding="utf-8")
    digest = hashlib.sha256(
        json.dumps(request, ensure_ascii=False,
                   separators=(",", ":")).encode("utf-8")).hexdigest()
    print(f"request_sha256={digest}")
    print(f"command={command}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
