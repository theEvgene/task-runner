"""Small primitives shared by the Dell and Lenovo sides of the LAN dev loop.

Nothing here adds a transport, a cryptography variant, or a receipt shape
that another area already owns. The queue is the signed LAN bridge
(``scripts/lan_bridge`` in this repository; ``skills/t-bank/lan-task-bridge``
in a full personal-agent-toolkit checkout), the bundle manifest is the
``git-bundle-manifest/1`` shape (JSON entries plus ``manifestDigest`` over the
compact entry array), and the GitLab readback is the external
``gitlab-mr-flow`` identity receipt supplied through ``--flow-dir``.

The only rule this module adds is the one the queue needs anyway: a job
carries one command, so every variable value travels either in the MAC-bound
job payload (as a SHA-256) or in the request file that digest names.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

DETAIL_MAX = 400
MANIFEST_SCHEMA = "git-bundle-manifest/1"
BASE_REQUEST_SCHEMA = "dell-lan-base-request/1"
BASE_EXPORT_SCHEMA = "dell-lan-base-export/1"
PUBLISH_REQUEST_SCHEMA = "dell-lan-publish-request/1"
PUBLISH_RESULT_SCHEMA = "dell-lan-publish-result/1"


class Stop(Exception):
    """A fail-closed stop with a reason, a verdict, and an exit code."""

    def __init__(self, reason: str, verdict: str = "mismatch", code: int = 1,
                 detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.verdict = verdict
        self.code = code
        self.detail = detail


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inside(child: Path, parent: Path) -> bool:
    try:
        Path(child).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


class _UnprovablePath(Exception):
    """A path whose canonical identity could not be established."""


_EXTENDED = "\\\\?\\"
_EXTENDED_UNC = "\\\\?\\UNC\\"


def _strip_extended(text: str) -> str:
    """Drop the Windows extended-length prefix so both spellings compare equal."""

    if text.startswith(_EXTENDED_UNC):
        return "\\\\" + text[len(_EXTENDED_UNC):]
    if text.startswith(_EXTENDED):
        return text[len(_EXTENDED):]
    return text


def canonical_identity(value: Path) -> str:
    """The one comparable identity of a path, or ``_UnprovablePath``.

    ``Path.resolve`` follows symlinks and NTFS junctions and, on Windows, asks
    the filesystem for the final path, which also collapses ``subst`` drives
    and 8.3 short names. Case and separators are normalised so ``C:\\Share``,
    ``c:/share`` and a trailing-dot spelling cannot pass as different places.
    """

    try:
        resolved = Path(value).resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise _UnprovablePath(str(error)) from error
    text = _strip_extended(os.path.normpath(str(resolved)))
    if not os.path.isabs(text):
        raise _UnprovablePath(f"not absolute after resolution: {text}")
    return os.path.normcase(text)


def lexical_identity(value: Path) -> str:
    """The same identity without following links: the path as the caller spells it."""

    text = _strip_extended(os.path.normpath(os.path.abspath(str(value))))
    if not os.path.isabs(text):
        raise _UnprovablePath(f"not absolute: {text}")
    return os.path.normcase(text)


def _within(child: str, parent: str) -> bool:
    if child == parent:
        return True
    root = parent if parent.endswith(os.sep) else parent + os.sep
    return child.startswith(root)


def containment(candidate: Path, root: Path) -> bool | None:
    """True when ``candidate`` is inside ``root``, False outside, None unproven.

    Three independent identities are compared: the resolved candidate, the
    resolved parent directory, and the lexical path. The parent catches a link
    or junction *entry* placed inside the root even when the bytes it names
    live outside, the lexical path catches a root reachable only through the
    caller's spelling, and the resolved path catches an alias that lands inside.
    Any resolution failure returns ``None`` so a caller can fail closed.
    """

    try:
        candidate_id = canonical_identity(candidate)
        parent_id = canonical_identity(Path(candidate).parent)
        candidate_lex = lexical_identity(candidate)
        root_id = canonical_identity(root)
        root_lex = lexical_identity(root)
    except _UnprovablePath:
        return None
    return (_within(candidate_id, root_id)
            or _within(parent_id, root_id)
            or _within(candidate_lex, root_lex))


def load_json(path: Path, reason: str) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise Stop(reason, detail=str(error)[:DETAIL_MAX])
    if not isinstance(data, dict):
        raise Stop(reason, detail="not a JSON object")
    return data


def write_json_atomic(path: Path, data: dict) -> None:
    """Write one receipt atomically next to its destination."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                         dir=str(path.parent))
    os.close(handle)
    try:
        Path(temp_name).write_bytes(
            (json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        )
        os.replace(temp_name, path)
    finally:
        if Path(temp_name).exists():
            Path(temp_name).unlink()


def safe_name(value: str) -> bool:
    """True for one contained file name: no directory part, no traversal."""

    if not isinstance(value, str) or not value.strip():
        return False
    return not any(part in value for part in ("/", "\\", "..", ":")) and not value.startswith(".")


def manifest_digest(files: list) -> str:
    """The issue-517 manifest digest: SHA-256 of the compact entry array."""

    return sha256_bytes(json.dumps(files, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


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


def load_bridge(bridge_dir: Path):
    path = Path(bridge_dir) / "lan_bridge.py"
    if not path.is_file():
        raise Stop("queue-unreadable", detail=f"bridge not found: {path}")
    spec = importlib.util.spec_from_file_location("dell_lan_bridge", str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules["dell_lan_bridge"] = module
    spec.loader.exec_module(module)
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


def default_bridge_dir() -> Path:
    """The signed bridge client directory: explicit override first.

    ``DELL_LAN_BRIDGE_DIR`` names it directly (owner-provided, absolute).
    Otherwise the task-runner sibling ``scripts/lan_bridge`` is used when it
    carries the client; the final fallback keeps the personal-agent-toolkit
    checkout spelling so this copy also runs from that tree. A missing client
    fails closed in ``load_bridge``; nothing is guessed.
    """

    override = os.environ.get("DELL_LAN_BRIDGE_DIR")
    if override:
        return Path(override)
    here = Path(__file__).resolve()
    runner = here.parent.parent / "lan_bridge"
    if (runner / "lan_bridge.py").is_file():
        return runner
    return here.parents[3] / "skills" / "t-bank" / "lan-task-bridge" / "scripts"


def default_flow_dir() -> Path:
    """The external publication-flow scripts directory: explicit override first.

    The flow itself (preflight, writer, readback) is NOT vendored here.
    ``DELL_LAN_FLOW_DIR`` or ``--flow-dir`` names the owner-provided checkout
    that carries it (the same pattern as the pipeline observer's client dir).
    The final fallback keeps the personal-agent-toolkit checkout spelling; a
    missing directory fails closed in ``load_flow`` with ``flow-unavailable``.
    """

    override = os.environ.get("DELL_LAN_FLOW_DIR")
    if override:
        return Path(override)
    here = Path(__file__).resolve()
    legacy = here.parents[2] / "t-bank" / "gitlab-mr-flow" / "scripts"
    if (legacy / "publish_mr.py").is_file():
        return legacy
    return here.parents[2] / "external" / "gitlab-mr-flow" / "scripts"
