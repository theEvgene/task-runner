"""Signed-queue and bridge helpers for the Dell/Lenovo LAN drivers.

Split from ``dell_lan_common.py`` so the shared dependency fits the file
token guard. Imports the constants and path custody from
``dell_lan_paths``; ``dell_lan_common.py`` re-exports every public name, so
existing ``from dell_lan_common import ...`` call sites keep working.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

from dell_lan_paths import (
    BASE_REQUEST_SCHEMA,
    DETAIL_MAX,
    FIRST_REQUEST_SCHEMA,
    PUBLISH_REQUEST_SCHEMA,
    Stop,
    containment,
    resolve_bridge_file,
)

FIRST_REQUEST_NAME_RE = re.compile(r"^first-request-([0-9a-f]{16})\.json$")
BASE_REQUEST_NAME_RE = re.compile(r"^base-request-([0-9a-f]{16})\.json$")
PUBLISH_REQUEST_NAME_RE = re.compile(r"^publish-request-([0-9a-f]{16})\.json$")


def request_file_token(name: str | os.PathLike) -> str | None:
    """The embedded token of a ``*-request-<16hex>.json`` file name, or None."""

    base = Path(str(name)).name
    for pattern in (FIRST_REQUEST_NAME_RE, BASE_REQUEST_NAME_RE,
                    PUBLISH_REQUEST_NAME_RE):
        match = pattern.fullmatch(base)
        if match:
            return match.group(1)
    return None

def expected_request_name(schema: str, token: str) -> str | None:
    """The one request file name bound to this schema and token."""

    if schema == FIRST_REQUEST_SCHEMA:
        return f"first-request-{token}.json"
    if schema == BASE_REQUEST_SCHEMA:
        return f"base-request-{token}.json"
    if schema == PUBLISH_REQUEST_SCHEMA:
        return f"publish-request-{token}.json"
    return None

def run(command: list[str], cwd: Path | None = None, env: dict | None = None,
        stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(part) for part in command],
        cwd=str(cwd) if cwd else None,
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

def git(repo: Path, *args: str) -> str:
    proc = run(["git", "-C", str(repo), *args])
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout or "git failed").strip()
        raise Stop("git-failed", detail=f"git {' '.join(args)}: {message}"[:DETAIL_MAX])
    return proc.stdout.strip()

def git_ok(repo: Path, *args: str) -> tuple[bool, str]:
    proc = run(["git", "-C", str(repo), *args])
    text = (proc.stdout if proc.returncode == 0 else (proc.stderr or proc.stdout)).strip()
    return proc.returncode == 0, text

def _bridge_api_gap(module) -> str | None:
    """The first missing/invalid required bridge member, or None when complete.

    The first-import flow calls ``load_key``, ``read``, ``open_envelope``,
    and ``fetch_result`` and catches ``BridgeError``, ``ProtocolError``,
    ``JobNotFoundError``, and ``FetchTimeout``. A pinned file that imports
    cleanly but omits or mistypes any of these would otherwise fail later
    with an unhandled ``AttributeError``/``TypeError`` and no bounded
    receipt, so the gap is reported here as ``driver-stale``.
    """

    for name in ("load_key", "read", "open_envelope", "fetch_result"):
        if not callable(getattr(module, name, None)):
            return name
    for name in ("BridgeError", "ProtocolError", "JobNotFoundError", "FetchTimeout"):
        member = getattr(module, name, None)
        if not isinstance(member, type) or not issubclass(member, BaseException):
            return name
    return None

def load_bridge(bridge_dir: Path):
    """Import the configured bridge client after the caller verified its pin.

    The caller must hash :func:`resolve_bridge_file` against the trusted pin
    *before* calling this: executing the file first and checking
    ``bridge.__file__`` afterwards lets a malicious bridge run its side
    effects even when its digest mismatches. A missing file fails closed
    with ``queue-unreadable``; a bridge that cannot be imported (syntax
    error, missing dependency, broken loader, or import-time termination
    such as ``SystemExit``/``KeyboardInterrupt``) fails closed with
    ``driver-stale`` so the caller still writes a bounded receipt instead
    of an unhandled traceback or a false-success exit. A pinned file that
    imports but does not expose the required bridge API also fails closed
    with ``driver-stale``. A file that changes between the pin check
    and this import is not detected from in-process measurement: execution
    integrity rests on a read-only verified checkout deployed over a
    trusted channel.
    """

    path = resolve_bridge_file(bridge_dir)
    if not path.is_file():
        raise Stop("queue-unreadable", detail=f"bridge not found: {path}")
    # lan_bridge imports its sibling syntax gate; make that directory importable.
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    try:
        spec = importlib.util.spec_from_file_location("dell_lan_bridge", str(path))
        if spec is None or spec.loader is None:
            raise Stop("driver-stale", detail="bridge import failed: no loader")
        module = importlib.util.module_from_spec(spec)
        sys.modules["dell_lan_bridge"] = module
        try:
            spec.loader.exec_module(module)
        except BaseException as error:
            sys.modules.pop("dell_lan_bridge", None)
            if isinstance(error, Stop):
                raise
            if isinstance(error, SystemExit):
                raise Stop("driver-stale",
                           detail=f"bridge import terminated: SystemExit({error.code})"[:DETAIL_MAX])
            if isinstance(error, KeyboardInterrupt):
                raise Stop("driver-stale",
                           detail="bridge import interrupted"[:DETAIL_MAX])
            raise Stop("driver-stale",
                       detail=f"bridge import failed: {error}"[:DETAIL_MAX])
    except Stop:
        raise
    except Exception as error:
        raise Stop("driver-stale",
                   detail=f"bridge import failed: {error}"[:DETAIL_MAX])
    gap = _bridge_api_gap(module)
    if gap is not None:
        sys.modules.pop("dell_lan_bridge", None)
        raise Stop("driver-stale",
                   detail=f"bridge API incomplete: missing {gap}"[:DETAIL_MAX])
    return module

def _as_paths(value) -> list[Path]:
    if value is None:
        return []
    if isinstance(value, (str, bytes, os.PathLike)):
        return [Path(value)]
    return [Path(item) for item in value]

def read_key(bridge, queue: Path, key_file: Path, key_format: str,
             repos=None, shared=None) -> bytes:
    """Load the queue key only after proving it is outside every protected root.

    The key is the trust anchor: whoever holds it can seal any payload. It
    therefore must not sit where the queue, a checkout, or the shared LAN
    folder can read or copy it. The roots are mandatory and explicit: a caller
    that names no checkout, or no shared folder, has proven nothing, so the key
    is refused instead of being loaded under a weaker check. Each root must be
    absolute, because a relative root names a different place for every caller,
    and each containment is decided by canonical identities (symlinks,
    junctions, ``subst`` drives and Windows 8.3 names included). A root whose
    containment cannot be proven refuses the key instead of assuming the better
    case.
    """

    checkout_roots = _as_paths(repos)
    shared_roots = _as_paths(shared)
    if not checkout_roots:
        raise Stop("key-refused", detail=(
            "no checkout root was named, so the key cannot be proven outside one"))
    if not shared_roots:
        raise Stop("key-refused", detail=(
            "no shared LAN folder root was named, so the key cannot be proven "
            "outside it"))
    protected: list[tuple[str, Path]] = [("queue", Path(queue))]
    protected += [("checkout", root) for root in checkout_roots]
    protected += [("shared LAN folder", root) for root in shared_roots]
    for label, root in protected:
        if not Path(root).is_absolute():
            raise Stop("key-refused", detail=(
                f"the {label} root is not an absolute path, so the key cannot be "
                "proven outside it"))
        verdict = containment(Path(key_file), root)
        if verdict is None:
            raise Stop("key-refused",
                       detail=f"cannot prove the key is outside the {label}")
        if verdict:
            raise Stop("key-refused", detail=f"key inside the {label}")
    try:
        return bridge.load_key(str(key_file), key_format, str(queue))
    except (bridge.BridgeError, OSError, ValueError) as error:
        raise Stop("key-refused", detail=str(error)[:DETAIL_MAX])

def verify_job(bridge, queue: Path, key: bytes, job_id: str, marker: str) -> dict:
    """Read the sealed job for this id and require the documented command."""

    try:
        raw = bridge.read(str(queue / "jobs"), job_id)
    except bridge.JobNotFoundError:
        raise Stop("queue-job-missing", detail=job_id)
    except (bridge.BridgeError, OSError) as error:
        raise Stop("queue-job-unverifiable", detail=str(error)[:DETAIL_MAX])
    try:
        job = bridge.open_envelope(raw, key, "job", job_id)
    except bridge.ProtocolError as error:
        raise Stop("queue-job-unverifiable", detail=str(error)[:DETAIL_MAX])
    command = str(job.get("command") or "")
    if marker not in command:
        raise Stop("queue-job-mismatch", detail=command[:DETAIL_MAX])
    return job

def verify_first_job(bridge, queue: Path, key: bytes, job_id: str) -> dict:
    """Bind the sealed job to the exact first-export command (fail closed).

    A substring marker is not enough: ``echo lenovo_first_export.py`` would
    pass it while executing nothing. The sealed command must parse to exactly
    ``<python> <driver> --request <request> --request-sha256 <digest>`` where
    the driver basename is ``lenovo_first_export.py``, the request basename
    is ``first-request-<16hex>.json`` and the digest is 64 hex. The driver
    and request paths must both be absolute: a relative path names a
    different file for every worker cwd and cannot pin the actually invoked
    driver. Anything else is ``queue-job-mismatch``: the sealed result
    proves nothing about this import. The parsed request file token is
    stashed as ``_requestFileToken`` so the sealed payload token can be
    bound to the exact file the job named.
    """

    try:
        raw = bridge.read(str(queue / "jobs"), job_id)
    except bridge.JobNotFoundError:
        raise Stop("queue-job-missing", detail=job_id)
    except (bridge.BridgeError, OSError) as error:
        raise Stop("queue-job-unverifiable", detail=str(error)[:DETAIL_MAX])
    try:
        job = bridge.open_envelope(raw, key, "job", job_id)
    except bridge.ProtocolError as error:
        raise Stop("queue-job-unverifiable", detail=str(error)[:DETAIL_MAX])
    command = str(job.get("command") or "")
    try:
        argv = shlex.split(command, posix=True)
    except ValueError as error:
        raise Stop("queue-job-mismatch", detail=str(error)[:DETAIL_MAX])
    if len(argv) != 6:
        raise Stop("queue-job-mismatch", detail=command[:DETAIL_MAX])
    python, driver, flag_request, request_path, flag_digest, digest = argv
    if not python or Path(driver).name != "lenovo_first_export.py":
        raise Stop("queue-job-mismatch", detail=command[:DETAIL_MAX])
    if not Path(driver).is_absolute() or not Path(request_path).is_absolute():
        raise Stop("queue-job-mismatch", detail="driver and request paths must be absolute")
    if flag_request != "--request" or flag_digest != "--request-sha256":
        raise Stop("queue-job-mismatch", detail=command[:DETAIL_MAX])
    request_token = request_file_token(Path(request_path).name)
    if request_token is None:
        raise Stop("queue-job-mismatch", detail=command[:DETAIL_MAX])
    if not re.fullmatch(r"[0-9a-f]{64}", digest.lower()):
        raise Stop("queue-job-mismatch", detail=command[:DETAIL_MAX])
    job["_requestFileToken"] = request_token
    return job

def fetch_verified(bridge, queue: Path, key: bytes, job_id: str,
                   timeout: float, poll: float) -> dict:
    """Fetch the sealed result and verify MAC plus kind/id binding."""

    try:
        return bridge.fetch_result(str(queue), key, job_id,
                                   timeout_seconds=timeout, poll_seconds=poll)
    except bridge.FetchTimeout:
        raise Stop("outcome-unknown", verdict="unknown", code=3)
    except bridge.ProtocolError as error:
        raise Stop("queue-result-unverifiable", detail=str(error)[:DETAIL_MAX])
    except (bridge.BridgeError, OSError) as error:
        raise Stop("queue-result-unreadable", detail=str(error)[:DETAIL_MAX])

def result_failure(payload: dict) -> dict | None:
    """The carried failure facts, or None when the job reported success."""

    status = payload.get("status")
    exit_code = payload.get("exit_code")
    timed_out = bool(payload.get("timed_out"))
    truncated = bool(payload.get("output_truncated"))
    if status == "ok" and not isinstance(exit_code, bool) and exit_code == 0 and not timed_out and not truncated:
        return None
    return {"status": status, "exitCode": exit_code, "timedOut": timed_out,
            "outputTruncated": truncated,
            "stderr": str(payload.get("stderr") or "")[:DETAIL_MAX]}

def command_line(parts: list[str]) -> str:
    """Render one command with every path/argument double-quoted."""

    return " ".join(f'"{part}"' for part in parts)
