"""Validation and construction of the authenticated LAN job payload."""

from __future__ import annotations

import re
from pathlib import PureWindowsPath

_HEX32 = re.compile(r"^[0-9a-f]{32}$")


def build_job_payload(job_id: str, command: str, working_directory: str,
                      timeout_seconds: int = 300, max_output_bytes: int = 65536,
                      return_files: list | None = None, dialect: str | None = None,
                      receipt_script_path: str | None = None) -> dict:
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
    return payload
