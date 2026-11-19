"""Shared shapes for the open-MR mirror route (issue #142).

Nothing here transports bytes, seals jobs, or talks to a remote. It only
names the request/export schemas, parses and validates a GitLab MR URL, and
loads and validates the repository-to-mirror mapping file. Secrets never
appear here: the mapping holds repository names only, and credentials stay
in their authorized host context (``glab`` on Lenovo, ``gh`` on Dell).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dell_lan"))

from dell_lan_common import DETAIL_MAX as _DETAIL_MAX  # noqa: E402
from dell_lan_common import Stop  # noqa: E402  (single fail-closed Stop for the route)

REQUEST_SCHEMA = "mirror-mr-request/1"
EXPORT_SCHEMA = "mirror-mr-export/1"
MAPPING_SCHEMA = "mirror-mapping/1"
STATE_SCHEMA = "mirror-mr-state/1"
RECEIPT_SCHEMA = "mirror-mr-receipt/1"

TOKEN_RE = re.compile(r"^[0-9a-f]{16}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
JOB_RE = re.compile(r"^[0-9a-f]{32}$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
IID_RE = re.compile(r"^[1-9][0-9]*$")
MIRROR_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
DETAIL_MAX = _DETAIL_MAX

__all__ = ["Stop", "REQUEST_SCHEMA", "EXPORT_SCHEMA", "MAPPING_SCHEMA",
           "STATE_SCHEMA", "RECEIPT_SCHEMA", "parse_mr_url", "mapping_key",
           "load_mapping", "resolve_mirror", "check_branch",
           "check_export_identity"]


def parse_mr_url(url: str) -> dict:
    """Split a GitLab MR URL into host, project path, and integer IID.

    Accepts only ``https://<host>/<project>/-/merge_requests/<iid>`` (the
    trailing slash is tolerated). Anything credential-bearing, non-https, or
    off-shape is refused with an actionable reason instead of being fetched.
    """

    text = (url or "").strip()
    if not text:
        raise Stop("mr-url-empty", detail="--mr-url names no merge request")
    try:
        parts = urlsplit(text)
    except ValueError as error:
        raise Stop("mr-url-invalid", detail=str(error)[:DETAIL_MAX])
    if parts.scheme != "https":
        raise Stop("mr-url-invalid", detail="MR URL must use https")
    if parts.username or parts.password:
        raise Stop("mr-url-credentials",
                   detail="MR URL must not carry credentials; "
                          "authenticate on the authorized host instead")
    if not parts.hostname:
        raise Stop("mr-url-invalid", detail="MR URL has no host")
    path = parts.path.rstrip("/")
    marker = "/-/merge_requests/"
    head, sep, tail = path.partition(marker)
    if not sep or not head or head.startswith("-"):
        raise Stop("mr-url-invalid", detail=(
            "expected https://<host>/<group/project>/-"
            "/merge_requests/<iid>"))
    project = head.strip("/")
    if not project or "//" in project or "." in project.split("/"):
        raise Stop("mr-url-invalid", detail="MR URL project path is not usable")
    if not IID_RE.match(tail or ""):
        raise Stop("mr-url-invalid", detail="MR IID must be a positive integer")
    return {"host": parts.hostname.lower(), "project": project,
            "iid": int(tail), "url": f"https://{parts.hostname.lower()}/"
            f"{project}/-/merge_requests/{int(tail)}"}


def mapping_key(host: str, project: str) -> str:
    return f"{host.lower()}/{project}"


def load_mapping(path: Path) -> dict:
    """Load and validate the repository-to-mirror mapping file."""

    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise Stop("mapping-unreadable", detail=str(error)[:DETAIL_MAX])
    if not isinstance(data, dict) or data.get("version") != MAPPING_SCHEMA:
        raise Stop("mapping-malformed", detail=f"expected {MAPPING_SCHEMA}")
    mirrors = data.get("mirrors")
    if not isinstance(mirrors, dict) or not mirrors:
        raise Stop("mapping-malformed", detail="mirrors must be a non-empty object")
    for key, value in mirrors.items():
        if not isinstance(key, str) or "/" not in key:
            raise Stop("mapping-malformed", detail=f"bad mapping key: {key!r}")
        if not isinstance(value, str) or not MIRROR_RE.match(value):
            raise Stop("mapping-malformed",
                       detail=f"mirror for {key!r} must be owner/repo")
    return data


def resolve_mirror(mapping: dict, host: str, project: str) -> str:
    """The single private mirror for one GitLab project, or a refusal."""

    want = mapping_key(host, project)
    found = [(key, value) for key, value in mapping["mirrors"].items()
             if key.lower() == want]
    if len(found) > 1:
        raise Stop("mapping-ambiguous",
                   detail=f"{len(found)} mirrors configured for {want}")
    if not found:
        raise Stop("mapping-missing",
                   detail=f"no mirror configured for {want}")
    return found[0][1]


def check_branch(value: str, label: str) -> str:
    if not value or not BRANCH_RE.match(value) or ".." in value \
            or value.startswith(("-", "/")) or value.endswith("/"):
        raise Stop("ref-invalid", detail=f"{label} is not a safe branch name")
    return value


def check_export_identity(exported: dict, mr_url: str, iid: int) -> None:
    if str(exported.get("mrUrl") or "") != mr_url \
            or int(exported.get("mrIid") or 0) != int(iid):
        raise Stop("request-malformed",
                   detail="sealed MR != requested MR")


def main(argv=None) -> int:  # pragma: no cover - import surface only
    print("mirror_mr_common: import-only helpers, no CLI", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
