"""Bind the executed command and its parameters to the verified request.

``dell_managed_submit`` re-reads the request file and proves its SHA-256, so
the request bytes are the one authenticated source on the Dell side. The
command a Lenovo job executes must therefore be a pure function of those
bytes: this module derives the exact command from the request alone and
refuses any writer record or command file that disagrees. Comparing the
writer's ``command`` text with the command-file text proves nothing, because
one caller supplies both.

Contract:

* The request records its own ``requestPath`` and an ``execution`` block
  (interpreter, driver, dialect, flow dir, working directory). Both live
  inside the digest the signed job carries, so neither can be re-pointed
  beside the request.
* The expected command is ``<python> <driver> --request <requestPath>
  --request-sha256 <digest> [--flow-dir <dir>] [--reconcile]`` rendered for
  the request's own dialect. The submit publishes exactly that text, not the
  file's.
* The driver is the schema's own: ``publish``, ``transfer``, and
  ``publish-existing`` never share one.
* ``--reconcile`` appears exactly when the request is ``reconcileOnly``, so a
  read-only request cannot execute a write command and a write request cannot
  execute an unclaimed probe.
"""

from __future__ import annotations

from pathlib import Path

from dell_lan_common import PUBLISH_REQUEST_SCHEMA
from dell_standalone_common import (PUBLISH_EXISTING_REQUEST_SCHEMA,
                                    TRANSFER_REQUEST_SCHEMA)
from shell_command import ShellDialectError, build_for_transport

WRITE_DRIVERS = {
    PUBLISH_REQUEST_SCHEMA: "lenovo_publish.py",
    TRANSFER_REQUEST_SCHEMA: "lenovo_transfer.py",
    PUBLISH_EXISTING_REQUEST_SCHEMA: "lenovo_publish_existing.py",
}
DIALECTS = ("cmd", "powershell")


class ExecutionRefused(ValueError):
    """The executed command is not the one the verified request defines."""


def _absolute(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise ExecutionRefused(f"{label} must be one absolute Lenovo path")
    return value


def execution_parameters(request: dict) -> dict:
    """The request's own authenticated execution parameters."""

    if request.get("schema") not in WRITE_DRIVERS:
        raise ExecutionRefused("request is not one of the three managed writes")
    block = request.get("execution")
    if not isinstance(block, dict):
        raise ExecutionRefused(
            "request carries no execution block; rebuild the request")
    driver = _absolute(block.get("driverPath"), "execution.driverPath")
    if Path(driver).name != WRITE_DRIVERS[request["schema"]]:
        raise ExecutionRefused(
            f"request driver {Path(driver).name} does not match its schema")
    dialect = block.get("dialect")
    if dialect not in DIALECTS:
        raise ExecutionRefused("request dialect must be exactly cmd or powershell")
    flow_dir = block.get("flowDir")
    return_file = block.get("returnFile")
    return {
        "requestPath": _absolute(request.get("requestPath"), "requestPath"),
        "python": _absolute(block.get("python"), "execution.python"),
        "driverPath": driver,
        "dialect": dialect,
        "flowDir": (None if flow_dir in (None, "")
                    else _absolute(flow_dir, "execution.flowDir")),
        "expectedCwd": _absolute(block.get("expectedCwd"),
                                 "execution.expectedCwd"),
        "returnFile": (None if return_file in (None, "")
                       else _absolute(return_file, "execution.returnFile")),
    }


def expected_command(request: dict, digest: str) -> tuple[str, dict]:
    """The one command this verified request defines, plus its parameters."""

    parameters = execution_parameters(request)
    parts = [parameters["python"], parameters["driverPath"],
             "--request", parameters["requestPath"],
             "--request-sha256", digest]
    if parameters["flowDir"]:
        parts += ["--flow-dir", parameters["flowDir"]]
    if request.get("reconcileOnly") is True:
        parts.append("--reconcile")
    try:
        command = build_for_transport(parts, parameters["dialect"])
    except ShellDialectError as error:
        raise ExecutionRefused(
            f"request cannot produce one command: {error}") from error
    return command, parameters


def verified_command(document: dict, command_text: str, request: dict,
                     digest: str) -> tuple[str, dict]:
    """The command to publish, or a refusal naming the disagreeing field."""

    command, parameters = expected_command(request, digest)
    expected = {
        "kind": request["schema"],
        "requestPath": parameters["requestPath"],
        "dialect": parameters["dialect"],
        "receiptScriptPath": parameters["driverPath"],
        "expectedCwd": parameters["expectedCwd"],
        "returnFile": parameters["returnFile"],
    }
    for name, value in expected.items():
        if document.get(name) != value:
            raise ExecutionRefused(
                f"writer output {name} disagrees with the verified request")
    if command_text != command:
        raise ExecutionRefused(
            "command file is not the command the verified request defines")
    return command, parameters
