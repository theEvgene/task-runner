"""Base direction: request from Lenovo, receive, verify, import exactly once.

Every case runs the real request writer, the real signed queue, the real
Lenovo export driver, and the real Dell importer against a disposable fixture.
The remote is a local bare repository and is never contacted.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from dell_fixture import Workspace, git, run, sha256_bytes

import sys

BASE_IMPORT = Path(__file__).resolve().parents[2] / "scripts" / "dell_lan" / "dell_base_import.py"


class BaseImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ws = Workspace().build()
        cls.job = cls.ws.request_base("00000000000000aa")
        cls.ws.run_job(cls.job)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.ws.cleanup()

    def test_import_binds_the_sealed_base_and_manifest(self):
        proc, data = self.ws.import_base_to(self.ws.root / "copy-one", self.job)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(data["schema"], "dell-base-import/1")
        self.assertEqual(data["verdict"], "imported")
        self.assertEqual(data["baseSha"], self.ws.base)
        self.assertEqual(data["repo"]["head"], self.ws.base)
        self.assertTrue(data["bundle"]["verified"])
        self.assertTrue(data["manifest"]["match"])
        self.assertTrue(data["manifest"]["returnFileObserved"])
        self.assertEqual(data["origin"]["projects"], ["group/project"])
        self.assertTrue(data["origin"]["projectMatches"])
        self.assertEqual(len(data["manifest"]["digest"]), 64)
        self.assertEqual((self.ws.root / "copy-one" / "work.txt").read_text(encoding="utf-8"),
                         "published base\n")

    def test_one_source_copy_is_never_replaced(self):
        target = self.ws.root / "copy-once"
        proc, data = self.ws.import_base_to(target, self.job)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc, data = self.ws.import_base_to(target, self.job)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "repo-exists")
        self.assertIn("one source copy", data["nextAction"])

    def test_a_swapped_bundle_cannot_borrow_the_sealed_digest(self):
        swapped = self.ws.root / "swapped"
        swapped.mkdir()
        git("init", "-q", "-b", "main", cwd=swapped)
        git("config", "user.email", "x@example.invalid", cwd=swapped)
        git("config", "user.name", "x", cwd=swapped)
        (swapped / "other.txt").write_text("other\n", encoding="utf-8")
        git("add", "other.txt", cwd=swapped)
        git("commit", "-qm", "other", cwd=swapped)
        other_bundle = self.ws.root / "other.bundle"
        git("bundle", "create", str(other_bundle), "refs/heads/main", cwd=swapped)

        name = "dell-base-00000000000000aa.bundle"
        manifest_path = self.ws.shared / "dell-base-00000000000000aa.manifest.json"
        target = self.ws.shared / name
        original = target.read_bytes()
        original_manifest = manifest_path.read_bytes()
        try:
            target.write_bytes(other_bundle.read_bytes())
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["files"][0]["sha256"] = sha256_bytes(target.read_bytes())
            manifest["files"][0]["bytes"] = target.stat().st_size
            manifest["manifestDigest"] = sha256_bytes(json.dumps(
                manifest["files"], separators=(",", ":")).encode("utf-8"))
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            proc, data = self.ws.import_base_to(self.ws.root / "copy-swapped", self.job)
        finally:
            target.write_bytes(original)
            manifest_path.write_bytes(original_manifest)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "artifact-mismatch")
        self.assertFalse(data["bundle"]["verified"])

    def test_key_inside_the_shared_folder_is_refused(self):
        shared_key = self.ws.shared / "key-inside.bin"
        shared_key.write_bytes(self.ws.key_bytes)
        try:
            proc, data = self.ws.import_base_to(
                self.ws.root / "copy-key-inside", self.job,
                "--key-file", str(shared_key))
        finally:
            shared_key.unlink(missing_ok=True)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "key-refused")
        self.assertIn("shared", data["detail"])
        self.assertFalse((self.ws.root / "copy-key-inside").exists())

    def test_key_inside_the_named_checkout_is_refused(self):
        checkout = self.ws.root / "guarded-checkout"
        checkout.mkdir(exist_ok=True)
        key = checkout / "key.bin"
        key.write_bytes(self.ws.key_bytes)
        try:
            proc, data = self.ws.import_base_to(
                self.ws.root / "copy-key-repo", self.job,
                "--repo", str(checkout), "--key-file", str(key))
        finally:
            key.unlink(missing_ok=True)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "key-refused")
        self.assertIn("checkout", data["detail"])

    def test_key_inside_the_importing_checkout_is_refused(self):
        """The source copy named by --repo is a protected root, not an optional one."""

        self.ws.dell.mkdir(parents=True, exist_ok=True)
        key = self.ws.dell / "key.bin"
        key.write_bytes(self.ws.key_bytes)
        try:
            proc, data = self.ws.import_base_to(
                self.ws.root / "copy-key-source", self.job, "--key-file", str(key))
        finally:
            key.unlink(missing_ok=True)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "key-refused")
        self.assertIn("checkout", data["detail"])
        self.assertFalse((self.ws.root / "copy-key-source").exists())

    def test_a_key_read_without_the_repo_root_is_refused_at_usage(self):
        """Omitting --repo cannot silently narrow the custody check."""

        receipt = self.ws.root / "no-repo.json"
        proc = run([
            sys.executable, str(BASE_IMPORT), "--queue", str(self.ws.queue),
            "--key-file", str(self.ws.key), "--job-id", self.job,
            "--shared", str(self.ws.shared),
            "--repo-out", str(self.ws.root / "copy-no-repo"),
            "--receipt", str(receipt),
        ])
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("--repo", proc.stderr)
        self.assertFalse(receipt.exists())
        self.assertFalse((self.ws.root / "copy-no-repo").exists())

    def test_a_relative_repo_root_is_refused_at_usage(self):
        receipt = self.ws.root / "relative-repo.json"
        proc, _ = self.ws.import_base_to(
            self.ws.root / "copy-relative-repo", self.job,
            "--repo", "dell-copy", "--receipt", str(receipt))
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("absolute", proc.stderr)
        self.assertFalse(receipt.exists())

    def test_a_key_outside_every_protected_root_imports(self):
        proc, data = self.ws.import_base_to(self.ws.root / "copy-key-clean", self.job)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(data["verdict"], "imported")
        self.assertEqual(data["repo"]["head"], self.ws.base)

    def test_a_request_the_job_did_not_name_is_refused(self):
        request = self.ws.base_request("00000000000000bb")
        command_file = Path(request["commandFile"])
        command_file.write_text(
            command_file.read_text(encoding="utf-8").replace(
                request["requestSha256"], "0" * 64), encoding="utf-8")
        job_id = self.ws.submit(command_file, return_file=Path(request["returnFile"]))
        _, proc = self.ws.run_job(job_id)
        self.assertNotEqual(proc.returncode, 0)
        result, data = self.ws.import_base_to(self.ws.root / "copy-bad", job_id)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(data["verdict"], "rejected")
        self.assertEqual(data["reason"], "base-job-failed")
        self.assertIn("request-digest-mismatch", data["detail"])

    def test_unknown_slot_is_reconciled_not_replaced(self):
        request = self.ws.base_request("00000000000000cc")
        job_id = self.ws.submit(request["commandFile"], return_file=Path(request["returnFile"]))
        proc, data = self.ws.import_base_to(
            self.ws.root / "copy-late", job_id, "--fetch-timeout", "0.2", "--poll", "0.05")
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(data["verdict"], "unknown")
        self.assertIn("do not start a second import", data["nextAction"])

        self.ws.run_job(job_id)
        proc, data = self.ws.import_base_to(self.ws.root / "copy-late", job_id)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(data["verdict"], "imported")
        self.assertEqual(data["baseSha"], self.ws.base)

    def test_project_pin_mismatch_is_refused(self):
        result, data = self.ws.import_base_to(
            self.ws.root / "copy-pin", self.job, "--expected-project", "other/project")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(data["reason"], "artifact-mismatch")
        self.assertFalse(data["origin"]["projectMatches"])

    def test_usage_guard_writes_no_receipt(self):
        receipt = self.ws.root / "usage-base.json"
        proc = run([            sys.executable, str(BASE_IMPORT), "--queue", str(self.ws.queue),
            "--key-file", str(self.ws.key), "--job-id", "not-a-job",
            "--shared", str(self.ws.shared), "--repo", str(self.ws.dell),
            "--repo-out", str(self.ws.root / "copy-usage"),
            "--receipt", str(receipt),
        ])
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(receipt.exists())


if __name__ == "__main__":
    unittest.main()
