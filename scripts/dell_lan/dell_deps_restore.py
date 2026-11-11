#!/usr/bin/env python3
"""One-call batch restoration of Dell npm dependencies with a signed transfer.

Evaluates one lockfile against one owned npm cache, classifies every locked
entry on verifiable data, fetches public tarballs from the named public
registry on Dell, pulls closed packages in ONE batch through the existing
signed LAN queue (Lenovo ships bytes; Dell re-verifies every byte against the
lock), and finishes with a true ``npm ci --offline`` plus the project's own
tests in a disposable copy. The source project and its lockfile stay
byte-identical; there is no resume from a stale receipt, every run re-proves.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import dell_deps_lib as lib
import dell_npm_probe
from dell_lan_common import (
    DETAIL_MAX, Stop, containment, sha256_bytes,
    sha256_file, write_json_atomic,
)

SCHEMA = "dell-deps-restore/1"


def invalidate_receipt(receipt_path: Path) -> None:
    """Remove a prior receipt so a refusal never leaves a stale success behind."""

    try:
        receipt_path.unlink(missing_ok=True)
    except OSError:
        pass


def refused_inside(candidate: Path, root: Path, what: str) -> str | None:
    try:
        verdict = containment(candidate, root)
    except Exception as error:
        return f"{what} containment is unprovable: {error}"
    if verdict is None:
        return f"{what} containment is unprovable; refusing"
    if verdict:
        return f"{what} must stay outside {root}"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dell_deps_restore",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("--project", required=True)
    parser.add_argument("--lockfile", default="package-lock.json")
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--scratch-root", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--npm", default="npm")
    parser.add_argument("--node", default="node")
    parser.add_argument("--public-registry", default="https://registry.npmjs.org")
    parser.add_argument("--stub-port", type=int, default=12771)
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--skip-tests-reason", default="")
    parser.add_argument("--ignore-scripts", action="store_true")
    parser.add_argument("--ignore-scripts-reason", default="")
    parser.add_argument("--queue", default="")
    parser.add_argument("--key-file", default="")
    parser.add_argument("--shared", default="")
    parser.add_argument("--lenovo-python", default="")
    parser.add_argument("--lenovo-driver-dir", default="")
    parser.add_argument("--lenovo-source-dir", default="")
    parser.add_argument("--token", default="")
    parser.add_argument("--fetch-timeout", type=float, default=600.0)
    parser.add_argument("--fetch-poll", type=float, default=2.0)
    args = parser.parse_args(argv)

    project = Path(args.project)
    cache_dir = Path(args.cache_dir)
    scratch_root = Path(args.scratch_root)
    receipt_path = Path(args.receipt)

    def fail(message: str) -> int:
        # Every refusal path clears the receipt first: a prior success must
        # never survive a failed run (no resume from a stale receipt).
        invalidate_receipt(receipt_path)
        print(f"deps restore refused: {message}", file=sys.stderr)
        return 2

    # Crash safety: a prior success must not survive a run that never
    # reaches a terminal verdict (exception, signal, kill). Delete any
    # prior receipt at start so an interrupted run leaves no receipt
    # (absent, never a stale ready); re-run is required, no resume.
    invalidate_receipt(receipt_path)

    for value, what in ((args.project, "--project"), (args.cache_dir, "--cache-dir"),
                        (args.scratch_root, "--scratch-root"),
                        (args.receipt, "--receipt")):
        if not Path(value).is_absolute():
            return fail(f"{what} must be an absolute path")
    package_json = project / "package.json"
    lockfile = project / args.lockfile
    if not package_json.is_file():
        return fail(f"package.json not found: {package_json}")
    if not lockfile.is_file():
        return fail(f"lockfile not found: {lockfile}")
    if args.skip_tests and not args.skip_tests_reason.strip():
        return fail("--skip-tests needs an explicit --skip-tests-reason")
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
            (receipt_path.parent, cache_dir, "receipt parent")):
        reason = refused_inside(candidate, root, what)
        if reason:
            return fail(reason)

    def done(verdict: str, code: int, extra: dict) -> int:
        write_json_atomic(receipt_path, {"schema": SCHEMA, "verdict": verdict,
                                         "lockSha256": lock_sha,
                                         "manifestSha256": manifest_sha, **extra})
        print(json.dumps({"schema": SCHEMA, "verdict": verdict}))
        return code

    public_host = (urlparse(args.public_registry).hostname or "").lower()
    if not public_host:
        return fail("--public-registry must be a URL with a host")
    npm_argv = lib.resolve_npm(args.npm, args.node)
    child_env = dell_npm_probe.npm_child_env()
    lock_bytes = lockfile.read_bytes()
    lock_sha = sha256_bytes(lock_bytes)
    manifest_bytes = package_json.read_bytes()
    manifest_sha = sha256_bytes(manifest_bytes)
    try:
        lock = json.loads(lock_bytes.decode("utf-8"))
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return fail("package.json or lockfile is not valid JSON")
    if not isinstance(lock, dict) or not isinstance(lock.get("packages"), dict):
        return fail("lockfile has no packages table")

    node_bin = shutil.which(args.node) or args.node
    node_proc = lib.run_command([node_bin, "--version"], project, child_env)
    npm_proc = lib.run_command(npm_argv + ["--version"], project, child_env)
    node_version = node_proc.stdout.strip()
    npm_version = (npm_proc.stdout or npm_proc.stderr).strip()
    if node_proc.returncode != 0 or npm_proc.returncode != 0:
        return fail("node or npm binary is not runnable")
    engines_checked, engines_error = lib.check_engines(manifest, node_version,
                                                          npm_version)
    if engines_error:
        return fail(engines_error)
    lockfile_version = lock.get("lockfileVersion")
    lockfile_error = lib.check_lockfile_compat(lockfile_version, npm_version)
    if lockfile_error:
        return fail(lockfile_error)

    link_error = lib.scan_workspace_links(lock["packages"], project)
    if link_error:
        return fail(link_error)

    buckets = lib.classify_lock(lock["packages"], public_host)
    if buckets["unknown"]:
        return done("unknown-classification", 1, {
            "reason": "entries with no verifiable source are never declared public",
            "nextAction": "give each entry a public URL with integrity or a "
                          "transferable corporate tarball; then re-run",
            "unknown": buckets["unknown"][:20]})
    if buckets["limited"]:
        return done("sourcing-limited", 1, {
            "reason": "git/bundled sources cannot travel the tarball batch",
            "nextAction": "vendor the source outside the cache, or pin a registry "
                          "tarball with integrity; then re-run",
            "limited": buckets["limited"][:20]})
    allow_pins = {f"{n['name']}@{n['version']}": True
                  for n in (*buckets["public"], *buckets["private"])}
    if args.ignore_scripts:
        allow_pins = {}

    cache_dir.mkdir(parents=True, exist_ok=True)
    fingerprint_before = lib.cache_fingerprint(cache_dir)
    # The stub binds before the probe so the rewritten lock (and its cache
    # keys) stays stable across runs on the fixed port; without private
    # entries there is no stub and no rewrite at all.
    stub = lib.LocalStub({}, args.stub_port if buckets["private"] else 0)
    if buckets["private"]:
        stub.start()
    rewrite_map = {n["resolved"]: f"http://127.0.0.1:{stub.port}"
                   f"{lib.stub_path(n['name'], n['version'])}"
                   for n in buckets["private"]}
    scratch_lock_text, scratch_manifest_text = lib.build_scratch_texts(
        lock_bytes, manifest_bytes, rewrite_map,
        allow_pins if not args.ignore_scripts else {})
    scratch_lock_sha = sha256_bytes(scratch_lock_text.encode("utf-8"))

    def prepare_copy(dest: Path) -> None:
        lib.copy_project(project, dest)
        (dest / args.lockfile).write_text(scratch_lock_text, encoding="utf-8")
        (dest / "package.json").write_text(scratch_manifest_text, encoding="utf-8")

    probe_work = Path(tempfile.mkdtemp(prefix="dell-deps-probe-", dir=str(scratch_root)))
    try:
        prepare_copy(probe_work)
        probe_proc = lib.run_command(
            npm_argv + ["ci", "--offline", "--no-audit", "--no-fund",
                        "--cache", str(cache_dir), "--ignore-scripts=false"],
            probe_work, child_env)
    finally:
        shutil.rmtree(probe_work, ignore_errors=True)
    probe_stderr = probe_proc.stderr or ""
    if probe_proc.returncode == 0:
        probe_verdict, missing = "ready", []
    else:
        probe_verdict, _ = dell_npm_probe.classify(probe_stderr)
        missing = dell_npm_probe.missing_specs(probe_stderr) \
            if probe_verdict == "cache-miss" else []

    transfer: dict = {"attempted": False}
    verified: dict[str, bytes] = {}
    if probe_verdict == "failed":
        stub.stop()
        return done("failed", 1, {
            "reason": "npm reported an error that is neither a cache miss nor a "
                      "known sourcing limit",
            "nextAction": "fix the npm error and re-run",
            "exitCode": probe_proc.returncode,
            "stderrExcerpt": probe_stderr.strip()[-DETAIL_MAX:]})
    if probe_verdict != "ready":
        by_rewritten = {rewrite_map.get(n["resolved"], n["resolved"]): n
                        for n in buckets["private"]}
        wanted = [by_rewritten[url] for url in
                  {u for item in missing for u in [item] if u in by_rewritten}
                  ] if missing else list(buckets["private"])
        # Fall back to the name match for npm's short package mentions.
        if missing and not wanted:
            wanted = [n for n in buckets["private"]
                      if any(n["name"] in item or n["resolved"] in item
                             for item in missing)] or list(buckets["private"])
        if wanted and not (args.queue and args.key_file and args.shared
                           and args.lenovo_python and args.lenovo_driver_dir
                           and args.lenovo_source_dir):
            stub.stop()
            return done("transfer-unconfigured", 1, {
                "reason": "the cache misses closed packages but no signed-queue "
                          "arguments were given",
                "nextAction": "pass --queue/--key-file/--shared plus the Lenovo paths; "
                              "then re-run",
                "missingPrivate": [f"{n['name']}@{n['version']}" for n in wanted]})
        if wanted:
            for value, what in ((args.queue, "--queue"), (args.shared, "--shared"),
                                (args.lenovo_python, "--lenovo-python"),
                                (args.lenovo_driver_dir, "--lenovo-driver-dir"),
                                (args.lenovo_source_dir, "--lenovo-source-dir")):
                if not Path(value).is_absolute():
                    stub.stop()
                    return fail(f"{what} must be an absolute path")
            import dell_deps_transfer as transfer_mod
            try:
                transfer, verified, batch_failure = transfer_mod.run_batch(
                    args, lock_sha=lock_sha, manifest_sha=manifest_sha,
                    wanted=wanted)
            except FileExistsError as error:
                stub.stop()
                return fail(str(error))
            except Stop as stop:
                stub.stop()
                return done(stop.verdict if stop.verdict != "mismatch" else "transfer-refused",
                            stop.code if stop.code in (1, 3) else 1, {
                                "reason": stop.reason,
                                "nextAction": "fix the queue/request inputs and re-run "
                                              "with a fresh token",
                                "detail": stop.detail,
                                "transfer": {"attempted": False}})
            if batch_failure is not None:
                stub.stop()
                verdict = batch_failure.pop("verdict")
                code = batch_failure.pop("code")
                return done(verdict, code, batch_failure)
    # The public host is the only host Dell ever fetches; anything else
    # stops the run before npm sees it.
    guard_failed = [n["resolved"] for n in buckets["public"]
                    if (urlparse(n["resolved"]).hostname or "").lower() != public_host]
    if guard_failed:
        stub.stop()
        return done("corporate-fetch-refused", 1, {
            "reason": "a supposedly public entry points outside the public registry",
            "nextAction": "reclassify it as private (queue transfer) or fix the lock",
            "refused": guard_failed[:10]})

    serve = {lib.stub_path(n["name"], n["version"]): verified[f"{n['name']}@{n['version']}"]
             for n in buckets["private"]
             if f"{n['name']}@{n['version']}" in verified}
    if serve:
        stub.stop()
        stub = lib.LocalStub(serve, args.stub_port)
        stub.start()
        for need in buckets["private"]:
            label = f"{need['name']}@{need['version']}"
            if label in verified:
                rewrite_map[need["resolved"]] = \
                    f"http://127.0.0.1:{stub.port}{lib.stub_path(need['name'], need['version'])}"
        scratch_lock_text, scratch_manifest_text = lib.build_scratch_texts(
            lock_bytes, manifest_bytes, rewrite_map,
            allow_pins if not args.ignore_scripts else {})
        scratch_lock_sha = sha256_bytes(scratch_lock_text.encode("utf-8"))
    fill_argv = ["ci", "--prefer-offline", "--no-audit", "--no-fund",
                 "--cache", str(cache_dir)]
    final_argv = ["ci", "--offline", "--no-audit", "--no-fund",
                  "--cache", str(cache_dir)]
    if args.ignore_scripts:
        fill_argv.append("--ignore-scripts")
        final_argv.append("--ignore-scripts")
    else:
        fill_argv += ["--ignore-scripts=false", "--strict-allow-scripts"]
        final_argv += ["--ignore-scripts=false", "--strict-allow-scripts"]
    try:
        if probe_verdict != "ready":
            fill_work = Path(tempfile.mkdtemp(prefix="dell-deps-fill-", dir=str(scratch_root)))
            try:
                prepare_copy(fill_work)
                fill_proc = lib.run_command(npm_argv + fill_argv, fill_work, child_env)
            finally:
                shutil.rmtree(fill_work, ignore_errors=True)
            if fill_proc.returncode != 0:
                return done("fill-failed", 1, {
                    "reason": "the explicitly networked fill step failed; the owned "
                              "cache still does not cover the lock",
                    "nextAction": "fix the reported fetch and re-run",
                    "exitCode": fill_proc.returncode,
                    "stderrExcerpt": (fill_proc.stderr or "").strip()[-DETAIL_MAX:],
                    "transfer": transfer})
    finally:
        stub_requests = stub.stop() if buckets["private"] else []

    final_work = Path(tempfile.mkdtemp(prefix="dell-deps-final-", dir=str(scratch_root)))
    try:
        prepare_copy(final_work)
        final_proc = lib.run_command(npm_argv + final_argv, final_work, child_env)
        final_stderr = (final_proc.stderr or "").strip()[-DETAIL_MAX:]
        tests: dict = {"ran": False, "exitCode": None}
        if final_proc.returncode == 0 and not args.skip_tests:
            test_proc = lib.run_command(npm_argv + ["test"], final_work, child_env)
            tests = {"ran": True, "exitCode": test_proc.returncode,
                     "stderrExcerpt": (test_proc.stderr or "").strip()[-DETAIL_MAX:],
                     "stdoutExcerpt": (test_proc.stdout or "").strip()[-DETAIL_MAX:]}
    finally:
        shutil.rmtree(final_work, ignore_errors=True)

    fingerprint_after = lib.cache_fingerprint(cache_dir)
    if (sha256_file(lockfile) != lock_sha
            or sha256_file(package_json) != manifest_sha
            or (project / "node_modules").exists()):
        return done("source-changed", 1, {
            "reason": "the source project changed during the run",
            "nextAction": "restore the pristine lockfile and re-run"})
    if final_proc.returncode != 0:
        return done("install-failed", 1, {
            "reason": "the final offline install failed",
            "nextAction": "fix the reported error and re-run",
            "exitCode": final_proc.returncode, "stderrExcerpt": final_stderr,
            "transfer": transfer, "rewriteMap": rewrite_map,
            "stubRequests": stub_requests[:50],
            "cacheFingerprint": fingerprint_after,
            "nodeVersion": node_version, "npmVersion": npm_version})
    if tests.get("ran") and tests.get("exitCode") != 0:
        return done("tests-failed", 1, {
            "reason": "dependencies installed offline, but the project tests failed",
            "nextAction": "fix the project tests and re-run",
            "tests": tests, "transfer": transfer, "rewriteMap": rewrite_map,
            "cacheFingerprint": fingerprint_after,
            "nodeVersion": node_version, "npmVersion": npm_version})
    return done("ready" if not args.skip_tests else "ready-tests-skipped", 0, {
        "reason": "a fresh offline install and the project tests succeeded in this "
                  "run" if not args.skip_tests else
                  "a fresh offline install succeeded; tests skipped with reason",
        "project": str(project), "lockfileVersion": lockfile_version,
        "lockUnchanged": True, "projectUntouched": True,
        "cacheDir": str(cache_dir),
        "cacheFingerprintBefore": fingerprint_before,
        "cacheFingerprint": fingerprint_after,
        "nodeVersion": node_version, "npmVersion": npm_version,
        "engines": engines_checked, "publicRegistry": args.public_registry,
        "classification": {k: [f"{n.get('name', n.get('key'))}"
                               f"@{n.get('version', '')}".rstrip("@") for n in v]
                           for k, v in buckets.items()},
        "probeVerdict": probe_verdict, "missing": missing[:20],
        "transfer": transfer, "rewriteMap": rewrite_map,
        "scratchLockSha256": scratch_lock_sha,
        "stubRequests": stub_requests[:50],
        "npmArgv": ["npm", *final_argv],
        "ignoreScripts": bool(args.ignore_scripts),
        "ignoreScriptsReason": args.ignore_scripts_reason if args.ignore_scripts else "",
        "allowScripts": allow_pins,
        "tests": tests if not args.skip_tests else
        {"ran": False, "reason": args.skip_tests_reason}})


if __name__ == "__main__":
    raise SystemExit(main())
