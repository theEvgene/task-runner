"""Unit checks for the Dell batch dependency restoration (no npm, no queue).

Covers classification policy, integrity verification, the Lenovo fill
driver, engines policy, and refusal paths of the orchestrator that need
no network. Share containment and related hardening live in
test_dell_deps_hardening.py; npm-backed behaviour in
test_dell_deps_restore.py.
"""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts" / "dell_lan"
sys.path.insert(0, str(SCRIPTS))

import dell_deps_lib as lib
import lenovo_deps_fill as fill

PY = sys.executable
RESTORE = SCRIPTS / "dell_deps_restore.py"


def integrity_of(data: bytes, algo: str = "sha512") -> str:
    digest = hashlib.new(algo, data).digest()
    return f"{algo}-{base64.b64encode(digest).decode('ascii')}"


def lock_entry(resolved: str, data: bytes, version: str = "1.0.0") -> dict:
    return {"version": version, "resolved": resolved,
            "integrity": integrity_of(data)}


class ClassifyTests(unittest.TestCase):
    PUB = "https://registry.npmjs.org/pub/-/pub-1.0.0.tgz"
    CORP = "https://corp.invalid/priv/-/priv-1.0.0.tgz"
    PROXY_PUB = "https://proxy.invalid/pub/-/pub-1.0.0.tgz"

    def test_public_vs_private_split(self) -> None:
        data = b"x"
        packages = {"": {}, "node_modules/pub": lock_entry(self.PUB, data),
                    "node_modules/priv": lock_entry(self.CORP, data)}
        buckets = lib.classify_lock(packages, "registry.npmjs.org")
        self.assertEqual([n["name"] for n in buckets["public"]], ["pub"])
        self.assertEqual([n["name"] for n in buckets["private"]], ["priv"])
        self.assertEqual(buckets["unknown"], [])
        self.assertEqual(buckets["limited"], [])

    def test_non_public_host_is_private(self) -> None:
        data = b"x"
        packages = {"": {}, "node_modules/pub": lock_entry(self.PROXY_PUB, data)}
        buckets = lib.classify_lock(packages, "registry.npmjs.org")
        self.assertEqual(buckets["public"], [])
        self.assertEqual(len(buckets["private"]), 1)

    def test_unknown_is_never_public(self) -> None:
        packages = {"": {},
                    "node_modules/a": {"version": "1.0.0"},
                    "node_modules/b": {"version": "1.0.0",
                                       "resolved": self.PUB},
                    "node_modules/c": {"version": "1.0.0",
                                       "resolved": self.PUB,
                                       "integrity": "bogus"},
                    "node_modules/d": {"version": "1.0.0",
                                       "resolved": "git+ssh://x/y.git",
                                       "integrity": integrity_of(b"d")}}
        buckets = lib.classify_lock(packages, "registry.npmjs.org")
        self.assertEqual(len(buckets["unknown"]), 3)
        self.assertEqual(len(buckets["limited"]), 1)
        self.assertEqual(buckets["public"], [])

    def test_git_and_bundled_are_limited(self) -> None:
        data = b"x"
        packages = {"": {},
                    "node_modules/g": {"version": "1.0.0",
                                       "resolved": "git+https://x/y.git",
                                       "integrity": integrity_of(data)},
                    "node_modules/h": {"version": "1.0.0",
                                       "resolved": self.PUB,
                                       "integrity": integrity_of(data),
                                       "bundled": True}}
        buckets = lib.classify_lock(packages, "registry.npmjs.org")
        self.assertEqual(len(buckets["limited"]), 2)

    def test_local_links_pass_through(self) -> None:
        packages = {"": {},
                    "node_modules/a": {"version": "1.0.0",
                                       "resolved": "packages/a", "link": True}}
        buckets = lib.classify_lock(packages, "registry.npmjs.org")
        self.assertEqual(len(buckets["local"]), 1)


class IntegrityTests(unittest.TestCase):
    def test_round_trip_and_tamper(self) -> None:
        data = b'{"name":"p"}'
        integrity = integrity_of(data)
        self.assertTrue(lib.check_integrity_bytes(data, integrity))
        self.assertFalse(lib.check_integrity_bytes(data + b"!", integrity))
        self.assertFalse(lib.check_integrity_bytes(data, "sha512-bogus!!"))
        self.assertFalse(lib.check_integrity_bytes(data, ""))


class EnginesTests(unittest.TestCase):
    def test_engines_refusal_and_pass(self) -> None:
        checked, error = lib.check_engines({"engines": {"node": ">=999.0.0"}},
                                           "v24.18.0", "11.16.0")
        self.assertIsNotNone(error)
        self.assertFalse(checked["node"]["satisfied"])
        checked, error = lib.check_engines({"engines": {"node": ">=18.0.0"}},
                                           "v24.18.0", "11.16.0")
        self.assertIsNone(error)

    def test_short_specs_accept_satisfying_toolchain(self) -> None:
        cases = [("v24.18.0", (">=18", ">=18.0.0 <25.0.0", "^24", "~24",
                                "24", "24.18", "24.x")),
                 ("v18.2.7", ("18", "18.2", "^18", "~18", "~18.2", ">=18"))]
        for installed, specs in cases:
            for spec in specs:
                checked, error = lib.check_engines({"engines": {"node": spec}},
                                                   installed, "11.16.0")
                self.assertIsNone(error, msg=f"{installed} vs {spec}")
                self.assertTrue(checked["node"]["satisfied"],
                                msg=f"{installed} vs {spec}")

    def test_short_specs_still_refuse_violations(self) -> None:
        checked, error = lib.check_engines({"engines": {"node": ">=99"}},
                                           "v24.18.0", "11.16.0")
        self.assertIsNotNone(error)
        checked, error = lib.check_engines({"engines": {"node": "18"}},
                                           "v24.18.0", "11.16.0")
        self.assertIsNotNone(error)
        self.assertFalse(checked["node"]["satisfied"])

    def test_unsupported_range_is_explicit(self) -> None:
        ok, why = lib.satisfy_range("1.2.3", "a || b")
        self.assertFalse(ok)
        self.assertIn("||", why)
        ok, why = lib.satisfy_range("1.2.3", ">=1.x")
        self.assertFalse(ok)


class WorkspaceLinkTests(unittest.TestCase):
    def test_escaping_link_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            packages = {"": {}, "node_modules/a": {"link": True,
                        "resolved": "https://corp.invalid/a.tgz"}}
            self.assertIsNotNone(lib.scan_workspace_links(packages, project))


class FillDriverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="dell-deps-fill-test-"))
        self.source = self.root / "source"
        self.source.mkdir()
        self.shared = self.root / "shared"
        self.shared.mkdir()
        self.data = b"fake-tarball-bytes-for-tests"
        self.integrity = integrity_of(self.data)
        (self.source / "priv-1.0.0.tgz").write_bytes(self.data)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def write_needs(self, entries: list[dict], token: str = "tok") -> tuple[Path, str]:
        needs = {"schema": "dell-deps-needs/1", "token": token,
                 "lockSha256": "abc", "packages": entries}
        text = json.dumps(needs, separators=(",", ":")) + "\n"
        path = self.shared / f"deps-needs-{token}.json"
        path.write_bytes(text.encode("utf-8"))
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return path, digest

    def entry(self, name: str = "priv", version: str = "1.0.0",
              integrity: str | None = None) -> dict:
        return {"name": name, "version": version,
                "resolved": f"https://corp.invalid/{name}.tgz",
                "integrity": integrity or self.integrity}

    def test_happy_path(self) -> None:
        path, digest = self.write_needs([self.entry()])
        request = fill.validated(path, digest)
        manifest = fill.pack(request, path, self.source, "deps-export-tok.json")
        self.assertEqual(manifest["token"], "tok")
        tarball = self.shared / manifest["files"][0]["tarball"]
        self.assertEqual(tarball.read_bytes(), self.data)

    def test_scoped_package_packs(self) -> None:
        scoped = b"scoped-tarball-bytes"
        (self.source / "@scope-pkg-1.0.0.tgz").write_bytes(scoped)
        entry = self.entry(name="@scope/pkg",
                           integrity=integrity_of(scoped))
        path, digest = self.write_needs([entry])
        manifest = fill.pack(fill.validated(path, digest), path, self.source,
                             "deps-export-tok.json")
        self.assertEqual(manifest["files"][0]["name"], "@scope/pkg")
        tarball = self.shared / manifest["files"][0]["tarball"]
        self.assertEqual(tarball.read_bytes(), scoped)

    def test_traversal_name_refused(self) -> None:
        for bad in ("../evil", "a/b/c", "..", "C:\\evil"):
            path, digest = self.write_needs([self.entry(name=bad)])
            with self.assertRaises(fill.Stop, msg=bad):
                fill.pack(fill.validated(path, digest), path, self.source,
                          "deps-export-tok.json")

    def test_tampered_source_refused(self) -> None:
        (self.source / "priv-1.0.0.tgz").write_bytes(b"tampered")
        path, digest = self.write_needs([self.entry()])
        with self.assertRaises(fill.Stop):
            fill.pack(fill.validated(path, digest), path, self.source,
                      "deps-export-tok.json")

    def test_missing_package_refused(self) -> None:
        path, digest = self.write_needs([self.entry(name="ghost")])
        with self.assertRaises(fill.Stop):
            fill.pack(fill.validated(path, digest), path, self.source,
                      "deps-export-tok.json")

    def test_digest_mismatch_refused(self) -> None:
        path, _ = self.write_needs([self.entry()])
        with self.assertRaises(fill.Stop):
            fill.validated(path, "0" * 64)

    def test_unsafe_out_name_refused(self) -> None:
        proc = subprocess.run(
            [PY, str(SCRIPTS / "lenovo_deps_fill.py"), "--request", "x",
             "--request-sha256", "y", "--source-dir", str(self.source),
             "--out-name", "../evil.json"],
            capture_output=True, text=True, check=False)
        self.assertNotEqual(proc.returncode, 0)


class RefusalPathTests(unittest.TestCase):
    def run_restore(self, project: Path, cache: Path, scratch: Path,
                    receipt: Path, extra: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            [PY, str(RESTORE), "--project", str(project),
             "--cache-dir", str(cache), "--scratch-root", str(scratch),
             "--receipt", str(receipt), "--npm", "npm", "--node", "node",
             "--public-registry", "https://registry.npmjs.org", *extra],
            capture_output=True, text=True, check=False)

    def make_project(self, root: Path, name: str, manifest: dict,
                     lock: dict) -> Path:
        project = root / name
        project.mkdir(parents=True, exist_ok=True)
        (project / "package.json").write_text(json.dumps(manifest),
                                              encoding="utf-8")
        (project / "package-lock.json").write_text(json.dumps(lock),
                                                   encoding="utf-8")
        return project

    def base_manifest(self, extra: dict | None = None) -> dict:
        manifest = {"name": "t", "version": "1.0.0",
                    "scripts": {"test": "node -e \"process.exit(0)\""}}
        if extra:
            manifest.update(extra)
        return manifest

    def base_lock(self, packages: dict) -> dict:
        full = {"": {"name": "t", "version": "1.0.0"}}
        full.update(packages)
        return {"name": "t", "version": "1.0.0", "lockfileVersion": 3,
                "requires": True, "packages": full}

    def test_unknown_classification(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dell-deps-refuse-") as tmp:
            root = Path(tmp)
            scratch = root / "scratch"
            scratch.mkdir()
            project = self.make_project(
                root, "proj", self.base_manifest(),
                self.base_lock({"node_modules/mystery": {"version": "1.0.0"}}))
            receipt = root / "r.json"
            proc = self.run_restore(project, root / "cache", scratch, receipt, [])
            self.assertEqual(proc.returncode, 1)
            data = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertEqual(data["verdict"], "unknown-classification")

    def test_engines_refusal_invalidates_stale_receipt(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dell-deps-refuse-") as tmp:
            root = Path(tmp)
            scratch = root / "scratch"
            scratch.mkdir()
            project = self.make_project(
                root, "proj",
                self.base_manifest({"engines": {"node": ">=999.0.0"}}),
                self.base_lock({}))
            receipt = root / "r.json"
            receipt.write_text(json.dumps({"schema": "x", "verdict": "ready"}),
                               encoding="utf-8")
            proc = self.run_restore(project, root / "cache", scratch, receipt, [])
            self.assertEqual(proc.returncode, 2)
            # No stale success may survive: the receipt is removed, never
            # left advertising a previous ready.
            self.assertFalse(receipt.exists())

    def test_transfer_modules_have_no_network_transport(self) -> None:
        bodies = {name: (SCRIPTS / name).read_text(encoding="utf-8")
                  for name in ("dell_deps_restore.py", "dell_deps_transfer.py",
                               "dell_deps_lib.py", "lenovo_deps_fill.py")}
        for name, text in bodies.items():
            for handle in ("requests.", "urlopen(", "http.client",
                           "create_connection", "0.0.0.0", "paramiko",
                           "urllib.request"):
                self.assertNotIn(handle, text, msg=f"{name}: {handle}")
        self.assertIn("127.0.0.1", bodies["dell_deps_lib.py"])

    def test_source_mutation_node_modules_refuses_with_source_changed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dell-deps-refuse-") as tmp:
            root = Path(tmp)
            scratch = root / "scratch"
            scratch.mkdir()
            project = self.make_project(
                root, "proj", self.base_manifest(),
                self.base_lock({}))
            (project / "node_modules").mkdir()
            lock_before = (project / "package-lock.json").read_bytes()
            receipt = root / "r.json"
            proc = self.run_restore(project, root / "cache", scratch, receipt, [])
            data = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(data["verdict"], "source-changed")
            self.assertEqual(data["lockSha256"],
                             hashlib.sha256(lock_before).hexdigest())

    def test_offline_strict_policy_in_receipt(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dell-deps-refuse-") as tmp:
            root = Path(tmp)
            scratch = root / "scratch"
            scratch.mkdir()
            project = self.make_project(
                root, "proj", self.base_manifest(),
                self.base_lock({}))
            lock_before = (project / "package-lock.json").read_bytes()
            receipt = root / "r.json"
            proc = self.run_restore(project, root / "cache", scratch, receipt, [])
            data = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertEqual(proc.returncode, 0, msg=proc.stderr[-2000:])
            self.assertEqual(data["verdict"], "ready")
            self.assertIn("--offline", data["npmArgv"])
            self.assertIn("--strict-allow-scripts", data["npmArgv"])
            self.assertTrue(data["lockUnchanged"])
            self.assertEqual((project / "package-lock.json").read_bytes(),
                             lock_before)


class LockfileCompatTests(unittest.TestCase):
    def test_v3_needs_npm9(self) -> None:
        self.assertIsNotNone(lib.check_lockfile_compat(3, "8.19.0"))
        self.assertIsNone(lib.check_lockfile_compat(3, "9.0.0"))
        self.assertIsNone(lib.check_lockfile_compat(3, "11.16.0"))

    def test_v2_needs_npm7(self) -> None:
        self.assertIsNotNone(lib.check_lockfile_compat(2, "6.14.0"))
        self.assertIsNone(lib.check_lockfile_compat(2, "7.0.0"))

    def test_v1_and_missing_pass(self) -> None:
        self.assertIsNone(lib.check_lockfile_compat(1, "6.14.0"))
        self.assertIsNone(lib.check_lockfile_compat(None, "6.14.0"))
        self.assertIsNone(lib.check_lockfile_compat("3", "8.0.0"))


if __name__ == "__main__":
    unittest.main()
