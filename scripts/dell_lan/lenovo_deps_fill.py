#!/usr/bin/env python3
"""Lenovo side: pack verified closed-dependency tarballs for one Dell request.

Runs as the single command of a signed LAN job. It reads a needs manifest
from the shared LAN path, re-hashes it against the SHA-256 that travelled
inside the MAC-bound job payload, copies each requested tarball from the
Lenovo-owned source directory to the share under a unique immutable name,
and verifies every byte against the integrity pinned in the needs manifest
before publishing. The export manifest is the small return file; the tarballs
stay on the share and are re-hashed there, exactly like the bundle flow.

Nothing here publishes, pushes, edits GitLab, writes into the checkout, or
contacts any registry: the source directory is prepared by the Lenovo owner
from the corporate source out of band. Dell re-verifies every tarball against
its lockfile, so this step is transport, never trust.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

from dell_lan_common import (
    DETAIL_MAX, Stop, load_json, safe_name, sha256_bytes, sha256_file,
    write_json_atomic,
)

SCHEMA_NEEDS = "dell-deps-needs/1"
SCHEMA_EXPORT = "dell-deps-export/1"


def parse_integrity(value: str) -> tuple[str, bytes]:
    """Split an npm integrity string into (algorithm, expected raw digest)."""

    if not isinstance(value, str) or "-" not in value:
        raise Stop("needs-malformed", detail="integrity must be <algo>-<base64>")
    algo, _, encoded = value.partition("-")
    if algo not in ("sha512", "sha384", "sha256", "sha1"):
        raise Stop("needs-malformed", detail=f"unsupported integrity algo: {algo}")
    try:
        return algo, base64.b64decode(encoded.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as error:
        raise Stop("needs-malformed", detail=f"integrity is not base64: {error}")


def check_integrity(path: Path, integrity: str) -> None:
    digest = hashlib.new(parse_integrity(integrity)[0])
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    if digest.digest() != parse_integrity(integrity)[1]:
        raise Stop("integrity-mismatch", detail=f"{path.name} does not match {integrity}")


def validated(request_path: Path, expected_digest: str) -> dict:
    raw = request_path.read_bytes()
    if sha256_bytes(raw) != (expected_digest or "").lower():
        raise Stop("request-digest-mismatch", detail="needs manifest digest differs")
    request = load_json(request_path, "request-malformed")
    if request.get("schema") != SCHEMA_NEEDS:
        raise Stop("request-malformed", detail=f"expected {SCHEMA_NEEDS}")
    packages = request.get("packages")
    if not isinstance(packages, list) or not packages:
        raise Stop("request-malformed", detail="packages must be a non-empty list")
    seen: set[tuple[str, str]] = set()
    for entry in packages:
        if not isinstance(entry, dict):
            raise Stop("request-malformed", detail="package entry must be an object")
        name = entry.get("name")
        version = entry.get("version")
        if not name or not version or not isinstance(name, str) or not isinstance(version, str):
            raise Stop("request-malformed", detail="package needs name and version")
        if (name, version) in seen:
            raise Stop("request-malformed", detail=f"duplicate package: {name}@{version}")
        seen.add((name, version))
        if not entry.get("resolved") or not entry.get("integrity"):
            raise Stop("request-malformed", detail=f"{name}@{version} needs resolved+integrity")
        parse_integrity(str(entry["integrity"]))
    return request


def source_candidate(source_dir: Path, name: str, version: str) -> Path:
    """The staged source tarball for one need, or raises Stop(missing-package).

    Unscoped ``priv`` stages as ``priv-1.0.0.tgz``; scoped ``@scope/pkg``
    stages as ``@scope-pkg-1.0.0.tgz`` (the one slash becomes a dash, so the
    staged name is always one contained file name). Anything else — traversal,
    backslashes, drive letters, empty scope segments — is not stageable.
    """

    if not isinstance(name, str) or not isinstance(version, str):
        raise Stop("missing-package", detail="package needs name and version")
    if "\\" in name or "\\" in version or ":" in name or ":" in version:
        raise Stop("missing-package", detail=f"{name}@{version} is not stageable")
    parts = name.split("/")
    if len(parts) > 2 or (len(parts) == 2 and not parts[0].startswith("@")):
        raise Stop("missing-package", detail=f"{name}@{version} is not stageable")
    if any(not part or part in (".", "..") for part in parts):
        raise Stop("missing-package", detail=f"{name}@{version} is not stageable")
    if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._+-]*", version):
        raise Stop("missing-package", detail=f"{name}@{version} is not stageable")
    candidate = source_dir / f"{'-'.join(parts)}-{version}.tgz"
    try:
        resolved = candidate.resolve()
        root = source_dir.resolve()
    except OSError:
        raise Stop("missing-package",
                   detail=f"{name}@{version} is not in the source dir")
    if resolved != root and root not in resolved.parents:
        raise Stop("missing-package",
                   detail=f"{name}@{version} escapes the source dir")
    if not resolved.is_file():
        raise Stop("missing-package",
                   detail=f"{name}@{version} is not in the source dir")
    return resolved


def pack(request: dict, request_path: Path, source_dir: Path, out_name: str) -> dict:
    shared = request_path.parent
    if not source_dir.is_dir():
        raise Stop("source-unavailable", detail=str(source_dir)[:DETAIL_MAX])
    token = str(request.get("token") or "")
    if not token:
        raise Stop("request-malformed", detail="needs manifest needs a token")
    files = []
    for index, entry in enumerate(request["packages"]):
        name = str(entry["name"])
        version = str(entry["version"])
        # Containment, not just a name check: the staged source entry must be
        # one contained file reached without traversal or link escape
        # (scoped names stage with their slash as a dash).
        candidate = source_candidate(source_dir, name, version)
        check_integrity(candidate, str(entry["integrity"]))
        tarball = f"deps-{token}-{index}.tgz"
        if not safe_name(tarball):
            raise Stop("request-malformed", detail="derived tarball name is unsafe")
        target = shared / tarball
        if target.exists():
            raise Stop("artifact-exists", detail=f"refusing to replace {tarball}")
        data = candidate.read_bytes()
        target.write_bytes(data)
        files.append({"name": name, "version": version, "tarball": tarball,
                      "sha256": sha256_bytes(data), "integrity": str(entry["integrity"]),
                      "bytes": len(data)})
    manifest = {"schema": SCHEMA_EXPORT, "token": token,
                "lockSha256": request.get("lockSha256"), "files": files}
    manifest_path = shared / out_name
    if manifest_path.exists():
        for item in files:
            (shared / str(item["tarball"])).unlink(missing_ok=True)
        raise Stop("artifact-exists", detail=f"refusing to replace {out_name}")
    write_json_atomic(manifest_path, manifest)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lenovo_deps_fill",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("--request", required=True)
    parser.add_argument("--request-sha256", required=True)
    parser.add_argument("--source-dir", required=True)
    parser.add_argument("--out-name", required=True)
    args = parser.parse_args(argv)
    if not safe_name(args.out_name):
        print(json.dumps({"schema": SCHEMA_EXPORT, "outcome": "refused",
                          "reason": "request-malformed",
                          "detail": "out-name must be one contained file name"},
                         ensure_ascii=False, separators=(",", ":")))
        return 2
    try:
        request = validated(Path(args.request), args.request_sha256)
        manifest = pack(request, Path(args.request), Path(args.source_dir), args.out_name)
    except Stop as stop:
        print(json.dumps({"schema": SCHEMA_EXPORT, "outcome": "refused",
                          "reason": stop.reason, "detail": stop.detail},
                         ensure_ascii=False, separators=(",", ":")))
        print(f"deps fill refused: {stop.reason}", file=sys.stderr)
        return stop.code
    except OSError as error:
        print(json.dumps({"schema": SCHEMA_EXPORT, "outcome": "refused",
                          "reason": "io-error", "detail": str(error)[:DETAIL_MAX]},
                         ensure_ascii=False, separators=(",", ":")))
        return 1
    print(json.dumps(manifest, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
