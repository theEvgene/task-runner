"""First verified import refusals: nothing unverified becomes a copy.

Every case runs the real signed queue and the real Dell importer against a
disposable fixture. The remote is a local bare repository and is never
contacted over the network.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

from first_fixture import FIRST_IMPORT, FirstWorkspace, git, run

FIRST_EXPORT_SCHEMA = "dell-lan-first-export/1"


class FirstImportRefusalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ws = FirstWorkspace().build()
        cls.job = cls.ws.request_first("00000000000000cc")
        cls.ws.run_job(cls.job)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.ws.cleanup()

    def import_branch(self, repo_out: Path, job_id: str, branch: str):
        receipt = self.ws.root / f"first-branch-{repo_out.name}.json"
        proc = run([
            sys.executable, str(FIRST_IMPORT), "--queue", str(self.ws.queue),
            "--key-file", str(self.ws.key), "--job-id", job_id,
            "--shared", str(self.ws.shared), "--repo", str(self.ws.anchor),
            "--repo-out", str(repo_out), "--remote-url", self.ws.remote_url,
            "--branch", branch, "--receipt", str(receipt),
        ])
        data = json.loads(receipt.read_text(encoding="utf-8")) if receipt.is_file() else None
        return proc, data

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

        name = "dell-first-00000000000000cc.bundle"
        target = self.ws.shared / name
        original = target.read_bytes()
        try:
            target.write_bytes(other_bundle.read_bytes())
            proc, data = self.ws.import_to(self.ws.root / "copy-swapped", self.job)
        finally:
            target.write_bytes(original)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "artifact-mismatch")
        self.assertFalse(data["bundle"]["verified"])
        self.assertFalse((self.ws.root / "copy-swapped").exists())

    def test_a_manifest_swapped_with_the_bundle_is_still_refused(self):
        name = "dell-first-00000000000000cc.bundle"
        manifest_name = "dell-first-00000000000000cc.manifest.json"
        bundle_path = self.ws.shared / name
        manifest_path = self.ws.shared / manifest_name
        original_bundle = bundle_path.read_bytes()
        original_manifest = manifest_path.read_bytes()
        try:
            manifest = json.loads(original_manifest.decode("utf-8"))
            manifest["files"][0]["bytes"] += 1
            manifest_path.write_bytes(json.dumps(manifest).encode("utf-8"))
            proc, data = self.ws.import_to(self.ws.root / "copy-manifest", self.job)
        finally:
            bundle_path.write_bytes(original_bundle)
            manifest_path.write_bytes(original_manifest)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "manifest-mismatch")
        self.assertFalse(data["manifest"]["match"])
        self.assertFalse((self.ws.root / "copy-manifest").exists())

    def test_a_wrong_key_cannot_open_the_sealed_result(self):
        wrong = self.ws.root / "wrong.bin"
        wrong.write_bytes(b"w" * 32)
        proc, data = self.ws.import_to(
            self.ws.root / "copy-wrong-key", self.job, "--key-file", str(wrong))
        self.assertEqual(proc.returncode, 1)
        # A wrong key fails before any trust decision: the sealed job envelope
        # itself cannot be opened, so no result is ever read with it.
        self.assertIn(data["reason"], ("queue-job-unverifiable", "queue-result-unverifiable", "key-refused"))
        self.assertFalse((self.ws.root / "copy-wrong-key").exists())

    def test_an_unknown_job_id_proves_nothing(self):
        proc, data = self.ws.import_to(self.ws.root / "copy-unknown", "f" * 32)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "queue-job-missing")
        self.assertFalse((self.ws.root / "copy-unknown").exists())

    def test_a_job_with_another_command_is_not_this_import(self):
        command_file = self.ws.root / "foreign-command.txt"
        command_file.write_text("echo hello\n", encoding="utf-8")
        job_id = self.ws.submit(command_file)
        self.ws.run_job(job_id)
        proc, data = self.ws.import_to(self.ws.root / "copy-foreign", job_id)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "queue-job-mismatch")
        self.assertFalse((self.ws.root / "copy-foreign").exists())

    def test_a_truncated_result_is_a_rejected_failure(self):
        token = "00000000000000dd"
        request = self.ws.first_request(token)
        job_id = self.ws.submit(request["commandFile"], return_file=Path(request["returnFile"]))
        self.ws.seal_result(job_id, stdout="{}", truncated=True)
        proc, data = self.ws.import_to(self.ws.root / "copy-truncated", job_id)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["verdict"], "rejected")
        self.assertEqual(data["reason"], "base-job-failed")
        self.assertFalse((self.ws.root / "copy-truncated").exists())

    def test_a_failed_worker_exit_is_a_rejected_failure(self):
        token = "00000000000000ee"
        request = self.ws.first_request(token)
        job_id = self.ws.submit(request["commandFile"], return_file=Path(request["returnFile"]))
        self.ws.seal_result(job_id, stdout="{}", status="error", exit_code=1,
                            stderr="lenovo blew up")
        proc, data = self.ws.import_to(self.ws.root / "copy-failed", job_id)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["verdict"], "rejected")
        self.assertEqual(data["reason"], "base-job-failed")
        self.assertFalse((self.ws.root / "copy-failed").exists())

    def test_sealed_artifacts_that_ignore_the_token_are_malformed(self):
        token = "00000000000000ef"
        request = self.ws.first_request(token)
        job_id = self.ws.submit(request["commandFile"], return_file=Path(request["returnFile"]))
        forged = {
            "schema": FIRST_EXPORT_SCHEMA, "token": token, "remoteUrl": self.ws.remote_url,
            "refFull": "refs/heads/main", "tipSha": "a" * 40, "sha256": "b" * 64,
            "bytes": 10, "bundleName": f"dell-base-{token}.bundle",
            "manifestName": f"dell-first-{token}.manifest.json",
            "manifestDigest": "c" * 64,
        }
        self.ws.seal_result(job_id, stdout=json.dumps(forged))
        proc, data = self.ws.import_to(self.ws.root / "copy-forged", job_id)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "request-malformed")
        self.assertFalse((self.ws.root / "copy-forged").exists())

    def test_an_unreachable_remote_is_a_rejected_failure(self):
        token = "00000000000000f0"
        missing = str(self.ws.root / "no-such-remote.git")
        request = self.ws.first_request(token, remote_url=missing)
        job_id = self.ws.submit(request["commandFile"], return_file=Path(request["returnFile"]))
        _, worker = self.ws.run_job(job_id)
        self.assertNotEqual(worker.returncode, 0, worker.stderr)
        proc, data = self.ws.import_to(self.ws.root / "copy-unreachable", job_id)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["verdict"], "rejected")
        self.assertEqual(data["reason"], "base-job-failed")
        self.assertIn("remote-unavailable", data["detail"])
        self.assertFalse((self.ws.root / "copy-unreachable").exists())

    def test_another_branch_is_never_imported_as_this_one(self):
        proc, data = self.import_branch(self.ws.root / "copy-branch", self.job, "feature")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "source-mismatch")
        self.assertFalse((self.ws.root / "copy-branch").exists())

    def test_another_project_is_never_imported_as_this_one(self):
        proc, data = self.ws.import_to(
            self.ws.root / "copy-pin", self.job, "--expected-project", "other/project")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "artifact-mismatch")
        self.assertFalse(data["origin"]["projectMatches"])
        self.assertFalse((self.ws.root / "copy-pin").exists())

    def test_a_request_digest_the_job_did_not_name_is_refused(self):
        token = "00000000000000f1"
        request = self.ws.first_request(token)
        command_file = Path(request["commandFile"])
        command_file.write_text(
            command_file.read_text(encoding="utf-8").replace(
                request["requestSha256"], "0" * 64), encoding="utf-8")
        job_id = self.ws.submit(command_file, return_file=Path(request["returnFile"]))
        _, worker = self.ws.run_job(job_id)
        self.assertNotEqual(worker.returncode, 0)
        proc, data = self.ws.import_to(self.ws.root / "copy-digest", job_id)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["verdict"], "rejected")
        self.assertEqual(data["reason"], "base-job-failed")
        self.assertIn("request-digest-mismatch", data["detail"])
        self.assertFalse((self.ws.root / "copy-digest").exists())


if __name__ == "__main__":
    unittest.main()
