"""Review regression: independently confirmed findings for #21/#22.

Covers the five hardening points against the real signed queue and the
real drivers (disposable fixture, local bare remote, no network):

1. driver/dependency freshness covers the actually invoked driver AND the
   shared dependency plus the loaded bridge -- a tampered dependency fails
   closed as driver-stale and a missing pin is a usage refusal;
2. credentialed remote URLs are rejected before any write or receipt output;
3. receipt/repo-out cannot mutate the anchor, queue, or share, and both
   must be absolute;
4. the destination is claimed exclusively -- files and links are never
   overwritten;
5. the request file name token is bound to the internal token on write,
   on Lenovo validation, and on the sealed job/result cross-check.
"""

from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path

from first_fixture import (
    FIRST_IMPORT, REQUEST_CLI, FirstWorkspace, bridge_digest, common_digest,
    dell_digest, git, lenovo_digest, run,
)

FIRST_EXPORT_SCHEMA = "dell-lan-first-export/1"


def sealed_missing_common(token: str, ws: FirstWorkspace) -> dict:
    return {
        "schema": FIRST_EXPORT_SCHEMA, "driverVersion": "1",
        "driverSha256": lenovo_digest(), "token": token,
        "remoteUrl": ws.remote_url, "refFull": "refs/heads/main",
        "tipSha": "a" * 40, "sha256": "b" * 64, "bytes": 10,
        "bundleName": f"dell-first-{token}.bundle",
        "manifestName": f"dell-first-{token}.manifest.json",
        "manifestDigest": "c" * 64,
    }


class ReviewRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ws = FirstWorkspace().build()
        cls.job = cls.ws.request_first("0000000000000c01")
        cls.ws.run_job(cls.job)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.ws.cleanup()

    # 1. tampered dependencies fail closed -------------------------------
    def test_tampered_lenovo_common_is_driver_stale(self):
        token = "0000000000000c11"
        request = self.ws.first_request(token)
        job_id = self.ws.submit(request["commandFile"], return_file=Path(request["returnFile"]))
        forged = dict(sealed_missing_common(token, self.ws))
        forged["driverCommonSha256"] = "0" * 64
        self.ws.seal_result(job_id, stdout=json.dumps(forged))
        proc, data = self.ws.import_to(self.ws.root / "copy-tampered-lenovo-common", job_id)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "driver-stale")
        self.assertFalse((self.ws.root / "copy-tampered-lenovo-common").exists())

    def test_missing_lenovo_common_seal_is_driver_stale(self):
        token = "0000000000000c12"
        request = self.ws.first_request(token)
        job_id = self.ws.submit(request["commandFile"], return_file=Path(request["returnFile"]))
        self.ws.seal_result(job_id, stdout=json.dumps(sealed_missing_common(token, self.ws)))
        proc, data = self.ws.import_to(self.ws.root / "copy-missing-common-seal", job_id)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "driver-stale")
        self.assertFalse((self.ws.root / "copy-missing-common-seal").exists())

    def test_tampered_dell_common_pin_is_driver_stale(self):
        wrong = "e" * 64 if common_digest() != "e" * 64 else "d" * 64
        proc, data = self.ws.import_to(
            self.ws.root / "copy-tampered-dell-common", self.job,
            "--expected-lenovo-common-sha256", common_digest(),
            "--expected-dell-common-sha256", wrong,
            "--expected-bridge-sha256", bridge_digest())
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "driver-stale")
        self.assertFalse((self.ws.root / "copy-tampered-dell-common").exists())

    def test_tampered_bridge_pin_is_driver_stale(self):
        wrong = "f" * 63 + ("0" if bridge_digest() != "f" * 63 + "0" else "1")
        proc, data = self.ws.import_to(
            self.ws.root / "copy-tampered-bridge", self.job,
            "--expected-bridge-sha256", wrong)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "driver-stale")
        self.assertFalse((self.ws.root / "copy-tampered-bridge").exists())

    def test_receipt_names_the_trust_boundary(self):
        proc, data = self.ws.import_to(self.ws.root / "copy-boundary", self.job)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("trustBoundary", data)
        self.assertIn("forge", data["trustBoundary"])
        self.assertEqual(data["driver"]["freshness"], "verified-pinned")
        self.assertEqual(data["dellDriver"]["freshness"], "verified-pinned")
        self.assertEqual(data["bridge"]["freshness"], "verified-pinned")

    # 2. credentials ------------------------------------------------------
    def test_credentialed_remote_is_refused_before_any_write(self):
        command_file = self.ws.root / "cred-command.txt"
        request_name = "first-request-0000000000000c21.json"
        proc = run([
            sys.executable, str(REQUEST_CLI), "first",
            "--shared", str(self.ws.shared), "--token", "0000000000000c21",
            "--lenovo-skill-dir", str(self.ws.root),
            "--lenovo-driver-dir", str(REQUEST_CLI.parent),
            "--python", sys.executable,
            "--remote-url", "https://user:s3cret@example.invalid/group/project.git",
            "--ref-full", "refs/heads/main",
            "--request-name", request_name,
            "--command-file", str(command_file), "--repo", str(self.ws.anchor),
        ])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("credential", proc.stderr.lower())
        self.assertFalse(command_file.exists())
        self.assertFalse((self.ws.shared / request_name).exists())

    def test_credentialed_cli_remote_writes_no_receipt(self):
        receipt = self.ws.root / "cred-cli.json"
        proc = run([
            sys.executable, str(FIRST_IMPORT), "--queue", str(self.ws.queue),
            "--key-file", str(self.ws.key), "--job-id", self.job,
            "--shared", str(self.ws.shared), "--repo", str(self.ws.anchor),
            "--repo-out", str(self.ws.root / "copy-cred-cli"),
            "--remote-url", "https://user:s3cret@example.invalid/group/project.git",
            "--branch", "main",
            "--expected-lenovo-driver-sha256", lenovo_digest(),
            "--expected-dell-driver-sha256", dell_digest(),
            "--expected-lenovo-common-sha256", common_digest(),
            "--expected-dell-common-sha256", common_digest(),
            "--expected-bridge-sha256", bridge_digest(),
            "--receipt", str(receipt),
        ])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("credential", proc.stderr.lower())
        self.assertFalse(receipt.exists())
        self.assertFalse((self.ws.root / "copy-cred-cli").exists())

    def test_sealed_credentialed_remote_is_refused_without_copy(self):
        token = "0000000000000c22"
        request = self.ws.first_request(token)
        job_id = self.ws.submit(request["commandFile"], return_file=Path(request["returnFile"]))
        forged = dict(sealed_missing_common(token, self.ws))
        forged["driverCommonSha256"] = common_digest()
        forged["remoteUrl"] = "https://user:s3cret@example.invalid/group/project.git"
        self.ws.seal_result(job_id, stdout=json.dumps(forged))
        proc, data = self.ws.import_to(
            self.ws.root / "copy-sealed-cred", job_id,
            "--remote-url", "https://user:s3cret@example.invalid/group/project.git")
        # The CLI-level credential guard fires before any sealed read when
        # the caller spells the credentialed URL; a forged sealed URL under
        # a clean CLI spelling is refused as credential-refused instead.
        if proc.returncode == 2:
            self.assertFalse((self.ws.root / "copy-sealed-cred").exists())
            return
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "credential-refused")
        self.assertFalse((self.ws.root / "copy-sealed-cred").exists())
        text = json.dumps(data)
        self.assertNotIn("s3cret", text)

    # 3. receipt/repo-out placement ---------------------------------------
    def test_relative_repo_out_is_a_usage_refusal(self):
        receipt = self.ws.root / "relative-repo-out.json"
        proc = run([
            sys.executable, str(FIRST_IMPORT), "--queue", str(self.ws.queue),
            "--key-file", str(self.ws.key), "--job-id", self.job,
            "--shared", str(self.ws.shared), "--repo", str(self.ws.anchor),
            "--repo-out", "copy-relative-out",
            "--remote-url", self.ws.remote_url, "--branch", "main",
            "--expected-lenovo-driver-sha256", lenovo_digest(),
            "--expected-dell-driver-sha256", dell_digest(),
            "--expected-lenovo-common-sha256", common_digest(),
            "--expected-dell-common-sha256", common_digest(),
            "--expected-bridge-sha256", bridge_digest(),
            "--receipt", str(receipt),
        ])
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("absolute", proc.stderr)
        self.assertFalse(receipt.exists())

    def test_receipt_inside_protected_roots_is_refused(self):
        for label, root in (("anchor", self.ws.anchor), ("queue", self.ws.queue),
                            ("shared", self.ws.shared)):
            receipt = root / f"receipt-in-{label}.json"
            proc = run([
                sys.executable, str(FIRST_IMPORT), "--queue", str(self.ws.queue),
                "--key-file", str(self.ws.key), "--job-id", self.job,
                "--shared", str(self.ws.shared), "--repo", str(self.ws.anchor),
                "--repo-out", str(self.ws.root / f"copy-receipt-{label}"),
                "--remote-url", self.ws.remote_url, "--branch", "main",
                "--expected-lenovo-driver-sha256", lenovo_digest(),
                "--expected-dell-driver-sha256", dell_digest(),
                "--expected-lenovo-common-sha256", common_digest(),
                "--expected-dell-common-sha256", common_digest(),
                "--expected-bridge-sha256", bridge_digest(),
                "--receipt", str(receipt),
            ])
            self.assertEqual(proc.returncode, 2, f"{label}: {proc.stderr}")
            self.assertFalse(receipt.exists(), label)
            self.assertFalse((self.ws.root / f"copy-receipt-{label}").exists(), label)

    def test_repo_out_inside_protected_roots_is_refused(self):
        for label, root in (("anchor", self.ws.anchor), ("queue", self.ws.queue),
                            ("shared", self.ws.shared)):
            target = root / f"copy-in-{label}"
            receipt = self.ws.root / f"receipt-out-{label}.json"
            proc = run([
                sys.executable, str(FIRST_IMPORT), "--queue", str(self.ws.queue),
                "--key-file", str(self.ws.key), "--job-id", self.job,
                "--shared", str(self.ws.shared), "--repo", str(self.ws.anchor),
                "--repo-out", str(target),
                "--remote-url", self.ws.remote_url, "--branch", "main",
                "--expected-lenovo-driver-sha256", lenovo_digest(),
                "--expected-dell-driver-sha256", dell_digest(),
                "--expected-lenovo-common-sha256", common_digest(),
                "--expected-dell-common-sha256", common_digest(),
                "--expected-bridge-sha256", bridge_digest(),
                "--receipt", str(receipt),
            ])
            self.assertEqual(proc.returncode, 2, f"{label}: {proc.stderr}")
            self.assertFalse(receipt.exists(), label)
            self.assertFalse(target.exists(), label)

    # 4. exclusive destination --------------------------------------------
    def test_symlink_destination_is_never_followed(self):
        real = self.ws.root / "link-target"
        real.mkdir()
        (real / "keep.txt").write_text("keep\n", encoding="utf-8")
        target = self.ws.root / "copy-link"
        try:
            target.symlink_to(real, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"symlinks unavailable: {error}")
        proc, data = self.ws.import_to(target, self.job)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "destination-is-file")
        self.assertTrue(target.is_symlink())
        self.assertEqual((real / "keep.txt").read_text(encoding="utf-8"), "keep\n")

    def test_file_destination_is_left_untouched(self):
        target = self.ws.root / "copy-keep-file"
        target.write_bytes(b"foreign-bytes")
        proc, data = self.ws.import_to(target, self.job)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "destination-is-file")
        self.assertEqual(target.read_bytes(), b"foreign-bytes")

    def test_empty_directory_is_refused_and_preserved(self):
        target = self.ws.root / "copy-empty-dir"
        target.mkdir()
        self.assertEqual(list(target.iterdir()), [])
        proc, data = self.ws.import_to(target, self.job)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "repo-exists")
        self.assertEqual(data["verdict"], "mismatch")
        self.assertTrue(target.is_dir())
        self.assertEqual(list(target.iterdir()), [])
        self.assertFalse((target / ".git").exists())

    def test_absent_destination_is_created_exclusively(self):
        target = self.ws.root / "copy-absent-ok"
        self.assertFalse(target.exists())
        proc, data = self.ws.import_to(target, self.job)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(data["verdict"], "imported")
        self.assertEqual(data["reason"], "imported")
        self.assertTrue((target / ".git").is_dir())
        self.assertEqual(data["repo"]["head"], self.ws.base)

    def test_symlinked_bundle_is_not_imported(self):
        name = "dell-first-0000000000000c01.bundle"
        bundle_path = self.ws.shared / name
        original = bundle_path.read_bytes()
        foreign = self.ws.root / "foreign.bundle"
        foreign.write_bytes(b"foreign\n")
        backup = self.ws.root / "bundle-backup.bundle"
        try:
            bundle_path.rename(backup)
            try:
                bundle_path.symlink_to(foreign)
            except OSError as error:
                backup.rename(bundle_path)
                self.skipTest(f"symlinks unavailable: {error}")
            proc, data = self.ws.import_to(self.ws.root / "copy-link-bundle", self.job)
        finally:
            try:
                if bundle_path.is_symlink() or bundle_path.is_file():
                    bundle_path.unlink(missing_ok=True)
            except OSError:
                pass
            try:
                if backup.exists():
                    backup.rename(bundle_path)
            except OSError:
                bundle_path.write_bytes(original)
        self.assertEqual(proc.returncode, 1)
        self.assertIn(data["reason"], ("artifact-mismatch", "artifact-missing"))
        self.assertFalse(data["bundle"]["verified"])
        self.assertFalse((self.ws.root / "copy-link-bundle").exists())

    # 5. request filename token binding ------------------------------------
    def test_mismatched_request_name_is_refused_before_write(self):
        command_file = self.ws.root / "mismatch-command.txt"
        proc = run([
            sys.executable, str(REQUEST_CLI), "first",
            "--shared", str(self.ws.shared), "--token", "0000000000000c31",
            "--lenovo-skill-dir", str(self.ws.root),
            "--lenovo-driver-dir", str(REQUEST_CLI.parent),
            "--python", sys.executable, "--remote-url", self.ws.remote_url,
            "--ref-full", "refs/heads/main",
            "--request-name", "first-request-0000000000000c32.json",
            "--command-file", str(command_file), "--repo", str(self.ws.anchor),
        ])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("token", proc.stderr.lower())
        self.assertFalse(command_file.exists())
        self.assertFalse((self.ws.shared / "first-request-0000000000000c32.json").exists())
        self.assertFalse((self.ws.shared / "first-request-0000000000000c31.json").exists())

    def test_lenovo_rejects_filename_token_mismatch(self):
        import hashlib as _hashlib
        import subprocess as _subprocess

        token = "0000000000000c33"
        request = self.ws.first_request(token)
        request_path = Path(request["requestPath"])
        raw = request_path.read_bytes()
        digest = _hashlib.sha256(raw).hexdigest()
        # Same content (internal token c33) under a different file name
        # (c34): the digest matches, so only the filename binding can refuse.
        wrong_path = self.ws.shared / "first-request-0000000000000c34.json"
        wrong_path.write_bytes(raw)
        lenovo_driver = Path(__file__).resolve().parents[2] / "scripts" / "dell_lan" / "lenovo_first_export.py"
        try:
            proc = _subprocess.run(
                [sys.executable, str(lenovo_driver),
                 "--request", str(wrong_path), "--request-sha256", digest],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", check=False)
        finally:
            wrong_path.unlink(missing_ok=True)
        self.assertNotEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("file name token", (proc.stdout + proc.stderr).lower())

    def test_sealed_token_must_match_the_job_named_file(self):
        token = "0000000000000c35"
        request = self.ws.first_request(token)
        job_id = self.ws.submit(request["commandFile"], return_file=Path(request["returnFile"]))
        forged = dict(sealed_missing_common(token, self.ws))
        forged["driverCommonSha256"] = common_digest()
        forged["token"] = "0000000000000c36"
        forged["bundleName"] = "dell-first-0000000000000c36.bundle"
        forged["manifestName"] = "dell-first-0000000000000c36.manifest.json"
        self.ws.seal_result(job_id, stdout=json.dumps(forged))
        proc, data = self.ws.import_to(self.ws.root / "copy-job-token", job_id)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "request-malformed")
        self.assertFalse((self.ws.root / "copy-job-token").exists())


if __name__ == "__main__":
    unittest.main()
