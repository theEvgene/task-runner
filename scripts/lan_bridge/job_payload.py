"""Validation and construction of the authenticated LAN job payload."""

from __future__ import annotations

import hashlib
import re
from pathlib import PureWindowsPath

_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_NOT_COMPARABLE = (".", "..")


def canonical_windows_path(value: object) -> str | None:
    """Return a canonical case-folded Windows path, or None when unusable.

    Only an absolute path without ``.``/``..`` segments is comparable: a
    relative or ambiguous path can never prove which directory it names.
    """

    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        path = PureWindowsPath(text)
    except (TypeError, ValueError):
        return None
    if not path.is_absolute():
        return None
    anchor = path.anchor.rstrip("\\/")
    if not anchor:
        return None
    rest = [part for part in path.parts[1:] if part not in ("\\", "/")]
    if any(part in _NOT_COMPARABLE for part in rest):
        return None
    return "\\".join([anchor.casefold(), *(part.casefold() for part in rest)])


def path_fingerprint(value: str) -> str:
    """Short path identity for a signed refusal: never the path text itself."""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def target_decision(expected: object,
                    actual: object) -> tuple[str, str, str]:
    """Compare a declared expected working directory with the job's own.

    Returns ``(state, expected_id, actual_id)``. ``state`` is ``verified``
    only when both paths are comparable and equal, ``cwd-mismatch`` only when
    both are comparable and differ, and ``unverified-target`` in every other
    case: an absent or unprovable expected/actual path is never reported as a
    match and never as a mismatch.
    """

    expected_path = canonical_windows_path(expected)
    actual_path = canonical_windows_path(actual)
    expected_id = path_fingerprint(expected_path) if expected_path else ""
    actual_id = path_fingerprint(actual_path) if actual_path else ""
    if expected_path is None or actual_path is None:
        return "unverified-target", expected_id, actual_id
    if expected_path == actual_path:
        return "verified", expected_id, actual_id
    return "cwd-mismatch", expected_id, actual_id


def build_job_payload(job_id: str, command: str, working_directory: str,
                      timeout_seconds: int = 300, max_output_bytes: int = 65536,
                      return_files: list | None = None, dialect: str | None = None,
                      receipt_script_path: str | None = None,
                      expected_working_directory: str | None = None) -> dict:
    if not isinstance(job_id, str) or _HEX32.fullmatch(job_id) is None:
        raise ValueError("job_id must be 32 lowercase hex")
    if not isinstance(command, str) or not command.strip():
        raise ValueError("command must be a nonempty string")
    if not isinstance(working_directory, str) or not working_directory:
        raise ValueError("working_directory is required")
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int)
            or not 1 <= timeout_seconds <= 3600):
        raise ValueError("timeout_seconds out of range")
    if (isinstance(max_output_bytes, bool) or not isinstance(max_output_bytes, int)
            or not 1 <= max_output_bytes <= 262144):
        raise ValueError("max_output_bytes out of range")
    if return_files is None:
        return_files = []
    if not isinstance(return_files, list):
        raise ValueError("return_files must be list")
    if dialect is not None and (not isinstance(dialect, str) or not dialect):
        raise ValueError("dialect must be a nonempty string or null")
    if receipt_script_path is not None and (
            not isinstance(receipt_script_path, str)
            or not receipt_script_path.strip()
            or not PureWindowsPath(receipt_script_path).is_absolute()):
        raise ValueError("receipt_script_path must be an absolute path or null")
    if expected_working_directory is not None and (
            not isinstance(expected_working_directory, str)
            or not expected_working_directory.strip()
            or not PureWindowsPath(expected_working_directory).is_absolute()):
        raise ValueError(
            "expected_working_directory must be an absolute path or null")
    payload = {
        "kind": "job", "id": job_id, "command": command,
        "working_directory": working_directory,
        "timeout_seconds": timeout_seconds,
        "max_output_bytes": max_output_bytes,
        "return_files": list(return_files),
    }
    if dialect is not None:
        payload["dialect"] = dialect
    if receipt_script_path is not None:
        payload["receipt_script_path"] = receipt_script_path
    if expected_working_directory is not None:
        payload["expected_working_directory"] = expected_working_directory
    return payload
