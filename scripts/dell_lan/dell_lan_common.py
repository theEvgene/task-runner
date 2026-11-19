"""Small primitives shared by the Dell and Lenovo sides of the LAN dev loop.

Nothing here adds a transport, a cryptography variant, or a receipt shape
that another area already owns. The queue is the signed LAN bridge
(``scripts/lan_bridge`` in this repository, overridable with
``DELL_LAN_BRIDGE_DIR``), the bundle manifest is the
``git-bundle-manifest/1`` shape (JSON entries plus ``manifestDigest`` over the
compact entry array), and the GitLab readback is the external
``gitlab-mr-flow`` identity receipt supplied through ``--flow-dir``.

The only rule this module adds is the one the queue needs anyway: a job
carries one command, so every variable value travels either in the MAC-bound
job payload (as a SHA-256) or in the request file that digest names.

Path custody and the signed-queue helpers live in dell_lan_paths.py and
dell_lan_queue.py so this dependency fits the file token guard; every
public name is re-exported here, so existing
from dell_lan_common import ... call sites keep working.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import tempfile
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from dell_lan_paths import (
    BASE_EXPORT_SCHEMA,
    BASE_REQUEST_SCHEMA,
    DETAIL_MAX,
    FIRST_EXPORT_SCHEMA,
    FIRST_REQUEST_SCHEMA,
    MANIFEST_SCHEMA,
    PUBLISH_REQUEST_SCHEMA,
    PUBLISH_RESULT_SCHEMA,
    Stop,
    canonical_identity,
    containment,
    default_bridge_dir,
    default_flow_dir,
    inside,
    lexical_identity,
    resolve_bridge_file,
    safe_name,
)
from dell_lan_queue import (
    BASE_REQUEST_NAME_RE,
    FIRST_REQUEST_NAME_RE,
    PUBLISH_REQUEST_NAME_RE,
    command_line,
    expected_request_name,
    fetch_verified,
    git,
    git_ok,
    load_bridge,
    read_key,
    request_file_token,
    result_failure,
    run,
    verify_first_job,
    verify_job,
)

#: Protocol revision of the first-import drivers. Bumped only with a code
#: change deployed on both machines; a sealed result naming another revision
#: is refused instead of imported.
FIRST_IMPORT_DRIVER_VERSION = "1"

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

def remote_has_credentials(url: str | None) -> bool:
    """True when a remote URL carries userinfo (a credential leak risk).

    Any ``user@`` or ``user:pass@`` authority in a ``scheme://`` URL is
    refused before any write or receipt output: the receipt embeds the URL
    verbatim, so a credentialed URL would leak into the share, the queue
    return file, and pasted reports. A bare local path and a normal
    ``git@host:path`` scp form (user without a password) carry no secret
    and are allowed; a scp user part containing ``:`` (``user:pass@``)
    is refused as well.
    """

    if not url or not isinstance(url, str):
        return False
    text = url.strip()
    if not text:
        return False
    if "://" in text:
        try:
            parsed = urllib.parse.urlsplit(text)
        except ValueError:
            return False
        netloc = parsed.netloc or ""
        if "@" in netloc:
            return True
        try:
            if parsed.username or parsed.password:
                return True
        except ValueError:
            return True
        return False
    if "@" in text:
        user_part = text.split("@", 1)[0].split("/")[-1].split("\\")[-1]
        if ":" in user_part:
            return True
    return False

def module_file_digest(module) -> str:
    """SHA-256 of the actual file backing an imported module, or ``""``.

    Legacy helper kept for older callers. It trusts the mutable
    ``module.__file__`` attribute, so a module that rewrites its own
    ``__file__`` can forge the answer. New trust checks must use
    :func:`common_file_digest` (spec origin, never the mutable attribute)
    or hash the resolved configured path directly.
    """

    try:
        path = Path(str(getattr(module, "__file__", "")))
        if not path.is_file():
            return ""
        return sha256_file(path)
    except (OSError, ValueError):
        return ""

def manifest_digest(files: list) -> str:
    """The bundle manifest digest: SHA-256 of the compact entry array."""

    return sha256_bytes(json.dumps(files, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

def common_file_digest() -> str:
    """SHA-256 over the shared dependency file set, or "".

    The dependency is split for the file token guard (dell_lan_paths.py
    and dell_lan_queue.py beside this file), so the pin covers the
    concatenation of all three files in this fixed order. Resolved
    through the import system (find_spec origin), never the mutable
    module.__file__ attribute: a module that rewrites its own
    __file__ cannot redirect this digest. An unresolvable or unreadable
    file yields "" so the caller fails closed.
    """

    try:
        spec = importlib.util.find_spec("dell_lan_common")
        origin = getattr(spec, "origin", None) if spec is not None else None
        if not origin or not Path(origin).is_file():
            return ""
        names = ("dell_lan_common.py", "dell_lan_paths.py", "dell_lan_queue.py")
        digest = hashlib.sha256()
        for name in names:
            candidate = Path(origin).parent / name
            if not candidate.is_file():
                return ""
            digest.update(candidate.read_bytes())
        return digest.hexdigest()
    except (OSError, ValueError):
        return ""
