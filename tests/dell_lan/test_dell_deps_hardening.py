"""Hardening checks for the Dell batch dependency restoration (no npm, no queue).

Share containment, content-bound cache fingerprints, the npm child
environment, and the public-guard tripwire. General policy checks live in
test_dell_deps_units.py; npm-backed behaviour in test_dell_deps_restore.py.
"""

from __future__ import annotations

import base64
import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts" / "dell_lan"
sys.path.insert(0, str(SCRIPTS))

import dell_deps_lib as lib


def integrity_of(data: bytes, algo: str = "sha512") -> str:
    digest = hashlib.new(algo, data).digest()
    return f"{algo}-{base64.b64encode(digest).decode('ascii')}"


class VerifyExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="dell-deps-verify-test-"))
        self.shared = self.root / "shared"
        self.shared.mkdir()
        self.data = b"verified-tarball"
        self.integrity = integrity_of(self.data)
        self.want = [{"name": "priv", "version": "1.0.0",
                      "resolved": "https://corp.invalid/priv.tgz",
                      "integrity": self.integrity}]

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def stage(self, data: bytes | None = None) -> dict:
        body = self.data if data is None else data
        (self.shared / "deps-tok-0.tgz").write_bytes(body)
        return {"schema": "dell-deps-export/1", "token": "tok",
                "files": [{"name": "priv", "version": "1.0.0",
                           "tarball": "deps-tok-0.tgz",
                           "sha256": hashlib.sha256(body).hexdigest(),
                           "integrity": self.integrity}]}

    def test_verified_ok(self) -> None:
        self.assertEqual(len(lib.verify_export_files(
            self.shared, self.stage(), self.want)), 1)

    def test_swapped_bytes_rejected(self) -> None:
        self.assertIsNone(lib.verify_export_files(
            self.shared, self.stage(b"swapped"), self.want))

    def test_incomplete_batch_rejected(self) -> None:
        export = self.stage()
        export["files"] = []
        self.assertIsNone(lib.verify_export_files(
            self.shared, export, self.want))

    def test_wrong_integrity_label_rejected(self) -> None:
        export = self.stage()
        export["files"][0]["integrity"] = integrity_of(b"other")
        self.assertIsNone(lib.verify_export_files(
            self.shared, export, self.want))

    def test_traversal_tarball_rejected(self) -> None:
        outside = self.root / "evil.bin"
        outside.write_bytes(b"evil-outside-bytes")
        export = self.stage()
        export["files"][0]["tarball"] = "../evil.bin"
        export["files"][0]["sha256"] = hashlib.sha256(
            b"evil-outside-bytes").hexdigest()
        export["files"][0]["integrity"] = integrity_of(b"evil-outside-bytes")
        want = [{"name": "priv", "version": "1.0.0",
                 "resolved": "https://corp.invalid/priv.tgz",
                 "integrity": integrity_of(b"evil-outside-bytes")}]
        self.assertIsNone(lib.verify_export_files(self.shared, export, want))

    def test_absolute_tarball_rejected(self) -> None:
        export = self.stage()
        export["files"][0]["tarball"] = str(self.shared / "deps-tok-0.tgz")
        self.assertIsNone(lib.verify_export_files(
            self.shared, export, self.want))

    def test_symlink_escape_rejected(self) -> None:
        outside = self.root / "real.bin"
        outside.write_bytes(self.data)
        self.stage()
        link = self.shared / "deps-tok-0.tgz"
        link.unlink()
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("symlinks unavailable")
        export = {"schema": "dell-deps-export/1", "token": "tok",
                  "files": [{"name": "priv", "version": "1.0.0",
                             "tarball": "deps-tok-0.tgz",
                             "sha256": hashlib.sha256(self.data).hexdigest(),
                             "integrity": self.integrity}]}
        self.assertIsNone(lib.verify_export_files(
            self.shared, export, self.want))


class PublicGuardTests(unittest.TestCase):
    def test_off_host_entry_is_private_by_construction(self) -> None:
        # The orchestrator's corporate-fetch-refused guard scans the public
        # bucket, but classify_lock only files exact-host entries there, so
        # the guard is a defense-in-depth tripwire that cannot fire today.
        data = b"x"
        packages = {"": {},
                    "node_modules/via-proxy": {
                        "version": "1.0.0",
                        "resolved": "https://proxy.invalid/pub/-/pub-1.0.0.tgz",
                        "integrity": integrity_of(data)}}
        buckets = lib.classify_lock(packages, "registry.npmjs.org")
        guard_failed = [n["resolved"] for n in buckets["public"]
                        if n["resolved"].split("/")[2].lower()
                        != "registry.npmjs.org"]
        self.assertEqual(guard_failed, [])
        self.assertEqual(len(buckets["private"]), 1)


class ChildEnvTests(unittest.TestCase):
    def test_proxy_and_auth_stripped(self) -> None:
        injected = {"HTTP_PROXY": "http://proxy.invalid:8080",
                    "https_proxy": "http://proxy.invalid:8080",
                    "NO_PROXY": "example.com",
                    "NPM_TOKEN": "secret",
                    "npm_config_proxy": "http://proxy.invalid:8080",
                    "npm_config__authToken": "secret",
                    "npm_config_ignore_scripts": "true"}
        saved = dict(os.environ)
        try:
            os.environ.update(injected)
            import dell_npm_probe as probe
            env = probe.npm_child_env()
        finally:
            os.environ.clear()
            os.environ.update(saved)
        for key in injected:
            self.assertNotIn(key, env)
        self.assertIn("PATH", env)


class FingerprintTests(unittest.TestCase):
    def test_content_swap_with_preserved_mtime_changes_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dell-deps-fp-") as tmp:
            cache = Path(tmp)
            target = cache / "entry.bin"
            target.write_bytes(b"A" * 64)
            stamp = target.stat()
            before = lib.cache_fingerprint(cache)
            target.write_bytes(b"B" * 64)
            os.utime(target, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            after = lib.cache_fingerprint(cache)
            self.assertNotEqual(before, after)


class ReceiptCrashTests(unittest.TestCase):
    def test_unexpected_crash_leaves_no_stale_ready(self) -> None:
        import dell_deps_restore as restore

        with tempfile.TemporaryDirectory(prefix="dell-deps-crash-") as tmp:
            root = Path(tmp)
            project = root / "proj"
            project.mkdir()
            (project / "package.json").write_text(
                '{"name":"t","version":"1.0.0"}', encoding="utf-8")
            (project / "package-lock.json").write_text(
                '{"name":"t","version":"1.0.0","lockfileVersion":3,'
                '"requires":true,"packages":{"":{"name":"t",'
                '"version":"1.0.0"}}}', encoding="utf-8")
            scratch = root / "scratch"
            scratch.mkdir()
            receipt = root / "r.json"
            receipt.write_text('{"schema":"x","verdict":"ready"}',
                               encoding="utf-8")
            original = lib.run_command

            def boom(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
                raise RuntimeError("simulated crash before terminal verdict")

            lib.run_command = boom  # type: ignore[method-assign]
            try:
                with self.assertRaises(RuntimeError):
                    restore.main([
                        "--project", str(project),
                        "--cache-dir", str(root / "cache"),
                        "--scratch-root", str(scratch),
                        "--receipt", str(receipt)])
            finally:
                lib.run_command = original  # type: ignore[method-assign]
            # A crash must never leave the prior ready behind: absent,
            # never a stale success. Re-run is required, no resume.
            self.assertFalse(receipt.exists())


if __name__ == "__main__":
    unittest.main()
