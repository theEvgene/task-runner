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
from pathlib import Path, PureWindowsPath
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
           "check_export_identity", "parse_mirror_command",
           "is_absolute_lenovo_path", "verify_export_job",
           "MIRROR_REQUEST_NAME_RE"]

MIRROR_REQUEST_NAME_RE = re.compile(r"^mirror-request-([0-9a-f]{16})\.json$")
_LAUNCHER_NAMES = frozenset({"powershell", "powershell.exe", "pwsh", "pwsh.exe"})
_LAUNCHER_FLAGS = frozenset({"-noprofile", "-noninteractive", "-nologo"})
_PYTHON_NAMES = frozenset({"python", "python.exe", "python3", "python3.exe"})
#: Tokens carrying these are a second statement on the worker shell.
#: Backslash, colon, spaces, and single quotes are path bytes, not meta.
_SHELL_META = ("%", "!", "^", "&", "|", "<", ">", "(", ")", ";", '"',
               "\n", "\r", "\x00")


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


def is_absolute_lenovo_path(value: object) -> bool:
    """True when a Lenovo-side path is absolute on Windows or the host.

    Lenovo paths are Windows spellings (drive letter or UNC). The Dell
    writer may run on Windows (where ``Path`` already proves them) or on
    a POSIX host (where only ``PureWindowsPath`` recognises them), so
    either proof counts. Relative spellings prove nothing on either host.
    """

    if not isinstance(value, str) or not value.strip():
        return False
    try:
        if Path(value).is_absolute():
            return True
    except (OSError, ValueError, RuntimeError):
        pass
    try:
        return PureWindowsPath(value).is_absolute()
    except (TypeError, ValueError):
        return False


def _parse_direct_argv(command: str) -> list[str] | None:
    """Parse the request writer's whitespace-separated double-quoted argv.

    Backslash-safe where POSIX ``shlex`` is not: a backslash run is
    literal path bytes unless it directly escapes a double quote, so
    ``C:\\x`` and ``\\\\SERVER\\share`` survive byte-identical. An
    unquoted cmd operator, an unterminated quote, or a NUL refuses with
    ``None`` instead of guessing.
    """

    if not isinstance(command, str) or not command.strip():
        return None
    text = command.strip()
    argv: list[str] = []
    token: list[str] = []
    quoted = False
    active = False
    i = 0
    while i < len(text):
        char = text[i]
        if char in "\r\n&|<>^" and not quoted:
            return None
        if char in " \t" and not quoted:
            if active:
                argv.append("".join(token))
                token, active = [], False
            i += 1
            continue
        if char == '"':
            quoted = not quoted
            active = True
            i += 1
            continue
        if char == "\\":
            start = i
            while i < len(text) and text[i] == "\\":
                i += 1
            count = i - start
            if i < len(text) and text[i] == '"':
                token.extend("\\" * (count // 2))
                if count % 2:
                    token.append('"')
                    i += 1
                else:
                    quoted = not quoted
                    i += 1
                active = True
            else:
                token.extend("\\" * count)
                active = True
            continue
        token.append(char)
        active = True
        i += 1
    if quoted:
        return None
    if active:
        argv.append("".join(token))
    return argv


def _parse_ps_argv(inner: str) -> list[str] | None:
    """Parse the ``& 'arg' 'arg'`` inner script with literal path bytes.

    The transport renders every argument single-quoted with embedded
    single quotes doubled. Bare words, expressions, and substitutions
    are not part of that dialect. Backslashes (including UNC pairs)
    are literal and preserved byte-identical.
    """

    if not isinstance(inner, str):
        return None
    text = inner.strip()
    if not text.startswith("&") or (len(text) > 1 and not text[1].isspace()):
        return None
    i = 1
    argv: list[str] = []
    while i < len(text):
        while i < len(text) and text[i].isspace():
            i += 1
        if i == len(text):
            break
        if text[i] != "'":
            return None
        i += 1
        token: list[str] = []
        while i < len(text):
            if text[i] != "'":
                token.append(text[i])
                i += 1
                continue
            if i + 1 < len(text) and text[i + 1] == "'":
                token.append("'")
                i += 2
                continue
            i += 1
            break
        else:
            return None
        if i < len(text) and not text[i].isspace():
            return None
        argv.append("".join(token))
    return argv or None


def _launcher_basename(token: str) -> str:
    return token.strip().strip('"').replace("/", "\\").rsplit("\\", 1)[-1].lower()


def _unwrap_canonical(command: str) -> list[str] | None:
    """The driver argv inside the canonical PowerShell wrapper, or None.

    Returns ``None`` for the legacy direct form (the caller tries that
    next) and raises ``Stop`` only for a wrapper-shaped command that is
    malformed, so a second statement can never pass as a wrapper.
    """

    outer = _parse_direct_argv(command)
    if not outer:
        raise Stop("queue-job-mismatch", detail=command[:DETAIL_MAX])
    if _launcher_basename(outer[0]) not in _LAUNCHER_NAMES:
        return None
    if len(outer) < 3 or outer[-2].lower() != "-command":
        raise Stop("queue-job-mismatch", detail=command[:DETAIL_MAX])
    if not {token.lower() for token in outer[1:-2]} <= _LAUNCHER_FLAGS:
        # The canonical writer emits exactly -NoProfile -NonInteractive;
        # anything else in a launcher-shaped command refuses fail-closed.
        raise Stop("queue-job-mismatch", detail=command[:DETAIL_MAX])
    inner = _parse_ps_argv(outer[-1])
    if not inner:
        raise Stop("queue-job-mismatch", detail=command[:DETAIL_MAX])
    return inner


def parse_mirror_command(command: str, expected_driver: str) -> dict:
    """Parse and bind the exact mirror-export invocation (fail closed).

    Accepts the canonical PowerShell wrapper the worker executes
    (``powershell.exe -NoProfile -NonInteractive -Command "& '...'``)
    and the legacy direct form it replaced. Either way the result is
    exactly ``<python> <driver> --request <request> --request-sha256
    <digest>`` with an optional absolute ``--flow-dir <dir>`` pair:
    absolute interpreter and driver paths, the ``mirror-request-<16hex>``
    basename, a 64-hex digest, no extra arguments, and no shell
    operators. Backslashes and UNC pairs are path bytes and survive
    byte-identical; anything that would let the sealed text name a
    second command refuses as ``queue-job-mismatch`` without weakening
    the digest, token, or driver identity the caller binds next.
    """

    if not isinstance(command, str) or not command.strip():
        raise Stop("queue-job-mismatch", detail="empty command")
    if len(command.encode("utf-8")) > 1024 * 1024:
        raise Stop("queue-job-mismatch", detail="command is too large")
    argv: list[str] | None = None
    try:
        argv = _unwrap_canonical(command)
    except Stop:
        raise
    if argv is None:
        parsed = _parse_direct_argv(command)
        if not parsed:
            raise Stop("queue-job-mismatch", detail=command[:DETAIL_MAX])
        argv = parsed
    for token in argv:
        if not token or any(char in token for char in _SHELL_META):
            raise Stop("queue-job-mismatch", detail=command[:DETAIL_MAX])
    if len(argv) not in (6, 8):
        raise Stop("queue-job-mismatch", detail=command[:DETAIL_MAX])
    python, driver, flag_request, request_path, flag_digest, digest = argv[:6]
    if PureWindowsPath(python).name.lower() not in _PYTHON_NAMES \
            and Path(python).name.lower() not in _PYTHON_NAMES:
        raise Stop("queue-job-mismatch", detail=command[:DETAIL_MAX])
    if not python or not is_absolute_lenovo_path(python):
        raise Stop("queue-job-mismatch",
                   detail="interpreter path must be absolute")
    if not is_absolute_lenovo_path(driver):
        raise Stop("queue-job-mismatch",
                   detail="driver and request paths must be absolute")
    if Path(driver).name != expected_driver \
            and PureWindowsPath(driver).name != expected_driver:
        raise Stop("queue-job-mismatch", detail=command[:DETAIL_MAX])
    if flag_request != "--request" or flag_digest != "--request-sha256":
        raise Stop("queue-job-mismatch", detail=command[:DETAIL_MAX])
    match = MIRROR_REQUEST_NAME_RE.fullmatch(
        PureWindowsPath(request_path).name
        if "\\" in request_path else Path(request_path).name)
    if match is None:
        raise Stop("queue-job-mismatch", detail=command[:DETAIL_MAX])
    if not re.fullmatch(r"[0-9a-f]{64}", digest.lower()):
        raise Stop("queue-job-mismatch", detail=command[:DETAIL_MAX])
    flow_dir: str | None = None
    if len(argv) == 8:
        if argv[6] != "--flow-dir" or not is_absolute_lenovo_path(argv[7]):
            raise Stop("queue-job-mismatch", detail=command[:DETAIL_MAX])
        for token in (argv[7],):
            if not token or any(char in token for char in _SHELL_META):
                raise Stop("queue-job-mismatch", detail=command[:DETAIL_MAX])
        flow_dir = argv[7]
    return {"python": python, "driver": driver,
            "requestPath": request_path, "digest": digest.lower(),
            "requestToken": match.group(1), "flowDir": flow_dir}


def _canonical_lenovo_path(value: object) -> str | None:
    """Canonical Windows identity of a Lenovo path, or None when unprovable."""

    if not isinstance(value, str) or not value.strip():
        return None
    try:
        path = PureWindowsPath(value.strip())
    except (TypeError, ValueError):
        return None
    if not path.is_absolute():
        return None
    anchor = path.anchor.rstrip("\\/")
    if not anchor:
        return None
    rest = [part for part in path.parts[1:] if part not in ("\\", "/")]
    if any(part in (".", "..") for part in rest):
        return None
    return "\\".join([anchor.casefold(), *(part.casefold() for part in rest)])


def verify_export_job(bridge, queue, key: bytes, job_id: str,
                      expected_driver: str) -> dict:
    """Bind the sealed job to the exact mirror-export command (fail closed).

    Shared by the open and merged import drivers. A substring marker is
    never enough: the sealed command must be the canonical PowerShell
    wrapper the worker executes (the legacy direct form still verifies
    for in-flight same-ID resumes) carrying exactly ``<python> <driver>
    --request <request> --request-sha256 <digest>`` with an optional
    ``--flow-dir <dir>`` pair. The parsed request file token is stashed
    as ``_requestFileToken`` so the sealed payload token binds to the
    exact file the job named. A sealed ``expected_working_directory``
    that disagrees with the job's own working directory refuses.
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
    parsed = parse_mirror_command(command, expected_driver)
    expected = job.get("expected_working_directory")
    if expected is not None:
        actual = job.get("working_directory")
        if _canonical_lenovo_path(expected) is None \
                or _canonical_lenovo_path(expected) \
                != _canonical_lenovo_path(actual):
            raise Stop("queue-job-mismatch",
                       detail="sealed working directory disagrees with its expectation")
    job["_requestFileToken"] = parsed["requestToken"]
    job["_requestDigest"] = parsed["digest"]
    job["_requestPath"] = parsed["requestPath"]
    if parsed["flowDir"] is not None:
        job["_requestFlowDir"] = parsed["flowDir"]
    return job


def main(argv=None) -> int:  # pragma: no cover - import surface only
    print("mirror_mr_common: import-only helpers, no CLI", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
