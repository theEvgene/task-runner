#!/usr/bin/env python3
"""Evaluate one npm lockfile against an owned cache with disposable working data.

The probe copies the whole project tree (excluding ``node_modules``, ``.git``,
and any ``.npmrc``) into a fresh scratch directory and runs
``npm ci --offline`` there, so the real worktree is never touched, workspace
members and files needed by lifecycle scripts travel with the probe, and a
changed lockfile or manifest is evaluated by re-running this script. Only a
``--offline`` success proves offline-readiness: a ``--prefer-offline`` success
may have used the network and proves nothing about the cache.

Lifecycle scripts run by default because the probe always passes an explicit
``--ignore-scripts=false`` and strips any inherited
``npm_config_ignore_scripts`` value from the child environment; project
``.npmrc`` files are never copied, so neither can silently disable scripts.
``--ignore-scripts`` is accepted only with an explicit
``--ignore-scripts-reason`` and is recorded in the receipt, so a
disabled-scripts run is never silent.

This script performs no online fill and contacts no registry: an offline miss
is reported, not repaired. The online fill is a separate, explicitly networked
step in [the reference](../references/npm-offline-deps.md).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from dell_lan_common import DETAIL_MAX, containment, sha256_file, write_json_atomic

SCHEMA = "dell-npm-probe/1"

_MISS_PATTERNS = (
    re.compile(r"ENOTCACHED"),
    re.compile(r"only-if-cached"),
    re.compile(r"no cached response is available"),
)
_MISSING_URL = re.compile(r"request to (\S+) failed")
_MISSING_PKG = re.compile(r"package ([\"']?)([\w@/.\-]+)\1", re.IGNORECASE)
_SOURCING_PATTERNS = (
    re.compile(r"git\b.*depend", re.IGNORECASE),
    re.compile(r"git\+ssh", re.IGNORECASE),
    re.compile(r"ENEEDAUTH"),
    re.compile(r"EPRIVATE"),
    re.compile(r"401|403 Unauthorized|authentication", re.IGNORECASE),
    re.compile(r"EBADPLATFORM|EBADENGINE"),
)


def fail(message: str) -> int:
    print(f"dell npm probe refused: {message}", file=sys.stderr)
    return 2


def run_npm(npm_bin: str, args: list[str], cwd: Path,
            env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    binary = npm_bin
    if Path(binary).name == binary and "/" not in binary and "\\" not in binary:
        resolved = shutil.which(binary)
        if resolved is None:
            raise FileNotFoundError(f"npm binary not on PATH: {binary}")
        binary = resolved
    return subprocess.run(
        [binary, *args], cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False, env=env,
    )


def npm_child_env() -> dict[str, str]:
    """Child environment with any inherited ignore-scripts setting removed.

    npm merges CLI, environment, and rc files with CLI winning, but removing
    the inherited ``npm_config_ignore_scripts`` value makes the effective
    scripts state depend only on the explicit flag this probe passes, so the
    receipt's ``ignoreScripts`` field is truthful.
    """

    env = dict(os.environ)
    for key in list(env.keys()):
        if key.lower() == "npm_config_ignore_scripts":
            del env[key]
    return env


def refused_inside(candidate: Path, root: Path, what: str) -> str | None:
    """Refusal reason when ``candidate`` cannot be proven outside ``root``."""

    try:
        verdict = containment(candidate, root)
    except Exception as error:  # fail closed on any comparison failure
        return f"{what} containment is unprovable: {error}"
    if verdict is None:
        return f"{what} containment is unprovable; refusing"
    if verdict:
        return f"{what} must stay outside {root}"
    return None


def classify(stderr: str) -> tuple[str, str]:
    if any(p.search(stderr) for p in _MISS_PATTERNS):
        return "cache-miss", "offline cache has no entry for a locked package"
    if any(p.search(stderr) for p in _SOURCING_PATTERNS):
        return "sourcing-limited", (
            "locked source is outside the tarball cache "
            "(git, authenticated, or platform-gated dependency)"
        )
    return "failed", "npm reported an error that is neither a clean cache miss nor a known sourcing limit"


def missing_specs(stderr: str) -> list[str]:
    found: list[str] = []
    for match in _MISSING_URL.finditer(stderr):
        url = match.group(1).strip().strip("'\".,;")
        if url and url not in found:
            found.append(url)
    for match in _MISSING_PKG.finditer(stderr):
        name = match.group(2)
        if name and name not in found and name.lower() not in ("to", "ci", "install"):
            found.append(name)
    return found[:20]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dell_npm_probe",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("--project", required=True,
                        help="directory holding package.json and the lockfile")
    parser.add_argument("--lockfile", default="package-lock.json",
                        help="lockfile name inside --project")
    parser.add_argument("--cache-dir", required=True,
                        help="owned npm cache directory (absolute, outside the project)")
    parser.add_argument("--scratch-root", required=True,
                        help="absolute directory under which a disposable probe copy is made")
    parser.add_argument("--receipt", required=True, help="receipt path to write")
    parser.add_argument("--npm", default="npm", help="npm binary")
    parser.add_argument("--ignore-scripts", action="store_true",
                        help="pass --ignore-scripts to npm (requires --ignore-scripts-reason)")
    parser.add_argument("--ignore-scripts-reason", default="",
                        help="why lifecycle scripts were disabled for this run")
    args = parser.parse_args(argv)

    project = Path(args.project)
    cache_dir = Path(args.cache_dir)
    scratch_root = Path(args.scratch_root)
    receipt_path = Path(args.receipt)
    if not project.is_absolute():
        return fail("--project must be an absolute path")
    if not cache_dir.is_absolute():
        return fail("--cache-dir must be an absolute path")
    if not scratch_root.is_absolute():
        return fail("--scratch-root must be an absolute path")
    if not receipt_path.is_absolute():
        return fail("--receipt must be an absolute path")
    package_json = project / "package.json"
    lockfile = project / args.lockfile
    if not package_json.is_file():
        return fail(f"package.json not found: {package_json}")
    if not lockfile.is_file():
        return fail(f"lockfile not found: {lockfile}")
    if args.ignore_scripts and not args.ignore_scripts_reason.strip():
        return fail("--ignore-scripts needs an explicit --ignore-scripts-reason")
    if not scratch_root.is_dir():
        return fail(f"scratch root does not exist: {scratch_root}")

    for candidate, root, what in (
        (cache_dir, project, "owned cache --cache-dir"),
        (project, cache_dir, "project --project"),
        (cache_dir, scratch_root, "owned cache --cache-dir"),
        (scratch_root, cache_dir, "scratch root --scratch-root"),
        (scratch_root, project, "scratch root --scratch-root"),
        (project, scratch_root, "project --project"),
        (receipt_path.parent, project, "receipt parent"),
        (receipt_path.parent, cache_dir, "receipt parent"),
    ):
        reason = refused_inside(candidate, root, what)
        if reason:
            return fail(reason)

    cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        npm_version_proc = run_npm(args.npm, ["--version"], project, npm_child_env())
        npm_version = npm_version_proc.stdout.strip() or npm_version_proc.stderr.strip()
    except (OSError, FileNotFoundError):
        return fail(f"npm binary not runnable: {args.npm}")

    lock_sha = sha256_file(lockfile)
    manifest_sha = sha256_file(package_json)
    if args.ignore_scripts:
        npm_args = ["ci", "--offline", "--no-audit", "--no-fund",
                    "--cache", str(cache_dir), "--ignore-scripts"]
    else:
        npm_args = ["ci", "--offline", "--no-audit", "--no-fund",
                    "--cache", str(cache_dir), "--ignore-scripts=false"]
    child_env = npm_child_env()

    work = Path(tempfile.mkdtemp(prefix="dell-npm-probe-", dir=str(scratch_root)))
    try:
        shutil.copytree(project, work, symlinks=True, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(".npmrc", "node_modules", ".git"))
        proc = run_npm(args.npm, npm_args, work, child_env)
    except (OSError, FileNotFoundError) as error:
        shutil.rmtree(work, ignore_errors=True)
        return fail(f"npm run failed: {error}")
    shutil.rmtree(work, ignore_errors=True)

    stderr = proc.stderr or ""
    if proc.returncode == 0:
        verdict, next_action = "ready", "cache covers this lockfile; re-run after any lock change"
        reason = "npm ci --offline succeeded against the owned cache"
    else:
        verdict, reason = classify(stderr)
        next_action = {
            "cache-miss": "fill the owned cache online, then re-probe offline",
            "sourcing-limited": "resolve the non-cacheable source per the reference, then re-probe",
            "failed": "fix the npm error and re-probe; do not claim offline-readiness",
        }[verdict]

    receipt = {
        "schema": SCHEMA,
        "verdict": verdict,
        "reason": reason,
        "nextAction": next_action,
        "npmVersion": npm_version,
        "npmArgv": ["npm", *npm_args],
        "ignoreScripts": bool(args.ignore_scripts),
        "ignoreScriptsReason": args.ignore_scripts_reason if args.ignore_scripts else "",
        "project": str(project),
        "manifest": str(package_json),
        "manifestSha256": manifest_sha,
        "lockfile": str(lockfile),
        "lockSha256": lock_sha,
        "cacheDir": str(cache_dir),
        "exitCode": proc.returncode,
        "missing": missing_specs(stderr) if verdict == "cache-miss" else [],
        "stderrExcerpt": stderr.strip()[-DETAIL_MAX:] if stderr.strip() else "",
    }
    write_json_atomic(Path(args.receipt), receipt)
    print(json.dumps({"schema": SCHEMA, "verdict": verdict,
                      "lockSha256": lock_sha, "manifestSha256": manifest_sha,
                      "exitCode": proc.returncode}))
    return 0 if verdict == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
