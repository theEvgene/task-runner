"""Local checks for the Dell npm offline-deps probe.

Everything is disposable: zero-dependency and crafted-miss projects under a
temporary root, an owned cache directory per case, and no network (the probe
runs ``npm ci --offline`` only; the empty project needs no fetch and the miss
case fails before any fetch). The real checkout is never installed into.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts" / "dell_lan"
PROBE = SCRIPTS / "dell_npm_probe.py"

PY = sys.executable
NPM = "npm"


def run_probe(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, str(PROBE), *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )


def write_project(root: Path, name: str, deps: dict | None = None,
                  lock_packages: dict | None = None,
                  manifest_extra: dict | None = None,
                  scripts: dict | None = None) -> Path:
    project = root / name
    project.mkdir(parents=True, exist_ok=True)
    manifest: dict = {"name": name, "version": "1.0.0"}
    if deps:
        manifest["dependencies"] = deps
    if manifest_extra:
        manifest.update(manifest_extra)
    if scripts:
        manifest["scripts"] = scripts
    (project / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
    packages: dict = {"": {"name": name, "version": "1.0.0"}}
    if deps:
        packages[""].setdefault("dependencies", dict(deps))
    if manifest_extra and "workspaces" in manifest_extra:
        packages[""].setdefault("workspaces", manifest_extra["workspaces"])
    if scripts:
        packages[""].setdefault("scripts", dict(scripts))
    if lock_packages:
        packages.update(lock_packages)
    lock = {"name": name, "version": "1.0.0", "lockfileVersion": 3,
            "requires": True, "packages": packages}
    (project / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    return project


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_receipt(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class NpmProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="dell-npm-probe-test-"))
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def probe(self, project: Path, extra: list[str] | None = None) -> tuple:
        cache = self.root / f"cache-{project.name}"
        receipt = self.root / f"{project.name}.json"
        args = ["--project", str(project), "--cache-dir", str(cache),
                "--scratch-root", str(self.scratch), "--receipt", str(receipt),
                "--npm", NPM]
        proc = run_probe(*(args + (extra or [])))
        return proc, cache, receipt

    def test_empty_project_is_offline_ready(self) -> None:
        project = write_project(self.root, "probe-empty")
        proc, cache, receipt_path = self.probe(project)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr[-2000:])
        receipt = read_receipt(receipt_path)
        self.assertEqual(receipt["schema"], "dell-npm-probe/1")
        self.assertEqual(receipt["verdict"], "ready")
        self.assertFalse(receipt["ignoreScripts"])
        self.assertIn("--ignore-scripts=false", receipt["npmArgv"])
        self.assertNotIn("--ignore-scripts", [a for a in receipt["npmArgv"] if a == "--ignore-scripts"])
        self.assertIn("--offline", receipt["npmArgv"])
        self.assertTrue(receipt["lockSha256"])
        self.assertEqual(receipt["lockSha256"], sha256_of(project / "package-lock.json"))
        self.assertEqual(receipt["manifestSha256"], sha256_of(project / "package.json"))
        self.assertFalse((project / "node_modules").exists(),
                         "the real project must stay untouched")

    def test_missing_tarball_is_cache_miss(self) -> None:
        project = write_project(
            self.root, "probe-miss", deps={"left-pad": "1.3.0"},
            lock_packages={"node_modules/left-pad": {
                "version": "1.3.0",
                "resolved": "https://registry.npmjs.org/left-pad/-/left-pad-1.3.0.tgz",
                "integrity": "sha512-XI5MPzVNApjAyhQzphX8BkmKsKUxD4vm3Jq/Te4cZaB0OUvnha8O6lN4aKTeT/B0Vh1ZP4cArkfW1ryKQ",
            }})
        proc, cache, receipt_path = self.probe(project)
        self.assertEqual(proc.returncode, 1, msg=proc.stdout[-2000:])
        receipt = read_receipt(receipt_path)
        self.assertEqual(receipt["verdict"], "cache-miss")
        self.assertTrue(receipt["missing"], "a miss must name what is missing")
        self.assertTrue(any("left-pad" in item for item in receipt["missing"]))
        self.assertFalse((project / "node_modules").exists())

    def test_ignore_scripts_needs_an_explicit_reason(self) -> None:
        project = write_project(self.root, "probe-scripts")
        cache = self.root / "cache-scripts"
        receipt = self.root / "probe-scripts.json"
        proc = run_probe("--project", str(project), "--cache-dir", str(cache),
                         "--scratch-root", str(self.scratch),
                         "--receipt", str(receipt), "--npm", NPM,
                         "--ignore-scripts")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--ignore-scripts-reason", proc.stderr)
        self.assertFalse(receipt.exists(), "a refused run writes no receipt")
        self.assertFalse((project / "node_modules").exists())

    def test_ignore_scripts_with_reason_is_recorded(self) -> None:
        project = write_project(self.root, "probe-recorded")
        proc, _, receipt_path = self.probe(
            project, ["--ignore-scripts", "--ignore-scripts-reason",
                      "tarball-layout check only; scripts verified separately"])
        self.assertEqual(proc.returncode, 0, msg=proc.stderr[-2000:])
        receipt = read_receipt(receipt_path)
        self.assertEqual(receipt["verdict"], "ready")
        self.assertTrue(receipt["ignoreScripts"])
        self.assertIn("--ignore-scripts", receipt["npmArgv"])
        self.assertTrue(receipt["ignoreScriptsReason"])

    def test_cache_inside_project_is_refused(self) -> None:
        project = write_project(self.root, "probe-contained")
        cache = project / "owned-cache"
        receipt = self.root / "probe-contained.json"
        proc = run_probe("--project", str(project), "--cache-dir", str(cache),
                         "--scratch-root", str(self.scratch),
                         "--receipt", str(receipt), "--npm", NPM)
        self.assertEqual(proc.returncode, 2, msg=proc.stderr[-2000:])
        self.assertIn("outside", proc.stderr)
        self.assertFalse(receipt.exists(), "a refused run writes no receipt")
        self.assertFalse(cache.exists(), "a refused cache is never created")
        self.assertFalse((project / "node_modules").exists())

    def test_cache_inside_scratch_is_refused(self) -> None:
        project = write_project(self.root, "probe-scratch-cache")
        cache = self.scratch / "owned-cache"
        receipt = self.root / "probe-scratch-cache.json"
        proc = run_probe("--project", str(project), "--cache-dir", str(cache),
                         "--scratch-root", str(self.scratch),
                         "--receipt", str(receipt), "--npm", NPM)
        self.assertEqual(proc.returncode, 2, msg=proc.stderr[-2000:])
        self.assertFalse(receipt.exists())
        self.assertFalse(cache.exists())

    def test_inherited_ignore_scripts_env_cannot_silently_disable(self) -> None:
        project = write_project(
            self.root, "probe-env-scripts",
            scripts={"postinstall": "node -e \"process.exit(1)\""})
        cache = self.root / "cache-env-scripts"
        receipt = self.root / "probe-env-scripts.json"
        old = os.environ.get("NPM_CONFIG_IGNORE_SCRIPTS")
        os.environ["NPM_CONFIG_IGNORE_SCRIPTS"] = "true"
        try:
            proc = run_probe("--project", str(project), "--cache-dir", str(cache),
                             "--scratch-root", str(self.scratch),
                             "--receipt", str(receipt), "--npm", NPM)
        finally:
            if old is None:
                os.environ.pop("NPM_CONFIG_IGNORE_SCRIPTS", None)
            else:
                os.environ["NPM_CONFIG_IGNORE_SCRIPTS"] = old
        self.assertEqual(proc.returncode, 1, msg=proc.stdout[-2000:])
        receipt_data = read_receipt(receipt)
        self.assertNotEqual(receipt_data["verdict"], "ready",
                            "a failing lifecycle script must not report ready")
        self.assertFalse(receipt_data["ignoreScripts"])
        self.assertIn("--ignore-scripts=false", receipt_data["npmArgv"])

    def test_failing_lifecycle_is_skipped_only_with_explicit_reason(self) -> None:
        project = write_project(
            self.root, "probe-disabled-scripts",
            scripts={"postinstall": "node -e \"process.exit(1)\""})
        proc, _, receipt_path = self.probe(
            project, ["--ignore-scripts", "--ignore-scripts-reason",
                      "lifecycle verified separately on an enabled run"])
        self.assertEqual(proc.returncode, 0, msg=proc.stderr[-2000:])
        receipt = read_receipt(receipt_path)
        self.assertEqual(receipt["verdict"], "ready")
        self.assertTrue(receipt["ignoreScripts"])

    def test_project_npmrc_cannot_silently_disable_scripts(self) -> None:
        project = write_project(
            self.root, "probe-npmrc",
            scripts={"postinstall": "node -e \"process.exit(1)\""})
        (project / ".npmrc").write_text("ignore-scripts=true\n", encoding="utf-8")
        proc, _, receipt_path = self.probe(project)
        self.assertEqual(proc.returncode, 1, msg=proc.stdout[-2000:])
        receipt = read_receipt(receipt_path)
        self.assertNotEqual(receipt["verdict"], "ready")

    def test_workspace_members_travel_with_the_probe(self) -> None:
        project = write_project(
            self.root, "probe-workspace",
            manifest_extra={"workspaces": ["packages/a"]})
        member = project / "packages" / "a"
        member.mkdir(parents=True, exist_ok=True)
        (member / "package.json").write_text(
            json.dumps({"name": "a", "version": "1.0.0"}), encoding="utf-8")
        (member / "index.js").write_text("module.exports = 1;\n", encoding="utf-8")
        lock = json.loads((project / "package-lock.json").read_text(encoding="utf-8"))
        lock["packages"]["packages/a"] = {"name": "a", "version": "1.0.0"}
        lock["packages"]["node_modules/a"] = {"resolved": "packages/a", "link": True}
        (project / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
        proc, _, receipt_path = self.probe(project)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr[-2000:])
        receipt = read_receipt(receipt_path)
        self.assertEqual(receipt["verdict"], "ready")
        self.assertEqual(receipt["manifestSha256"], sha256_of(project / "package.json"))

    def test_receipt_binds_manifest_and_lock(self) -> None:
        project = write_project(self.root, "probe-bound")
        proc, _, receipt_path = self.probe(project)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr[-2000:])
        before = read_receipt(receipt_path)
        manifest = json.loads((project / "package.json").read_text(encoding="utf-8"))
        manifest["description"] = "touched without touching the lockfile"
        (project / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
        proc2, _, receipt2_path = self.probe(project)
        self.assertEqual(proc2.returncode, 0, msg=proc2.stderr[-2000:])
        after = read_receipt(receipt2_path)
        self.assertEqual(before["lockSha256"], after["lockSha256"])
        self.assertNotEqual(before["manifestSha256"], after["manifestSha256"],
                            "a manifest change must invalidate the old receipt")

    def test_sourcing_limit_is_not_a_cache_miss(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        import dell_npm_probe
        verdict, _ = dell_npm_probe.classify(
            "npm error code ENEEDAUTH\nnpm error need auth for private scope")
        self.assertEqual(verdict, "sourcing-limited")
        verdict, _ = dell_npm_probe.classify(
            "npm error code ENOTCACHED\ncache mode is 'only-if-cached' "
            "but no cached response is available.")
        self.assertEqual(verdict, "cache-miss")


if __name__ == "__main__":
    unittest.main()
