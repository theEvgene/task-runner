"""Publish receipt gate: success only on a confirmed structured receipt.

``published`` means all three hold: the Lenovo driver envelope says
``outcome == "ok"``, its ``stage == "readback"``, and the carried
identity ``head_sha`` equals the expected task commit. Everything else
is not success: a driver ``unknown`` (or a timeout/truncation that
hides the verdict) stays ``unknown`` with exit 3 and reconciles the
SAME job id, never a blind retry and never a second MR.

Diagnostics are secret-free: reasons name the gap (mismatch, absent,
unavailable, unknown), never key material or full command text.
"""

from __future__ import annotations

import re
import ntpath
from pathlib import PureWindowsPath

PUBLISH_RESULT_SCHEMA = "dell-lan-publish-result/1"
_PYTHON = re.compile(r"^(?:python(?:3(?:\.\d+)?)?|py)(?:\.exe)?$", re.IGNORECASE)
_POWERSHELL_LAUNCHER = re.compile(
    r"^(powershell(?:\.exe)?|pwsh(?:\.exe)?)\s+"
    r"-NoProfile\s+-NonInteractive\s+-Command\s+\"([^\"]+)\"\s*$",
    re.IGNORECASE,
)


def _command_argv(command: str) -> list[str] | None:
    """Parse the queue's restricted Windows command line, or fail closed.

    The request writer emits only whitespace-separated, double-quoted argv.
    Shell operators and unterminated quotes are not part of that dialect.
    """

    if not isinstance(command, str) or not command.strip():
        return None
    command = command.strip()
    argv: list[str] = []
    token: list[str] = []
    quoted = False
    active = False
    i = 0
    while i < len(command):
        char = command[i]
        if char in "\r\n&|<>^" and not quoted:
            return None
        if char in " \t" and not quoted:
            if active:
                argv.append("".join(token))
                token, active = [], False
            i += 1
            continue
        if char == '"':
            quoted = not quoted
            active = True
            i += 1
            continue
        # command_line() doubles a backslash run before a literal quote.
        if char == "\\":
            start = i
            while i < len(command) and command[i] == "\\":
                i += 1
            count = i - start
            if i < len(command) and command[i] == '"':
                token.extend("\\" * (count // 2))
                if count % 2:
                    token.append('"')
                    i += 1
                else:
                    quoted = not quoted
                    i += 1
                active = True
            else:
                token.extend("\\" * count)
                active = True
            continue
        token.append(char)
        active = True
        i += 1
    if quoted:
        return None
    if active:
        argv.append("".join(token))
    return argv


def _powershell_command_argv(command: str) -> list[str] | None:
    """Parse the restricted PowerShell argv emitted by ``_ps_quote``.

    The supported shape is ``& 'arg' 'arg'`` with embedded single quotes
    doubled.  No expressions, substitutions, pipelines, or bare arguments
    are accepted.
    """

    if not isinstance(command, str):
        return None
    text = command.strip()
    if not text.startswith("&") or (len(text) > 1 and not text[1].isspace()):
        return None
    i = 1
    argv: list[str] = []
    while i < len(text):
        while i < len(text) and text[i].isspace():
            i += 1
        if i == len(text):
            break
        if text[i] != "'":
            return None
        i += 1
        token: list[str] = []
        while i < len(text):
            if text[i] != "'":
                token.append(text[i])
                i += 1
                continue
            if i + 1 < len(text) and text[i + 1] == "'":
                token.append("'")
                i += 2
                continue
            i += 1
            break
        else:
            return None
        if i < len(text) and not text[i].isspace():
            return None
        argv.append("".join(token))
    return argv or None


def _wrapped_powershell_command_argv(command: str) -> list[str] | None:
    """Parse the supported explicit launcher and its literal inner argv."""

    if not isinstance(command, str):
        return None
    matched = _POWERSHELL_LAUNCHER.fullmatch(command.strip())
    if matched is None:
        return None
    return _powershell_command_argv(matched.group(2))


def documented_command_kind(command: str,
                            intended_script_path: object = None) -> str | None:
    """Return the receipt contract bound to the signed exact script path."""

    if not isinstance(intended_script_path, str) or not intended_script_path:
        return None
    intended = ntpath.normcase(ntpath.normpath(intended_script_path))
    candidates = (
        _command_argv(command),
        _powershell_command_argv(command),
        _wrapped_powershell_command_argv(command),
    )
    argv = next((candidate for candidate in candidates
                 if candidate and len(candidate) >= 2
                 and _PYTHON.fullmatch(PureWindowsPath(candidate[0]).name)
                 and ntpath.normcase(ntpath.normpath(candidate[1])) == intended),
                None)
    if argv is None:
        return None
    script = PureWindowsPath(intended_script_path).name.lower()
    if script == "lenovo_publish.py":
        return "publish-driver"
    if script == "mr_identity.py" and len(argv) > 2 and argv[2] == "capture":
        return "direct-identity"
    if script == "mr_ready_run.py":
        return "mr-ready-runner"
    return None


def _norm_sha(value: object) -> str:
    return str(value or "").strip().lower()


def identity_head(identity: object) -> str:
    """The bound head SHA of an ``mr_identity`` receipt, or ``""``."""

    if not isinstance(identity, dict):
        return ""
    return _norm_sha(identity.get("head_sha"))


def is_published(driver_result: dict, expected_commit: str) -> bool:
    """True only for ``ok`` + ``readback`` + matching ``head_sha``."""

    if not isinstance(driver_result, dict):
        return False
    if driver_result.get("schema") != PUBLISH_RESULT_SCHEMA:
        return False
    if driver_result.get("outcome") != "ok":
        return False
    if driver_result.get("stage") != "readback":
        return False
    expected = _norm_sha(expected_commit)
    if not expected:
        return False
    identity = driver_result.get("identity")
    return identity_head(identity) == expected


def classify_driver(driver_result: dict, expected_commit: str) -> tuple[str, int, str]:
    """Map a driver envelope to ``(verdict, exit_code, reason)``.

    ``published`` (0) only via :func:`is_published`; ``unknown`` (3)
    for ``outcome == "unknown"`` with a same-id reconcile action;
    everything else is ``refused``/``mismatch`` (1), never silent ok.
    """

    if not isinstance(driver_result, dict):
        return ("mismatch", 1, "publication-readback-malformed")
    outcome = driver_result.get("outcome")
    if outcome == "unknown":
        return ("unknown", 3, "outcome-unknown")
    if is_published(driver_result, expected_commit):
        return ("published", 0, "published")
    if outcome not in ("ok", "observed"):
        return ("rejected", 1, "publication-job-failed")
    identity = driver_result.get("identity")
    if identity is None:
        return ("mismatch", 1, "publication-absent")
    if identity_head(identity) != _norm_sha(expected_commit):
        return ("mismatch", 1, "publication-head-mismatch")
    return ("mismatch", 1, "publication-readback-malformed")


def reconcile_action(reason: str) -> str:
    """The fail-closed next action for an unknown or rejected outcome."""

    if reason == "outcome-unknown":
        return "keep the slot, re-fetch the SAME job id; do not resubmit or open a second MR"
    return "read the carried exit code and stderr, fix on the anchored base, then a new job id"
