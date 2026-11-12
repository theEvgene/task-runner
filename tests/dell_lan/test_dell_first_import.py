"""First verified import: one sealed job becomes one checked Dell copy.

Every case runs the real request writer, the real signed queue, the real
Lenovo first-export driver, and the real Dell importer against a disposable
fixture. The remote is a local bare repository and is never contacted over
the network.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from first_fixture import FirstWorkspace, bridge_digest, common_digest, dell_digest, git, lenovo_digest, run

FIRST_IMPORT = Path(__file__).resolve().parents[2] / "scripts" / "dell_lan" / "dell_first_import.py"


class FirstImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ws = FirstWorkspace().build()
        cls.job = cls.ws.request_first("00000000000000aa")
        cls.ws.run_job(cls.job)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.ws.cleanup()

    def test_import_binds_source_branch_revision_and_bundle(self):
        proc, data = self.ws.import_to(self.ws.root / "copy-one", self.job)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(data["schema"], "dell-first-import/1")
        self.assertEqual(data["verdict"], "imported")
        self.assertEqual(data["provenance"], "fixture")
        self.assertEqual(data["baseSha"], self.ws.base)
        self.assertEqual(data["refFull"], "refs/heads/main")
        self.assertEqual(data["remoteUrl"], self.ws.remote_url)
        self.assertEqual(data["repo"]["head"], self.ws.base)
        self.assertTrue(data["bundle"]["verified"])
        self.assertTrue(data["manifest"]["match"])
        self.assertTrue(data["manifest"]["returnFileObserved"])
        self.assertEqual(data["driver"]["freshness"], "verified-pinned")
        self.assertEqual(data["dellDriver"]["freshness"], "verified-pinned")
        self.assertEqual(len(data["manifest"]["digest"]), 64)
        self.assertEqual((self.ws.root / "copy-one" / "work.txt").read_text(encoding="utf-8"),
                         "first base\n")

    def test_sealed_result_head_matches_the_imported_copy(self):
        proc, data = self.ws.import_to(self.ws.root / "copy-head", self.job)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        head = git("rev-parse", "--verify", "HEAD", cwd=self.ws.root / "copy-head")
        self.assertEqual(head, data["baseSha"])

    def test_receipt_carries_no_secrets(self):
        _, data = self.ws.import_to(self.ws.root / "copy-clean", self.job)
        text = json.dumps(data)
        self.assertNotIn(self.ws.key_bytes.hex(), text)
        self.assertNotIn("credential", text.lower())
        self.assertNotIn("BEGIN", text)

    def test_one_first_copy_is_never_replaced(self):
        target = self.ws.root / "copy-once"
        proc, data = self.ws.import_to(target, self.job)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc, data = self.ws.import_to(target, self.job)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "repo-exists")
        self.assertEqual(data["verdict"], "mismatch")
        head = git("rev-parse", "--verify", "HEAD", cwd=target)
        self.assertEqual(head, self.ws.base)

    def test_a_pre_existing_directory_is_refused(self):
        target = self.ws.root / "copy-busy"
        target.mkdir()
        (target / "other.txt").write_text("чужое\n", encoding="utf-8")
        proc, data = self.ws.import_to(target, self.job)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "repo-exists")
        self.assertEqual((target / "other.txt").read_text(encoding="utf-8"), "чужое\n")

    def test_unknown_slot_is_reconciled_with_the_same_id(self):
        job_id = self.ws.request_first("00000000000000bb")
        before = sorted((self.ws.queue / "jobs").glob("*.json"))
        proc, data = self.ws.import_to(
            self.ws.root / "copy-late", job_id, "--fetch-timeout", "0.2", "--poll", "0.05")
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(data["verdict"], "unknown")
        self.assertIn("same job id", data["nextAction"])

        self.ws.run_job(job_id)
        after = sorted((self.ws.queue / "jobs").glob("*.json"))
        self.assertEqual([path.name for path in after], [path.name for path in before],
                         "recovery must not submit a second job")

        proc, data = self.ws.import_to(self.ws.root / "copy-late", job_id)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(data["verdict"], "imported")
        self.assertEqual(data["baseSha"], self.ws.base)

    def test_key_inside_the_shared_folder_is_refused(self):
        shared_key = self.ws.shared / "key-inside.bin"
        shared_key.write_bytes(self.ws.key_bytes)
        try:
            proc, data = self.ws.import_to(
                self.ws.root / "copy-key-inside", self.job,
                "--key-file", str(shared_key))
        finally:
            shared_key.unlink(missing_ok=True)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "key-refused")
        self.assertIn("shared", data["detail"])
        self.assertFalse((self.ws.root / "copy-key-inside").exists())

    def test_key_inside_the_anchor_checkout_is_refused(self):
        key = self.ws.anchor / "key.bin"
        key.write_bytes(self.ws.key_bytes)
        try:
            proc, data = self.ws.import_to(
                self.ws.root / "copy-key-anchor", self.job, "--key-file", str(key))
        finally:
            key.unlink(missing_ok=True)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "key-refused")
        self.assertIn("checkout", data["detail"])
        self.assertFalse((self.ws.root / "copy-key-anchor").exists())

    def test_usage_guard_writes_no_receipt(self):
        receipt = self.ws.root / "usage-first.json"
        proc = run([sys.executable, str(FIRST_IMPORT), "--queue", str(self.ws.queue),
                    "--key-file", str(self.ws.key), "--job-id", "not-a-job",
                    "--shared", str(self.ws.shared), "--repo", str(self.ws.anchor),
                    "--repo-out", str(self.ws.root / "copy-usage"),
                    "--remote-url", self.ws.remote_url,
                    "--expected-lenovo-driver-sha256", lenovo_digest(),
                    "--expected-dell-driver-sha256", dell_digest(),
                    "--receipt", str(receipt)])
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(receipt.exists())

    def test_a_relative_anchor_root_is_refused_at_usage(self):
        receipt = self.ws.root / "relative-anchor.json"
        proc = run([sys.executable, str(FIRST_IMPORT), "--queue", str(self.ws.queue),
                    "--key-file", str(self.ws.key), "--job-id", self.job,
                    "--shared", str(self.ws.shared), "--repo", "dell-anchor",
                    "--repo-out", str(self.ws.root / "copy-relative"),
                    "--remote-url", self.ws.remote_url,
                    "--expected-lenovo-driver-sha256", lenovo_digest(),
                    "--expected-dell-driver-sha256", dell_digest(),
                    "--expected-lenovo-common-sha256", common_digest(),
                    "--expected-dell-common-sha256", common_digest(),
                    "--expected-bridge-sha256", bridge_digest(),
                    "--receipt", str(receipt)])
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("absolute", proc.stderr)
        self.assertFalse(receipt.exists())


if __name__ == "__main__":
    unittest.main()
