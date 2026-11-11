"""npm-backed checks for the Dell batch dependency restoration.

Fixtures are fully local: real tarballs built with ``npm pack``, a
localhost stub as the public registry, a Lenovo source directory, and
the real signed queue with a test worker that executes the single
Lenovo fill command. No corporate network, no new transport.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts" / "dell_lan"
BRIDGE = ROOT / "scripts" / "lan_bridge"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(BRIDGE))

import dell_deps_lib as lib
import lan_bridge

PY = sys.executable
RESTORE = SCRIPTS / "dell_deps_restore.py"


def npm_argv() -> list[str]:
    return lib.resolve_npm("npm", shutil.which("node") or "node")


def pack_fixture_pkg(root: Path, name: str, version: str = "1.0.0") -> bytes:
    pkg = root / f"src-{name}"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "package.json").write_text(json.dumps(
        {"name": name, "version": version, "main": "index.js"}),
        encoding="utf-8")
    (pkg / "index.js").write_text("module.exports = 42;\n", encoding="utf-8")
    proc = subprocess.run(npm_argv() + ["pack", "--silent"], cwd=str(pkg),
                          capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise AssertionError(f"npm pack failed: {proc.stderr[-2000:]}")
    tgz = proc.stdout.strip().splitlines()[-1].strip()
    data = (pkg / tgz).read_bytes()
    (pkg / tgz).unlink()
    return data


def integrity_of(data: bytes) -> str:
    return "sha512-" + base64.b64encode(
        hashlib.sha512(data).digest()).decode("ascii")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SignedQueueWorker:
    """Executes the one Lenovo fill job restore submits, through lan_bridge."""

    def __init__(self, queue: Path, key: bytes, tamper: str = "none") -> None:
        self.queue = queue
        self.key = key
        self.tamper = tamper

    def run_once(self, timeout: float = 100.0) -> str:
        jobs = self.queue / "jobs"
        deadline = time.time() + timeout
        while time.time() < deadline:
            candidates = sorted(jobs.glob("*.json")) if jobs.is_dir() else []
            if candidates:
                return candidates[0].stem
            time.sleep(0.2)
        raise AssertionError("restore submitted no signed job in time")

    def execute(self, job_id: str, shared: Path) -> None:
        raw = lan_bridge.read(str(self.queue / "jobs"), job_id)
        payload = lan_bridge.open_envelope(raw, self.key, "job", job_id)
        proc = subprocess.run(
            payload["command"], shell=True,
            cwd=payload["working_directory"], env=dict(os.environ),
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=payload["timeout_seconds"], check=False)
        if self.tamper == "swap":
            for tarball in shared.glob("deps-*.tgz"):
                tarball.write_bytes(b"swapped-on-the-wire")
        files: dict[str, str] = {}
        for declared in payload.get("return_files") or []:
            path = Path(declared)
            if path.is_file():
                files[str(path)] = base64.b64encode(
                    path.read_bytes()).decode("ascii")
        limit = int(payload["max_output_bytes"])
        result = lan_bridge.build_result_payload(
            job_id, "ok" if proc.returncode == 0 else "error",
            proc.returncode, stdout=proc.stdout[:limit],
            stderr=proc.stderr[:limit], started_at=now_iso(),
            finished_at=now_iso(),
            output_truncated=len(proc.stdout) > limit, files=files)
        lan_bridge.publish(str(self.queue / "results"), job_id,
                           lan_bridge.seal(result, self.key))


class MixedRestoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="dell-deps-e2e-"))
        self.proj = self.root / "proj"
        self.cache = self.root / "cache"
        self.scratch = self.root / "scratch"
        self.shared = self.root / "shared"
        self.queue = self.root / "queue"
        self.source = self.root / "lenovo-source"
        for path in (self.proj, self.cache, self.scratch, self.shared,
                     self.source):
            path.mkdir(parents=True, exist_ok=True)
        self.key_file = self.root / "key.bin"
        self.key_file.write_bytes(os.urandom(32))
        self.key = self.key_file.read_bytes()
        self.pub_data = pack_fixture_pkg(self.root, "dell-pub-fixture")
        self.priv_data = pack_fixture_pkg(self.root, "dell-priv-fixture")
        (self.source / "dell-priv-fixture-1.0.0.tgz").write_bytes(
            self.priv_data)
        self.pub_stub = lib.LocalStub(
            {"/dell-pub-fixture/-/dell-pub-fixture-1.0.0.tgz": self.pub_data})
        self.pub_stub.start()
        self.addCleanup(self.pub_stub.stop)
        self.pub_reg = f"http://127.0.0.1:{self.pub_stub.port}"
        self.write_project()

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def write_project(self) -> None:
        manifest = {
            "name": "mixed", "version": "1.0.0",
            "dependencies": {"dell-pub-fixture": "1.0.0",
                             "dell-priv-fixture": "1.0.0"},
            "scripts": {"test": "node -e \"process.exit(0)\""},
        }
        lock = {
            "name": "mixed", "version": "1.0.0", "lockfileVersion": 3,
            "requires": True,
            "packages": {
                "": {"name": "mixed", "version": "1.0.0",
                     "dependencies": {"dell-pub-fixture": "1.0.0",
                                      "dell-priv-fixture": "1.0.0"}},
                "node_modules/dell-pub-fixture": {
                    "version": "1.0.0",
                    "resolved": f"{self.pub_reg}/dell-pub-fixture/-/"
                                "dell-pub-fixture-1.0.0.tgz",
                    "integrity": integrity_of(self.pub_data)},
                "node_modules/dell-priv-fixture": {
                    "version": "1.0.0",
                    "resolved": "https://deps.invalid/dell-priv-fixture-1.0.0.tgz",
                    "integrity": integrity_of(self.priv_data)},
            },
        }
        (self.proj / "package.json").write_text(json.dumps(manifest),
                                                encoding="utf-8")
        (self.proj / "package-lock.json").write_text(json.dumps(lock),
                                                     encoding="utf-8")
        self.lock_before = (self.proj / "package-lock.json").read_bytes()

    def base_args(self, receipt: Path, token: str) -> list[str]:
        return [
            "--project", str(self.proj), "--cache-dir", str(self.cache),
            "--scratch-root", str(self.scratch), "--receipt", str(receipt),
            "--npm", "npm", "--node", "node",
            "--public-registry", self.pub_reg, "--stub-port", "0",
            "--queue", str(self.queue), "--key-file", str(self.key_file),
            "--shared", str(self.shared), "--lenovo-python", PY,
            "--lenovo-driver-dir", str(SCRIPTS),
            "--lenovo-source-dir", str(self.source), "--token", token,
            "--fetch-timeout", "120", "--fetch-poll", "0.2",
        ]

    def run_with_worker(self, token: str, tamper: str = "none",
                        drop_priv_source: bool = False) -> dict:
        if drop_priv_source:
            (self.source / "dell-priv-fixture-1.0.0.tgz").unlink()
        receipt = self.root / f"receipt-{token}.json"
        outcome: dict = {}
        worker = SignedQueueWorker(self.queue, self.key, tamper=tamper)

        def target() -> None:
            proc = subprocess.run(
                [PY, str(RESTORE), *self.base_args(receipt, token)],
                capture_output=True, text=True, check=False)
            outcome["rc"] = proc.returncode
            outcome["stdout"] = proc.stdout
            outcome["stderr"] = proc.stderr

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        try:
            job_id = worker.run_once()
            worker.execute(job_id, self.shared)
        finally:
            thread.join(timeout=180)
        if thread.is_alive():
            raise AssertionError("restore did not finish after the batch")
        outcome["receipt"] = json.loads(receipt.read_text(encoding="utf-8"))
        return outcome

    def test_mixed_batch_restores_and_runs_tests(self) -> None:
        outcome = self.run_with_worker("a" * 32)
        receipt = outcome["receipt"]
        self.assertEqual(outcome["rc"], 0, msg=outcome["stderr"][-2000:])
        self.assertEqual(receipt["verdict"], "ready")
        self.assertTrue(receipt["transfer"]["attempted"])
        self.assertEqual(receipt["lockSha256"],
                        hashlib.sha256(self.lock_before).hexdigest())
        self.assertEqual((self.proj / "package-lock.json").read_bytes(),
                        self.lock_before)
        self.assertFalse((self.proj / "node_modules").exists())
        self.assertEqual(receipt["tests"]["exitCode"], 0)
        names = receipt["classification"]["private"]
        self.assertIn("dell-priv-fixture@1.0.0", names)
        pubs = receipt["classification"]["public"]
        self.assertIn("dell-pub-fixture@1.0.0", pubs)

    def test_swapped_tarball_never_succeeds(self) -> None:
        outcome = self.run_with_worker("b" * 32, tamper="swap")
        receipt = outcome["receipt"]
        self.assertNotEqual(outcome["rc"], 0)
        self.assertEqual(receipt["verdict"], "integrity-mismatch")
        self.assertEqual((self.proj / "package-lock.json").read_bytes(),
                        self.lock_before)
        self.assertFalse((self.proj / "node_modules").exists())

    def test_missing_closed_package_refuses(self) -> None:
        outcome = self.run_with_worker("c" * 32, drop_priv_source=True)
        receipt = outcome["receipt"]
        self.assertNotEqual(outcome["rc"], 0)
        self.assertEqual(receipt["verdict"], "transfer-refused")
        self.assertEqual((self.proj / "package-lock.json").read_bytes(),
                        self.lock_before)
        self.assertFalse((self.proj / "node_modules").exists())


class FreshnessTests(unittest.TestCase):
    """A changed lock, manifest, or cache never reuses an old success."""

    def run_empty(self, root: Path, receipt_name: str,
                  manifest_patch: dict | None = None,
                  lock_patch: dict | None = None) -> tuple[int, dict]:
        proj = root / "proj"
        if not proj.is_dir():
            proj.mkdir(parents=True)
            (proj / "package.json").write_text(json.dumps(
                {"name": "fresh", "version": "1.0.0",
                 "scripts": {"test": "node -e \"process.exit(0)\""}}),
                encoding="utf-8")
            (proj / "package-lock.json").write_text(json.dumps(
                {"name": "fresh", "version": "1.0.0", "lockfileVersion": 3,
                 "requires": True,
                 "packages": {"": {"name": "fresh", "version": "1.0.0"}}}),
                encoding="utf-8")
        if manifest_patch is not None:
            manifest = json.loads((proj / "package.json").read_text(
                encoding="utf-8"))
            manifest.update(manifest_patch)
            (proj / "package.json").write_text(json.dumps(manifest),
                                               encoding="utf-8")
        if lock_patch is not None:
            lock = json.loads((proj / "package-lock.json").read_text(
                encoding="utf-8"))
            lock["packages"].update(lock_patch)
            (proj / "package-lock.json").write_text(json.dumps(lock),
                                                    encoding="utf-8")
        receipt = root / receipt_name
        proc = subprocess.run(
            [PY, str(RESTORE), "--project", str(proj),
             "--cache-dir", str(root / "cache"),
             "--scratch-root", str(root / "scratch"),
             "--receipt", str(receipt), "--npm", "npm", "--node", "node",
             "--public-registry", "https://registry.npmjs.org"],
            capture_output=True, text=True, check=False)
        return proc.returncode, json.loads(receipt.read_text(
            encoding="utf-8"))

    def test_lock_change_invalidates(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dell-deps-fresh-") as tmp:
            root = Path(tmp)
            (root / "scratch").mkdir()
            rc1, first = self.run_empty(root, "r1.json")
            self.assertEqual(rc1, 0)
            self.assertEqual(first["verdict"], "ready")
            rc2, second = self.run_empty(
                root, "r2.json",
                lock_patch={"node_modules/ghost": {"version": "9.9.9"}})
            self.assertNotEqual(rc2, 0)
            self.assertNotEqual(second["verdict"], "ready")
            self.assertNotEqual(second["lockSha256"], first["lockSha256"])

    def test_manifest_change_invalidates(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dell-deps-fresh-") as tmp:
            root = Path(tmp)
            (root / "scratch").mkdir()
            _, first = self.run_empty(root, "r1.json")
            _, second = self.run_empty(root, "r2.json",
                                       manifest_patch={"description": "new"})
            self.assertNotEqual(second["manifestSha256"],
                                first["manifestSha256"])

    def test_cache_write_changes_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dell-deps-fp-") as tmp:
            cache = Path(tmp)
            before = lib.cache_fingerprint(cache)
            (cache / "entry.bin").write_bytes(os.urandom(64))
            after = lib.cache_fingerprint(cache)
            self.assertNotEqual(before, after)


if __name__ == "__main__":
    unittest.main()
