"""Bind one sealed command to its digest-bound request and owning driver.

The Dell managed submit accepts a command file only when it parses to exactly
one invocation of the one driver that owns the request schema, carrying this
request's ``--request`` path and ``--request-sha256`` and no other statement or
argument. A substring marker is never enough: parse the command, never match
its text. The PowerShell dialect must carry the transport's explicit
``-Command`` launcher whose script is one ``&`` call of that argv.

This is the command half of ``dell_managed_submit``; the request, footprint and
receipt halves stay there. It refuses through the same ``Stop`` and imports
nothing from its caller, so the entry module keeps one direction of import.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

from dell_lan_common import DETAIL_MAX, PUBLISH_REQUEST_SCHEMA, Stop
from dell_standalone_common import (
    PUBLISH_EXISTING_REQUEST_SCHEMA, TRANSFER_REQUEST_SCHEMA,
)

#: One schema, one driver that owns its footprint.
DRIVERS = {
    PUBLISH_REQUEST_SCHEMA: "lenovo_publish.py",
    TRANSFER_REQUEST_SCHEMA: "lenovo_transfer.py",
    PUBLISH_EXISTING_REQUEST_SCHEMA: "lenovo_publish_existing.py",
}
MANAGED_SCHEMAS = frozenset(DRIVERS)
PYTHON_NAMES = frozenset({"python", "python.exe", "python3", "python3.exe"})
LAUNCHER_NAMES = frozenset({"powershell", "powershell.exe", "pwsh", "pwsh.exe"})
LAUNCHER_FLAGS = frozenset({"-noprofile", "-noninteractive", "-nologo"})
#: No generated argument carries these: such a token is a second statement.
SHELL_META = ("%", "!", "^", "&", "|", "<", ">", "(", ")", ";", '"', "\n",
              "\r", "\x00")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_COMMAND_BYTES = 1024 * 1024


def same_path(left: object, right: object) -> bool:
    """Compare two Lenovo paths by spelling; links are resolved on Lenovo only."""

    try:
        first, second = Path(str(left)), Path(str(right))
        if not first.is_absolute() or not second.is_absolute():
            return False
        return (os.path.normcase(os.path.normpath(str(first)))
                == os.path.normcase(os.path.normpath(str(second))))
    except (OSError, ValueError, RuntimeError):
        return False


def _split(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError as error:
        raise Stop("command-unbound", detail=str(error)[:DETAIL_MAX]) from error


def _refuse(detail: str) -> Stop:
    return Stop("command-unbound", detail=detail[:DETAIL_MAX])


def _extra_args(tail: list[str]) -> None:
    """Only the documented flow arguments may follow the bound request pair."""

    if tail[:1] == ["--flow-dir"]:
        if len(tail) < 2 or not Path(tail[1]).is_absolute():
            raise _refuse("--flow-dir needs one absolute path")
        tail = tail[2:]
    if tail and tail != ["--reconcile"]:
        raise _refuse("unexpected extra arguments after the bound request pair")


def driver_argv(command: str, dialect: str) -> list[str]:
    """The single driver invocation the sealed command runs, or a refusal.

    Mirrors ``verify_first_job``: parse, never match a substring. One absolute
    python interpreter, one absolute driver, and the exact ``--request`` /
    ``--request-sha256`` pair. The PowerShell dialect must carry the
    transport's explicit ``-Command`` launcher whose script is one ``&`` call
    of that argv; any second statement, operator, or trailing argument refuses
    before publication.
    """

    argv = _split(command)
    if dialect == "powershell":
        name = (argv[0].replace("/", "\\").rsplit("\\", 1)[-1].lower()
                if argv else "")
        if name not in LAUNCHER_NAMES:
            raise _refuse("--dialect powershell needs the explicit "
                          "powershell -Command launcher")
        if len(argv) < 3 or argv[-2].lower() != "-command":
            raise _refuse("the launcher must end with one -Command script")
        if not {token.lower() for token in argv[1:-2]} <= LAUNCHER_FLAGS:
            raise _refuse("unsupported powershell launcher flag")
        script = _split(argv[-1])
        if not script or script[0] != "&":
            raise _refuse("the -Command script must be one '&' call")
        argv = script[1:]
    elif dialect != "cmd":
        raise _refuse("explicit dialect required: cmd|powershell")
    for token in argv:
        if not token or any(char in token for char in SHELL_META):
            raise _refuse("the command carries a shell operator or an empty "
                          "argument")
    if len(argv) < 6:
        raise _refuse("required: <python> <driver> --request <path> "
                      "--request-sha256 <digest>")
    python, driver, flag_request, request_path, flag_digest, digest = argv[:6]
    if (Path(python).name.lower() not in PYTHON_NAMES
            or not Path(python).is_absolute()):
        raise _refuse("the command must start with one absolute python "
                      "interpreter")
    if not Path(driver).is_absolute():
        raise _refuse("the driver path must be absolute")
    if flag_request != "--request" or flag_digest != "--request-sha256":
        raise _refuse("the command must pass --request and --request-sha256")
    if DIGEST_RE.fullmatch(digest.lower()) is None:
        raise _refuse("the command must name a 64-hex request sha256")
    _extra_args(argv[6:])
    return [python, driver, request_path, digest]


def bind_command(command: str, request_path: Path, request_digest: str,
                 schema: str, dialect: str) -> None:
    """The sealed command must be exactly this request's one driver invocation."""

    if not isinstance(command, str) or not command.strip():
        raise Stop("command-unreadable", detail="command file is empty")
    if len(command.encode("utf-8")) > MAX_COMMAND_BYTES:
        raise Stop("command-unreadable", detail="command file is too large")
    _, driver, named_request, digest = driver_argv(command, dialect)
    expected = DRIVERS.get(schema)
    if expected is None or Path(driver).name.lower() != expected:
        raise Stop("command-unbound", detail=(
            f"the command must run {expected} for this request schema"))
    if not same_path(named_request, request_path):
        raise Stop("command-unbound", detail=(
            "the command must name this exact request file"))
    if digest.lower() != request_digest:
        raise Stop("command-unbound", detail=(
            "the command must name this request's sha256"))
