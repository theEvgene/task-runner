"""First-import hardening: exact command binding, driver freshness, exclusive
artifacts, malicious remote handling, and file-destination distinction.

Every case runs the real signed queue and the real drivers against a
disposable fixture. The remote is a local bare repository and is never
contacted over the network.
"""

from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path

from first_fixture import FIRST_IMPORT, REQUEST_CLI, FirstWorkspace, dell_digest, git, run

FIRST_EXPORT_SCHEMA = "dell-lan-first-export/1"


def driver_digest() -> str:
    return hashlib.sha256(
        (Path(__file__).resolve().parents[2]
         / "scripts" / "dell_lan" / "lenovo_first_export.py").read_bytes()
    ).hexdigest()


def dell_driver_digest() -> str:
    return dell_digest()


class FirstImportHardeningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ws = FirstWorkspace().build()
        cls.job = cls.ws.request_first("0000000000000aa1")
        cls.ws.run_job(cls.job)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.ws.cleanup()

    def test_a_command_that_only_mentions_the_driver_proves_nothing(self):
        command_file = self.ws.root / "echo-marker.txt"
        command_file.write_text("echo lenovo_first_export.py\n", encoding="utf-8")
        job_id = self.ws.submit(command_file)
        self.ws.run_job(job_id)
        proc, data = self.ws.import_to(self.ws.root / "copy-echo-marker", job_id)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "queue-job-mismatch")
        self.assertFalse((self.ws.root / "copy-echo-marker").exists())

    def test_a_valid_command_with_an_extra_argument_is_refused(self):
        request = self.ws.first_request("0000000000000aa2")
        command_file = Path(request["commandFile"])
        text = command_file.read_text(encoding="utf-8").rstrip("\n")
        command_file.write_text(text + ' "--evil" "1"\n', encoding="utf-8")
        job_id = self.ws.submit(command_file, return_file=Path(request["returnFile"]))
        proc, data = self.ws.import_to(self.ws.root / "copy-extra-arg", job_id)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "queue-job-mismatch")
        self.assertFalse((self.ws.root / "copy-extra-arg").exists())

    def test_a_stale_driver_version_is_refused(self):
        token = "0000000000000aa3"
        request = self.ws.first_request(token)
        job_id = self.ws.submit(request["commandFile"], return_file=Path(request["returnFile"]))
        forged = {
            "schema": FIRST_EXPORT_SCHEMA, "driverVersion": "0",
            "driverSha256": "c" * 64, "token": token,
            "remoteUrl": self.ws.remote_url, "refFull": "refs/heads/main",
            "tipSha": "a" * 40, "sha256": "b" * 64, "bytes": 10,
            "bundleName": f"dell-first-{token}.bundle",
            "manifestName": f"dell-first-{token}.manifest.json",
            "manifestDigest": "c" * 64,
        }
        self.ws.seal_result(job_id, stdout=json.dumps(forged))
        proc, data = self.ws.import_to(self.ws.root / "copy-stale", job_id)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "driver-stale")
        self.assertFalse((self.ws.root / "copy-stale").exists())

    def test_a_missing_driver_version_is_refused(self):
        token = "0000000000000aa4"
        request = self.ws.first_request(token)
        job_id = self.ws.submit(request["commandFile"], return_file=Path(request["returnFile"]))
        forged = {
            "schema": FIRST_EXPORT_SCHEMA, "token": token,
            "remoteUrl": self.ws.remote_url, "refFull": "refs/heads/main",
            "tipSha": "a" * 40, "sha256": "b" * 64, "bytes": 10,
            "bundleName": f"dell-first-{token}.bundle",
            "manifestName": f"dell-first-{token}.manifest.json",
            "manifestDigest": "c" * 64,
        }
        self.ws.seal_result(job_id, stdout=json.dumps(forged))
        proc, data = self.ws.import_to(self.ws.root / "copy-no-version", job_id)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "driver-stale")
        self.assertFalse((self.ws.root / "copy-no-version").exists())

    def test_pinned_digest_mismatch_refuses_and_match_imports(self):
        proc, data = self.ws.import_to(self.ws.root / "copy-pin-read", self.job)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        sealed_digest = (data.get("driver") or {}).get("sha256")
        self.assertTrue(sealed_digest, data)
        self.assertEqual(data["driver"]["freshness"], "verified-pinned")
        self.assertEqual(sealed_digest, driver_digest())
        self.assertEqual(data["dellDriver"]["freshness"], "verified-pinned")
        self.assertEqual(data["dellDriver"]["sha256"], dell_driver_digest())

        wrong = "0" * 63 + ("1" if sealed_digest != "0" * 63 + "1" else "2")
        proc, data = self.ws.import_to(
            self.ws.root / "copy-pin-wrong", self.job,
            "--expected-lenovo-driver-sha256", wrong)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "driver-stale")
        self.assertFalse((self.ws.root / "copy-pin-wrong").exists())

        proc, data = self.ws.import_to(
            self.ws.root / "copy-pin-ok", self.job,
            "--expected-lenovo-driver-sha256", sealed_digest)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(data["verdict"], "imported")
        self.assertEqual(data["driver"]["freshness"], "verified-pinned")

    def test_missing_lenovo_pin_is_refused_without_copy(self):
        receipt = self.ws.root / "missing-lenovo-pin.json"
        proc = run([
            sys.executable, str(FIRST_IMPORT), "--queue", str(self.ws.queue),
            "--key-file", str(self.ws.key), "--job-id", self.job,
            "--shared", str(self.ws.shared), "--repo", str(self.ws.anchor),
            "--repo-out", str(self.ws.root / "copy-missing-lenovo"),
            "--remote-url", self.ws.remote_url, "--branch", "main",
            "--expected-dell-driver-sha256", dell_driver_digest(),
            "--receipt", str(receipt),
        ])
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertFalse((self.ws.root / "copy-missing-lenovo").exists())

    def test_missing_dell_pin_is_refused_without_copy(self):
        receipt = self.ws.root / "missing-dell-pin.json"
        proc = run([
            sys.executable, str(FIRST_IMPORT), "--queue", str(self.ws.queue),
            "--key-file", str(self.ws.key), "--job-id", self.job,
            "--shared", str(self.ws.shared), "--repo", str(self.ws.anchor),
            "--repo-out", str(self.ws.root / "copy-missing-dell"),
            "--remote-url", self.ws.remote_url, "--branch", "main",
            "--expected-lenovo-driver-sha256", driver_digest(),
            "--receipt", str(receipt),
        ])
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertFalse((self.ws.root / "copy-missing-dell").exists())

    def test_wrong_dell_pin_is_driver_stale_without_copy(self):
        sealed = driver_digest()
        wrong_dell = "f" * 63 + ("0" if dell_driver_digest() != "f" * 63 + "0" else "1")
        proc, data = self.ws.import_to(
            self.ws.root / "copy-wrong-dell", self.job,
            "--expected-lenovo-driver-sha256", sealed,
            "--expected-dell-driver-sha256", wrong_dell)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "driver-stale")
        self.assertFalse((self.ws.root / "copy-wrong-dell").exists())

    def test_a_foreign_manifest_is_never_overwritten(self):
        token = "0000000000000aa5"
        request = self.ws.first_request(token)
        manifest_path = self.ws.shared / f"dell-first-{token}.manifest.json"
        foreign = b'{"foreign": true}\n'
        manifest_path.write_bytes(foreign)
        job_id = self.ws.submit(request["commandFile"], return_file=Path(request["returnFile"]))
        _, worker = self.ws.run_job(job_id)
        self.assertNotEqual(worker.returncode, 0, worker.stderr)
        self.assertEqual(manifest_path.read_bytes(), foreign)
        self.assertFalse((self.ws.shared / f"dell-first-{token}.bundle").exists())
        proc, data = self.ws.import_to(self.ws.root / "copy-foreign-manifest", job_id)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["verdict"], "rejected")
        self.assertIn("artifact-exists", data["detail"])
        self.assertFalse((self.ws.root / "copy-foreign-manifest").exists())

    def test_a_leading_dash_remote_is_refused_before_submit(self):
        command_file = self.ws.root / "dash-command.txt"
        proc = run([
            sys.executable, str(REQUEST_CLI), "first",
            "--shared", str(self.ws.shared), "--token", "0000000000000aa6",
            "--lenovo-skill-dir", str(self.ws.root),
            "--lenovo-driver-dir", str(REQUEST_CLI.parent),
            "--python", sys.executable, "--remote-url", "-evil-remote",
            "--ref-full", "refs/heads/main",
            "--request-name", "first-request-0000000000000aa6.json",
            "--command-file", str(command_file), "--repo", str(self.ws.anchor),
        ])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("remote-url", proc.stderr)

    def test_a_file_at_the_destination_is_distinguished(self):
        target = self.ws.root / "copy-is-file"
        target.write_text("чужое\n", encoding="utf-8")
        proc, data = self.ws.import_to(target, self.job)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "destination-is-file")
        self.assertEqual(target.read_text(encoding="utf-8"), "чужое\n")


if __name__ == "__main__":
    unittest.main()
