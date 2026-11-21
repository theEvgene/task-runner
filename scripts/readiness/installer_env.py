"""Installer-environment preflight for the Lenovo readiness probe.

Issue #160: a present shell is not proof that an installer command
resolves in that process. PAT rollout on Lenovo failed with PowerShell
available but ``Get-FileHash`` absent until the process imported its
module. ``check.py`` therefore accepts an optional ``lenovo.environment``
block: PowerShell commands verified in the installer-process context
(``Get-Command``, with the module fallback that names the process-import
fix) and runtime versions (``--version`` against an owner minimum).

Worker output for these entries carries an allowlisted ``kind``/``detail``
pair; the Dell side rebuilds every bounded reason from the trusted
configuration and never relays worker text. States reuse the readiness
vocabulary: ``confirmed`` is available, ``missing`` is absent, ``stale``
is present but below the required minimum, ``unconfirmed`` is unknown.
Any non-confirmed entry makes the report ``not-ready``; the dependent
install must not run. Recovery re-reads the same signed job id and never
submits a second job while the outcome is unknown.

This module also owns the directory revision-evidence helper: binding an
observed checkout SHA to its exact cwd is environment evidence for the
selected check and never changes the readiness outcome.
"""

from __future__ import annotations

import hashlib
import re

COMMAND_DETAILS = ("available", "command-absent", "module-available",
                   "module-missing", "shell-unavailable", "probe-error")
RUNTIME_DETAILS = ("meets-minimum", "no-requirement", "absent",
                   "below-minimum", "unreadable")

_DETAIL_STATE = {
    "available": "confirmed",
    "command-absent": "missing",
    "module-available": "missing",
    "module-missing": "missing",
    "shell-unavailable": "unconfirmed",
    "probe-error": "unconfirmed",
    "meets-minimum": "confirmed",
    "no-requirement": "confirmed",
    "absent": "missing",
    "below-minimum": "stale",
    "unreadable": "unconfirmed",
}

_COMMAND_RE = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,63}")
_MODULE_RE = re.compile(r"[A-Za-z][A-Za-z0-9._-]{0,127}")
_VERSION_RE = re.compile(r"\d+(?:\.\d+){0,2}")
_MAX_TEXT = 4096


class Refusal(ValueError):
    pass


def _shell_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip() \
            or len(value) > _MAX_TEXT or "\x00" in value:
        raise Refusal("lenovo environment shell entries must be nonempty text")
    return value


def _command_definition(item: object) -> dict:
    if not isinstance(item, dict):
        raise Refusal("every environment command must be an object")
    extra = set(item) - {"name", "module"}
    if extra:
        raise Refusal("environment commands accept only name and module")
    name = item.get("name")
    if not isinstance(name, str) or not _COMMAND_RE.fullmatch(name):
        raise Refusal("environment command names use letters, digits and dashes")
    result = {"name": name}
    module = item.get("module")
    if module is not None:
        if not isinstance(module, str) or not _MODULE_RE.fullmatch(module):
            raise Refusal(f"environment command {name!r} has an invalid module")
        result["module"] = module
    return result


def _runtime_definition(item: object) -> dict:
    if not isinstance(item, dict):
        raise Refusal("every environment runtime must be an object")
    extra = set(item) - {"name", "command", "minimum"}
    if extra:
        raise Refusal("environment runtimes accept only name, command and minimum")
    name = item.get("name")
    if not isinstance(name, str) or not _COMMAND_RE.fullmatch(name):
        raise Refusal("environment runtime names use letters, digits and dashes")
    command = item.get("command")
    if not isinstance(command, str) or not command.strip() \
            or len(command) > _MAX_TEXT or "\x00" in command:
        raise Refusal(f"environment runtime {name!r} needs an executable command")
    result = {"name": name, "command": command}
    minimum = item.get("minimum")
    if minimum is not None:
        if not isinstance(minimum, str) or not _VERSION_RE.fullmatch(minimum):
            raise Refusal(f"environment runtime {name!r} has an invalid minimum")
        result["minimum"] = minimum
    return result


def validate_environment(lenovo: dict) -> dict | None:
    """Normalize the optional ``lenovo.environment`` block, or None."""
    raw = lenovo.get("environment")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise Refusal("lenovo environment must be an object")
    extra = set(raw) - {"commands", "runtimes", "shell"}
    if extra:
        raise Refusal(f"lenovo environment has an unexpected field {sorted(extra)[0]!r}")
    commands = raw.get("commands", [])
    runtimes = raw.get("runtimes", [])
    if not isinstance(commands, list) or not isinstance(runtimes, list):
        raise Refusal("lenovo environment commands and runtimes must be lists")
    shell = raw.get("shell")
    if shell is not None:
        if isinstance(shell, str):
            shell = _shell_text(shell)
        elif isinstance(shell, list) and 1 <= len(shell) <= 8:
            shell = [_shell_text(part) for part in shell]
        else:
            raise Refusal("lenovo environment shell is a path or a 1..8 argv list")
    return {"commands": [_command_definition(x) for x in commands],
            "runtimes": [_runtime_definition(x) for x in runtimes],
            "shell": shell}


def env_definitions(config: dict) -> tuple[dict, dict]:
    """Split the configured environment entries by reporting name."""
    env = (config.get("lenovo") or {}).get("environment") or {}
    return ({item["name"]: item for item in env.get("commands", [])},
            {item["name"]: item for item in env.get("runtimes", [])})


def all_names(environment: dict | None) -> list[str]:
    """Every environment reporting name, for the uniqueness gate."""
    env = environment or {}
    return [item["name"] for item in env.get("commands", [])] + \
        [item["name"] for item in env.get("runtimes", [])]


def details(kind: str) -> tuple[str, ...]:
    """Allowlisted detail codes for one environment kind."""
    return COMMAND_DETAILS if kind == "command" else RUNTIME_DETAILS


def expected_state(detail: str) -> str:
    """The only worker state a detail code may arrive with."""
    return _DETAIL_STATE[detail]


def describe(kind: str, detail: str, definition: dict) -> tuple[str, str]:
    """Bounded (reason, nextAction) rebuilt from the trusted configuration."""
    if kind == "command":
        module = definition.get("module")
        return {
            "available": ("required command is available"
                          " in the installer process context", "none"),
            "command-absent": ("required command is absent"
                               " in the installer process context",
                               "provide the required command on the target host"),
            "module-available": ("required command is absent but its module"
                                 " is available; the installer process must"
                                 " import it",
                                 f"import {module} in the installer"
                                 " process before use"),
            "module-missing": ("required command and its module are absent",
                               "provide the required module on the target host"),
            "shell-unavailable": ("installer shell is unavailable;"
                                 " commands could not be confirmed",
                                 "restore the installer shell on the target host"),
            "probe-error": ("installer command check could not be completed",
                            "repair or separately verify the required condition"),
        }[detail]
    minimum = definition.get("minimum")
    return {
        "meets-minimum": ("required runtime meets the configured minimum", "none"),
        "no-requirement": ("required runtime is available", "none"),
        "absent": ("required runtime executable is absent",
                   "install or select the required runtime on the target host"),
        "below-minimum": ("required runtime is present but below"
                          " the required minimum",
                          f"update the required runtime to {minimum} or newer"),
        "unreadable": ("required runtime version could not be read",
                       "repair or separately verify the required runtime"),
    }[detail]


def check_item(item: dict, cmd_defs: dict, rt_defs: dict) -> tuple[str, str] | None:
    """Validate one worker component for the environment gate.

    Returns the rebuilt ``(reason, nextAction)`` for environment names and
    None for classic file/tool components. Raises Refusal when an
    environment entry carries a wrong kind/detail/state, or when a classic
    entry smuggles kind/detail.
    """
    name = item.get("name")
    if name in cmd_defs:
        kind, definition = "command", cmd_defs[name]
    elif name in rt_defs:
        kind, definition = "runtime", rt_defs[name]
    else:
        if "kind" in item or "detail" in item:
            raise Refusal("verified worker result contains an unexpected component")
        return None
    detail = item.get("detail")
    if item.get("kind") != kind or detail not in details(kind) \
            or item.get("state") != _DETAIL_STATE[detail]:
        raise Refusal("verified worker result contains an unexpected component")
    return describe(kind, detail, definition)


def revision_evidence(schema: str, item: dict, definition: dict) -> dict:
    """Bind a directory component's observed SHA to its exact cwd.

    ``/1`` evidence predates the revision field and is legacy, never verified
    revision evidence.  A malformed SHA or a cwd digest for another path is
    untrusted.  This field is evidence for the selected check only; it never
    changes this readiness report's outcome.
    """
    if schema != "lenovo-readiness-probe/2":
        return {"observedRevision": None, "revisionEvidence": "legacy"}
    digest = hashlib.sha256(definition["path"].encode("utf-8")).hexdigest()
    observed = item.get("observedRevision")
    readable = observed is None or (
        isinstance(observed, str) and len(observed) == 40
        and all(char in "0123456789abcdef" for char in observed))
    if item.get("cwdDigest") != digest or not readable:
        return {"observedRevision": None, "revisionEvidence": "untrusted"}
    if observed is None:
        return {"observedRevision": None, "revisionEvidence": "unconfirmed"}
    return {"observedRevision": observed, "revisionEvidence": "verified"}
