#!/usr/bin/env python3
"""Pure helpers for the Dell batch dependency restoration (no npm, no queue).

Split out of the orchestrator so the file token guard stays green and unit
tests can import the policy without running npm. Every helper is
fail-closed: unknown input yields an explicit refusal reason, never a guess.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import re
import shutil
import socketserver
import subprocess
import threading
from pathlib import Path
from urllib.parse import urlparse

INTEGRITY_RE = re.compile(r"^(sha512|sha384|sha256|sha1)-([A-Za-z0-9+/]+={0,2})$")


def resolve_npm(npm_bin: str, node_bin: str) -> list[str]:
    """An argv prefix that really executes npm (a Windows .cmd is not execable)."""

    path = Path(npm_bin)
    if path.name == "npm-cli.js" or (path.is_file() and path.suffix == ".js"):
        return [node_bin, str(path)]
    if path.parent == path or str(path) == path.name:
        found = shutil.which(npm_bin)
        if found:
            path = Path(found)
    if os.name == "nt" and path.suffix.lower() in (".cmd", ".bat"):
        cli = path.parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
        node = path.parent / "node.exe"
        if cli.is_file() and node.is_file():
            return [str(node), str(cli)]
    return [str(path)]


def run_command(argv: list[str], cwd: Path,
                env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(part) for part in argv], cwd=str(cwd), capture_output=True,
        text=True, encoding="utf-8", errors="replace", check=False, env=env,
    )


def parse_version(text: str) -> tuple[int, int, int] | None:
    parts = text.strip().split(".")
    if len(parts) != 3:
        return None
    try:
        numbers = (int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return None
    if any(n < 0 for n in numbers):
        return None
    return numbers


def _range_fields(dotted: str) -> tuple[list[int | None], list[bool]]:
    """Split '18', '18.2' or '1.2.x' into values and explicit-wildcard flags.

    Returns ([major, minor, patch], [is_wildcard, ...]); absent parts are
    (None, False), explicit x/* parts are (None, True).
    """

    values: list[int | None] = []
    wildcards: list[bool] = []
    for field in dotted.split("."):
        if field.lower() in ("x", "*"):
            values.append(None)
            wildcards.append(True)
        else:
            values.append(int(field))
            wildcards.append(False)
    while len(values) < 3:
        values.append(None)
        wildcards.append(False)
    return values[:3], wildcards[:3]


def satisfy_range(version: str, spec: str) -> tuple[bool, str]:
    """Minimal engines check: ^ ~ >= <= > < = exact, partial versions and space-AND.

    A missing or x minor/patch widens the comparison the way npm does:
    ``>=18`` means ``>=18.0.0``, bare ``18`` means ``18.x`` (>=18.0.0
    <19.0.0), ``~18.2`` means >=18.2.0 <18.3.0. Anything else unknown
    (``||``, hyphen ranges, prereleases) is an explicit refusal, never a pass.
    """

    current = parse_version(version)
    if current is None:
        return False, f"unparseable installed version: {version}"
    text = spec.strip()
    if not text or text in ("*", "x", "X"):
        return True, ""
    if "||" in text or " - " in text:
        return False, f"unsupported range (no || or hyphen): {spec}"
    for token in text.split():
        match = re.fullmatch(r"(>=|<=|>|<|=|~|\^)?([0-9xX*]+(?:\.[0-9xX*]+){0,2})", token)
        if not match:
            return False, f"unsupported range token: {token}"
        operator, dotted = match.groups()
        wanted, wild = _range_fields(dotted)
        specified = [n is not None for n in wanted]
        if not any(specified):
            continue  # a bare x/* token matches anything
        base = tuple(n if n is not None else 0 for n in wanted)
        operator = operator or "="
        if operator in (">=", "<=", ">", "<"):
            if any(wild):
                # A comparator against an explicit wildcard (">=18.x") has
                # no single bound: refuse instead of guessing. A merely
                # absent part (">=18") pads with zeros.
                return False, f"unsupported wildcard comparator: {token}"
            if operator == ">=":
                ok = current >= base
            elif operator == "<=":
                ok = current <= base
            elif operator == ">":
                ok = current > base
            else:
                ok = current < base
            if not ok:
                return False, f"{version} does not satisfy {spec}"
            continue
        last = max(i for i, known in enumerate(specified) if known)
        if operator == "=":
            if all(specified):
                ok = current == base
            else:
                ceiling = base[:last] + (base[last] + 1,) + (0,) * (2 - last)
                ok = current >= base and current < ceiling
        elif operator == "~":
            if last == 0:
                ceiling = (base[0] + 1, 0, 0)
            else:
                ceiling = (base[0], base[1] + 1, 0)
            ok = current >= base and current < ceiling
        else:  # operator == "^"
            if base[0] > 0 or not specified[0]:
                ceiling = (base[0] + 1, 0, 0)
            elif len(specified) > 1 and specified[1] and base[1] > 0:
                ceiling = (0, base[1] + 1, 0)
            elif len(specified) > 2 and specified[2]:
                ceiling = (0, 0, base[2] + 1)
            elif len(specified) > 1 and specified[1]:
                ceiling = (0, base[1] + 1, 0)
            else:
                ceiling = (1, 0, 0)
            ok = current >= base and current < ceiling
        if not ok:
            return False, f"{version} does not satisfy {spec}"
    return True, ""


def check_integrity_bytes(data: bytes, integrity: str) -> bool:
    """True when raw bytes match an npm integrity string (transport-untrusted)."""

    match = INTEGRITY_RE.fullmatch(integrity or "")
    if not match:
        return False
    digest = hashlib.new(match.group(1), data).digest()
    return base64.b64encode(digest).decode("ascii") == match.group(2)


def classify_lock(packages: dict, public_host: str) -> dict:
    """Split lock entries on verifiable data; unknown is never public.

    Public means exactly: an http(s) resolved URL on the named public host
    plus a well-formed integrity. A corporate proxy hostname that serves
    both public and closed packages therefore classifies as private
    (fail-closed: an extra queue batch, never a leak or a wrong fetch).
    """

    buckets: dict[str, list] = {"public": [], "private": [], "local": [],
                                "unknown": [], "limited": []}
    for key, entry in packages.items():
        if not key or not isinstance(entry, dict):
            continue
        name = key.split("node_modules/")[-1]
        version = entry.get("version")
        if not version or not isinstance(version, str):
            buckets["unknown"].append({"key": key, "reason": "no-version"})
            continue
        if entry.get("link"):
            buckets["local"].append({"key": key, "name": name, "version": version})
            continue
        resolved = entry.get("resolved")
        integrity = entry.get("integrity")
        if entry.get("bundled"):
            buckets["limited"].append({"key": key, "name": name, "version": version,
                                      "reason": "bundled-dep"})
            continue
        if not resolved or not isinstance(resolved, str):
            buckets["unknown"].append({"key": key, "name": name, "version": version,
                                      "reason": "no-resolved"})
            continue
        if resolved.startswith(("git+", "git://")):
            buckets["limited"].append({"key": key, "name": name, "version": version,
                                      "reason": "git-source"})
            continue
        if resolved.startswith("file:"):
            buckets["local"].append({"key": key, "name": name, "version": version,
                                     "resolved": resolved})
            continue
        if not resolved.startswith(("https://", "http://")):
            buckets["unknown"].append({"key": key, "name": name, "version": version,
                                      "reason": f"scheme: {resolved[:24]}"})
            continue
        if not isinstance(integrity, str) or not INTEGRITY_RE.fullmatch(integrity):
            buckets["unknown"].append({"key": key, "name": name, "version": version,
                                      "resolved": resolved, "reason": "no-integrity"})
            continue
        need = {"key": key, "name": name, "version": version,
                "resolved": resolved, "integrity": integrity}
        host = (urlparse(resolved).hostname or "").lower()
        buckets["public" if host == public_host else "private"].append(need)
    return buckets


def cache_fingerprint(cache_dir: Path) -> str:
    """Bind the actual cache state (paths, sizes, mtimes, content) without parsing npm.

    Every file contributes its SHA-256, so a same-size byte swap with a
    preserved mtime still changes the fingerprint. Directory walks stay
    cheap enough for one before/after pair per run; _logs are excluded.
    """

    rows = []
    for current, _dirs, files in os.walk(cache_dir):
        if "_logs" in Path(current).parts:
            continue
        for name in files:
            path = Path(current) / name
            try:
                stat = path.stat()
                digest = hashlib.sha256()
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1 << 20), b""):
                        digest.update(chunk)
            except OSError:
                continue
            rows.append(f"{path.relative_to(cache_dir)}|{stat.st_size}|"
                        f"{stat.st_mtime_ns}|{digest.hexdigest()}")
    digest = hashlib.sha256()
    digest.update(("\n".join(sorted(rows)) + "\n").encode("utf-8"))
    return digest.hexdigest()


def stub_file(name: str, version: str) -> str:
    return f"{name.split('/')[-1]}-{version}.tgz"


def stub_path(name: str, version: str) -> str:
    """Registry-shaped stub path so npm trusts name+version from the URL."""

    return f"/{name}/-/{stub_file(name, version)}"


class _StubHandler(http.server.BaseHTTPRequestHandler):
    files: dict[str, bytes] = {}
    log: list[str] = []

    def do_GET(self) -> None:  # noqa: N802
        type(self).log.append(self.path)
        body = type(self).files.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


class LocalStub:
    """A disposable localhost-only server for verified tarballs (serves bytes)."""

    def __init__(self, files: dict[str, bytes], port: int = 0) -> None:
        self.files = files
        self.wanted_port = port
        self.port = 0
        self.stable = False
        self.server: socketserver.TCPServer | None = None
        self.thread: threading.Thread | None = None
        self.requests: list[str] = []

    def start(self) -> int:
        _StubHandler.files = dict(self.files)
        _StubHandler.log = []
        try:
            self.server = socketserver.TCPServer(("127.0.0.1", self.wanted_port),
                                                 _StubHandler)
            self.stable = self.wanted_port != 0
        except OSError:
            self.server = socketserver.TCPServer(("127.0.0.1", 0), _StubHandler)
            self.stable = False
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.port

    def stop(self) -> list[str]:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=10)
        self.requests = list(_StubHandler.log)
        return self.requests


def build_scratch_texts(lock_bytes: bytes, manifest_bytes: bytes,
                        rewrite_map: dict[str, str],
                        allow_pins: dict[str, bool]) -> tuple[str, str]:
    """Scratch-only lock/manifest: remapped private URLs plus exact-pin policy."""

    scratch_lock = json.loads(lock_bytes.decode("utf-8"))
    for entry in scratch_lock["packages"].values():
        if isinstance(entry, dict) and entry.get("resolved") in rewrite_map:
            entry["resolved"] = rewrite_map[entry["resolved"]]
    scratch_manifest = json.loads(manifest_bytes.decode("utf-8"))
    if allow_pins:
        scratch_manifest["allowScripts"] = dict(allow_pins)
    return json.dumps(scratch_lock), json.dumps(scratch_manifest)


def copy_project(project: Path, dest: Path) -> None:
    shutil.copytree(project, dest, symlinks=True, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(".npmrc", "node_modules", ".git"))


def check_engines(manifest: dict, node_version: str,
                  npm_version: str) -> tuple[dict, str | None]:
    """Refuse when the toolchain violates package.json engines (explicit)."""

    checked = {}
    for tool, current in (("node", node_version.lstrip("v")),
                          ("npm", npm_version.lstrip("v"))):
        spec = (manifest.get("engines") or {}).get(tool)
        if not spec:
            checked[tool] = {"required": "", "satisfied": True}
            continue
        ok, why = satisfy_range(current, str(spec))
        checked[tool] = {"required": str(spec), "satisfied": ok}
        if not ok:
            return checked, f"{tool} {current} refused by engines: {spec} ({why})"
    return checked, None


def check_lockfile_compat(lockfile_version: object,
                          npm_version: str) -> str | None:
    """Refuse when npm cannot read the lockfileVersion (explicit, testable).

    Returns the refusal message, or None when the combination is accepted.
    Non-integer lockfileVersion values carry no gate (same as no gate).
    """

    if not isinstance(lockfile_version, int):
        return None
    npm_major = (parse_version(npm_version.lstrip("v")) or (0, 0, 0))[0]
    if lockfile_version >= 3 and npm_major < 9:
        return f"npm {npm_version} cannot read lockfileVersion 3"
    if lockfile_version >= 2 and npm_major < 7:
        return f"npm {npm_version} cannot read lockfileVersion {lockfile_version}"
    return None


def scan_workspace_links(packages: dict, project: Path) -> str | None:
    """Refuse link members with install scripts (no exact pin can approve them)."""

    resolved_project = project.resolve()
    for key, entry in packages.items():
        if not key or not isinstance(entry, dict) or not entry.get("link"):
            continue
        resolved = str(entry.get("resolved") or "")
        if resolved.startswith(("http", "git")):
            return f"linked entry escapes the project: {key}"
        member = (project / resolved).resolve()
        if member != resolved_project and resolved_project not in member.parents:
            return f"linked entry escapes the project: {key}"
        try:
            scripts = json.loads(
                (member / "package.json").read_text(encoding="utf-8")).get("scripts") or {}
        except (OSError, ValueError):
            continue
        hooks = [h for h in ("preinstall", "install", "postinstall") if scripts.get(h)]
        if hooks:
            return (f"workspace member {key} has install scripts "
                    f"({', '.join(hooks)}) that strict mode cannot approve")
    return None


def _safe_share_name(value: object) -> bool:
    """True for one contained share file name (same rule as safe_name)."""

    if not isinstance(value, str) or not value.strip():
        return False
    return (not any(part in value for part in ("/", "\\", "..", ":"))
            and not value.startswith("."))


def verify_export_files(shared: Path, export: dict,
                        wanted: list[dict]) -> dict[str, bytes] | None:
    """Re-verify every transferred tarball against the lock (transport-untrusted).

    The tarball field is a single contained file name: anything else
    (traversal, absolute path, symlink escape from the share) refuses the
    whole batch. The read goes through the resolved path that passed the
    containment check.
    """

    import hashlib as _hashlib

    by_need = {(n["name"], n["version"]): n for n in wanted}
    try:
        shared_root = (shared).resolve()
    except OSError:
        return None
    verified: dict[str, bytes] = {}
    for item in export.get("files") or []:
        if not isinstance(item, dict):
            return None
        need = by_need.get((item.get("name"), item.get("version")))
        tarball = item.get("tarball")
        if not need or not _safe_share_name(tarball):
            return None
        try:
            resolved = (shared / str(tarball)).resolve()
        except OSError:
            return None
        if resolved != shared_root and shared_root not in resolved.parents:
            return None
        try:
            data = resolved.read_bytes()
        except OSError:
            return None
        if not data:
            return None
        if not isinstance(item.get("sha256"), str):
            return None
        if _hashlib.sha256(data).hexdigest() != item.get("sha256"):
            return None
        if not check_integrity_bytes(data, need["integrity"]):
            return None
        if not check_integrity_bytes(data, str(item.get("integrity") or "")):
            return None
        verified[f"{need['name']}@{need['version']}"] = data
    if len(verified) != len(wanted):
        return None
    return verified
