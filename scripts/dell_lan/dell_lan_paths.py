"""Path custody and directory premises for the Dell/Lenovo LAN drivers.

Split from ``dell_lan_common.py`` so the shared dependency fits the file
token guard. Layer zero: only the standard library, the fail-closed path
identities, the shared constants, and the default directory premises.
``dell_lan_common.py`` re-exports every public name, so existing
``from dell_lan_common import ...`` call sites keep working.
"""

from __future__ import annotations

import os
from pathlib import Path

DETAIL_MAX = 400
MANIFEST_SCHEMA = "git-bundle-manifest/1"
BASE_REQUEST_SCHEMA = "dell-lan-base-request/1"
BASE_EXPORT_SCHEMA = "dell-lan-base-export/1"
FIRST_REQUEST_SCHEMA = "dell-lan-first-request/1"
FIRST_EXPORT_SCHEMA = "dell-lan-first-export/1"
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

def safe_name(value: str) -> bool:
    """True for one contained file name: no directory part, no traversal."""

    if not isinstance(value, str) or not value.strip():
        return False
    return not any(part in value for part in ("/", "\\", "..", ":")) and not value.startswith(".")

def resolve_bridge_file(bridge_dir: Path) -> Path:
    """The configured bridge client file inside ``bridge_dir`` (never executed)."""

    return Path(bridge_dir) / "lan_bridge.py"

def default_bridge_dir() -> Path:
    """The signed bridge client directory: explicit override first.

    ``DELL_LAN_BRIDGE_DIR`` names it directly (owner-provided, absolute).
    Otherwise the task-runner sibling ``scripts/lan_bridge`` is used; it
    carries the client in every checkout of this repository. There is no
    fallback into another repository: a missing client fails closed in
    ``load_bridge``; nothing is guessed.
    """

    override = os.environ.get("DELL_LAN_BRIDGE_DIR")
    if override:
        return Path(override)
    here = Path(__file__).resolve()
    return here.parent.parent / "lan_bridge"

def default_flow_dir() -> Path:
    """The external publication-flow scripts directory: explicit override first.

    The flow itself (preflight, writer, readback) is NOT vendored here.
    ``DELL_LAN_FLOW_DIR`` or ``--flow-dir`` names the owner-provided checkout
    that carries it (the same pattern as the pipeline observer's client dir).
    Otherwise the ``external/gitlab-mr-flow/scripts`` layout beside this
    repository is assumed; a missing directory fails closed in ``load_flow``
    with ``flow-unavailable``. No checkout of another agent repository is
    consulted.
    """

    override = os.environ.get("DELL_LAN_FLOW_DIR")
    if override:
        return Path(override)
    here = Path(__file__).resolve()
    return here.parents[2] / "external" / "gitlab-mr-flow" / "scripts"
