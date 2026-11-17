"""Descriptor model for one host-bound selected check (stdlib only).

The descriptor is owner-provided JSON outside every checkout.  Validation is
strict: an unexpected field refuses, so a typo never silently changes the
check.  There is exactly one argv and one host; no list of checks exists.
"""

from __future__ import annotations

import json
from pathlib import Path

SCHEMA = "lan-selected-check/1"
RECEIPT_SCHEMA = "lan-selected-check-receipt/1"
PROBE_SCHEMA = "lenovo-selected-check/1"
HOSTS = ("dell", "lenovo")
NOT_RUN_EXIT = 78
INCOMPLETE_EXIT = 79
MAX_TIMEOUT_SECONDS = 600
MAX_COMMAND_CHARS = 7000
NOT_RUN_REASONS = ("cwd-missing", "tool-missing", "revision-stale",
                   "revision-unconfirmed", "command-unavailable")
NEXT_ACTIONS = {
    "cwd-missing": "fix the selected check working directory on the target host",
    "tool-missing": "install or select the required executable on the target host",
    "revision-stale": "check out the expected revision on the target host",
    "revision-unconfirmed": "make the target checkout revision readable",
    "command-unavailable": "install or select the check executable on the target host",
    "command-refused": "fix the selected check command for the target transport",
    "queue-missing": "restore the owner-provided queue mount",
    "key-unusable": "restore the owner-provided key outside the queue and checkout",
    "python-unproven": "use the Lenovo python already proven by readiness evidence",
    "cwd-unproven": "bind the descriptor cwd to a confirmed readiness directory component",
    "tool-unproven": "bind the descriptor tool to a confirmed readiness tool component",
    "evidence-unbound": "re-run the existing readiness check and use its new job id",
    "worker-unproven": "run the existing readiness check for this Lenovo worker first",
    "worker-refused": "fix the target host selected-check environment",
}


class Refusal(Exception):
    pass


def _strict(obj: dict, allowed: tuple[str, ...], where: str) -> None:
    extra = sorted(set(obj) - set(allowed))
    if extra:
        raise Refusal(f"{where} has an unexpected field {extra[0]!r}")


def _string(obj: dict, name: str, where: str) -> str:
    value = obj.get(name)
    if not isinstance(value, str) or not value.strip():
        raise Refusal(f"{where} field {name!r} is required")
    return value


def _integer(obj: dict, name: str, where: str, default: int,
             low: int, high: int) -> int:
    value = obj.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise Refusal(f"{where} field {name!r} must be an integer in {low}..{high}")
    return value


def _hex(value: object, name: str, where: str, length: int) -> str:
    if (not isinstance(value, str) or len(value) != length
            or any(char not in "0123456789abcdef" for char in value)):
        raise Refusal(f"{where} field {name!r} must be {length} lowercase hex")
    return value


def _name(obj: dict) -> str:
    value = _string(obj, "name", "check")
    if len(value) > 80 or "\\" in value or "/" in value:
        raise Refusal("check.name must be a short label without path separators")
    return value


def _argv(value: object) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 64:
        raise Refusal("check.argv must be a list of 1..64 strings")
    result = []
    for item in value:
        if (not isinstance(item, str) or not item or "\x00" in item
                or len(item) > 4096):
            raise Refusal("check.argv entries must be nonempty strings without NUL")
        result.append(item)
    return result


def _positive_seconds(value: object, name: str) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not 0 < value <= 3600 or value != value):
        raise Refusal(f"lenovo field {name!r} must be a positive number of seconds")
    return float(value)


def validate_descriptor(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise Refusal("descriptor must be a JSON object")
    _strict(raw, ("schema", "check", "dell", "lenovo"), "descriptor")
    if raw.get("schema") != SCHEMA:
        raise Refusal(f"descriptor schema must be {SCHEMA}")
    check = raw.get("check")
    if not isinstance(check, dict):
        raise Refusal("descriptor field 'check' must be an object")
    _strict(check, ("name", "host", "effect", "argv", "cwd",
                    "expectedRevision", "timeoutSeconds", "maxOutputBytes"),
            "check")
    host = _string(check, "host", "check")
    if host not in HOSTS:
        raise Refusal("check.host must be dell or lenovo")
    if check.get("effect") != "read-only":
        raise Refusal("check.effect must be read-only: this seam runs one "
                      "declared read-only check")
    result = {"check": {
        "name": _name(check),
        "host": host,
        "effect": "read-only",
        "argv": _argv(check.get("argv")),
        "cwd": _string(check, "cwd", "check"),
        "expectedRevision": _hex(check.get("expectedRevision"),
                                 "expectedRevision", "check", 40),
        "timeoutSeconds": _integer(check, "timeoutSeconds", "check", 120,
                                   1, MAX_TIMEOUT_SECONDS),
        "maxOutputBytes": _integer(check, "maxOutputBytes", "check", 65536,
                                   1024, 262144),
    }}
    if host == "dell":
        if "lenovo" in raw:
            raise Refusal("a dell descriptor must not carry a lenovo block")
        block = raw.get("dell")
        if not isinstance(block, dict):
            raise Refusal("a dell descriptor requires a 'dell' object")
        _strict(block, ("revisionRepository", "git"), "dell")
        git = block.get("git", "git")
        if not isinstance(git, str) or not git.strip():
            raise Refusal("dell field 'git' must be a nonempty string")
        result["dell"] = {"revisionRepository": _string(
            block, "revisionRepository", "dell"), "git": git}
        return result
    if "dell" in raw:
        raise Refusal("a lenovo descriptor must not carry a dell block")
    block = raw.get("lenovo")
    if not isinstance(block, dict):
        raise Refusal("a lenovo descriptor requires a 'lenovo' object")
    _strict(block, ("queue", "keyFile", "keyFormat", "python", "git",
                    "readinessConfig", "readinessJobId", "requiredComponents",
                    "waitSeconds", "pollSeconds", "evidenceWaitSeconds"),
            "lenovo")
    required = block.get("requiredComponents")
    if not isinstance(required, dict):
        raise Refusal("lenovo.requiredComponents must be an object")
    _strict(required, ("cwd", "tool"), "lenovo.requiredComponents")
    key_format = block.get("keyFormat", "raw")
    if key_format not in ("raw", "base64"):
        raise Refusal("lenovo.keyFormat must be raw or base64")
    python = _string(block, "python", "lenovo")
    if any(char in python for char in '"%!$`\r\n'):
        raise Refusal("lenovo.python must not contain cmd interpolation characters")
    git = block.get("git", "git")
    if not isinstance(git, str) or not git.strip():
        raise Refusal("lenovo field 'git' must be a nonempty string")
    wait = _positive_seconds(block.get("waitSeconds", 600), "waitSeconds")
    poll = _positive_seconds(block.get("pollSeconds", 30), "pollSeconds")
    evidence = _positive_seconds(block.get("evidenceWaitSeconds", 0.5),
                                 "evidenceWaitSeconds")
    if poll > wait:
        raise Refusal("lenovo.pollSeconds must not exceed waitSeconds")
    result["lenovo"] = {
        "queue": _string(block, "queue", "lenovo"),
        "keyFile": _string(block, "keyFile", "lenovo"),
        "keyFormat": key_format,
        "python": python,
        "git": git,
        "readinessConfig": _string(block, "readinessConfig", "lenovo"),
        "readinessJobId": _hex(block.get("readinessJobId"),
                               "readinessJobId", "lenovo", 32),
        "requiredComponents": {
            "cwd": _string(required, "cwd", "lenovo.requiredComponents"),
            "tool": _string(required, "tool", "lenovo.requiredComponents"),
        },
        "waitSeconds": wait,
        "pollSeconds": poll,
        "evidenceWaitSeconds": evidence,
    }
    return result


def read_object(path: Path) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise Refusal("referenced JSON is unreadable or invalid") from error
    if not isinstance(value, dict):
        raise Refusal("referenced JSON must be an object")
    return value


def inside(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False
