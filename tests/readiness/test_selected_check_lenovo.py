"""Fast fake-worker fixtures for the Lenovo selected check and its receipts."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "readiness" / "selected.py"
READINESS_DIR = ROOT / "scripts" / "readiness"
BRIDGE_DIR = ROOT / "scripts" / "lan_bridge"
sys.path.insert(0, str(BRIDGE_DIR))
sys.path.insert(0, str(READINESS_DIR))
import check as readiness  # noqa: E402
import lan_bridge as bridge  # noqa: E402
import selected  # noqa: E402
import selected_hosts  # noqa: E402


def _head() -> str:
    return subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()


class SelectedCheckLenovoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="selected-lenovo-"))
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.queue = self.temp / "queue"
        self.queue.mkdir()
        self.key_path = self.temp / "fixture-secret.key"
        self.key = bytes(range(32))
        self.key_path.write_bytes(self.key)
        self.target = self.temp / "target"
        self.target.mkdir()
        self.head = _head()
        self.config_path = self.temp / "readiness.json"
        self.config_path.write_text(json.dumps({
            "schema": "lan-readiness-config/1",
            "scenario": "lan-bridge",
            "queue": str(self.queue),
            "keyFile": str(self.key_path),
            "local": {"components": [], "tools": []},
            "lenovo": {
                "python": sys.executable,
                "cwd": str(self.temp),
                "components": [{"name": "checkout", "type": "directory",
                                "path": str(self.target)}],
                "tools": [{"name": "python", "command": sys.executable}],
            },
        }), encoding="utf-8")
        self.config = readiness.validate_config(
            json.loads(self.config_path.read_text(encoding="utf-8")))
        self.evidence_job = bridge.submit_job(
            str(self.queue), self.key, readiness.probe_command(self.config),
            self.config["lenovo"]["cwd"], timeout_seconds=60,
            max_output_bytes=65536, dialect="cmd")
        self._publish_evidence()
        self.descriptor_path = self.temp / "descriptor.json"
        self.descriptor = {
            "schema": "lan-selected-check/1",
            "check": {"name": "fixture", "host": "lenovo",
                      "effect": "read-only",
                      "argv": [sys.executable, "-c", "raise SystemExit(0)"],
                      "cwd": str(self.target), "expectedRevision": self.head,
                      "timeoutSeconds": 30, "maxOutputBytes": 65536},
            "lenovo": {
                "queue": str(self.queue), "keyFile": str(self.key_path),
                "keyFormat": "raw", "python": sys.executable, "git": "git",
                "readinessConfig": str(self.config_path),
                "readinessJobId": self.evidence_job,
                "requiredComponents": {"cwd": "checkout", "tool": "python"},
                "waitSeconds": 15.0, "pollSeconds": 0.05,
                "evidenceWaitSeconds": 0.5,
            },
        }

    _DEFAULT = object()

    def _publish_evidence(self, tamper: bool = False,
                          schema: str = "lenovo-readiness-probe/2",
                          revision: object = _DEFAULT,
                          cwd_digest: str | None = None) -> None:
        if revision is self._DEFAULT:
            revision = self.head
        checkout = {"name": "checkout", "state": "confirmed"}
        if schema == "lenovo-readiness-probe/2":
            checkout["observedRevision"] = revision
            checkout["cwdDigest"] = cwd_digest or hashlib.sha256(
                str(self.target).encode("utf-8")).hexdigest()
        body = {
            "schema": schema, "scenario": "lan-bridge",
            "components": [checkout, {"name": "python", "state": "confirmed"}],
        }
        result = bridge.build_result_payload(
            self.evidence_job, "completed", exit_code=0,
            stdout=json.dumps(body, separators=(",", ":")))
        sealed = bytearray(bridge.seal(result, self.key))
        if tamper:
            sealed[-3] ^= 1
        bridge.publish(self.queue / "results", self.evidence_job, bytes(sealed))

    def _republish_evidence(self, **kwargs: object) -> None:
        (self.queue / "results" / f"{self.evidence_job}.json").unlink()
        self._publish_evidence(**kwargs)

    def _job_ids(self) -> list[str]:
        jobs = self.queue / "jobs"
        return sorted(path.stem for path in jobs.glob("*.json")) \
            if jobs.exists() else []

    def _run(self, descriptor: dict | None = None,
             extra: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
        self.descriptor_path.write_text(
            json.dumps(descriptor or self.descriptor), encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--descriptor",
             str(self.descriptor_path), *extra],
            capture_output=True, text=True, check=False)

    def _fake_worker(self, receipt: dict, status: str = "completed",
                     exit_code: int | None = 0, tamper: bool = False,
                     timed_out: bool = False,
                     job_id: str | None = None) -> threading.Thread:
        def run() -> None:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                pending = [job for job in self._job_ids()
                           if job != self.evidence_job
                           and (job_id is None or job == job_id)]
                if pending:
                    job_id_found = pending[0]
                    bridge.open_envelope(
                        bridge.read(self.queue / "jobs", job_id_found),
                        self.key, "job", job_id_found)
                    payload = bridge.build_result_payload(
                        job_id_found, status, exit_code=exit_code,
                        timed_out=timed_out,
                        stdout=json.dumps(receipt, separators=(",", ":")))
                    sealed = bytearray(bridge.seal(payload, self.key))
                    if tamper:
                        sealed[-3] ^= 1
                    bridge.publish(self.queue / "results", job_id_found,
                                   bytes(sealed))
                    return
                time.sleep(0.01)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    def _receipt(self, proc: subprocess.CompletedProcess) -> dict:
        self.assertEqual(len(proc.stdout.strip().splitlines()), 1, proc.stderr)
        return json.loads(proc.stdout)

    def _check_job_id(self, receipt: dict) -> str:
        return receipt["execution"]["jobId"]

    def test_lenovo_positive_is_one_submit_bound_to_the_evidence(self) -> None:
        worker = self._fake_worker({
            "schema": "lenovo-selected-check/1", "state": "RUN",
            "reason": "completed", "exitCode": 0,
            "observedRevision": self.head})
        proc = self._run()
        worker.join(20)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        receipt = self._receipt(proc)
        self.assertEqual(receipt["outcome"], "PASS")
        self.assertEqual(receipt["requestedHost"], "lenovo")
        self.assertEqual(receipt["observedHost"], "lenovo")
        self.assertTrue(receipt["hostBound"])
        self.assertEqual(receipt["hostEvidence"], "signed-queue-result")
        self.assertEqual(receipt["revision"]["state"], "confirmed")
        self.assertEqual(receipt["revision"]["source"],
                         "signed-target-preamble")
        self.assertEqual(receipt["preflight"],
                         {"state": "confirmed", "reason": "confirmed"})
        self.assertEqual(receipt["execution"]["kind"], "signed-queue")
        self.assertRegex(self._check_job_id(receipt), r"^[0-9a-f]{32}$")
        self.assertEqual(len(self._job_ids()), 2, "exactly one check submit")
        self.assertNotIn(str(self.temp), proc.stdout + proc.stderr)
        self.assertNotIn(self.key.hex(), proc.stdout + proc.stderr)
        job = bridge.open_envelope(
            bridge.read(self.queue / "jobs", self._check_job_id(receipt)),
            self.key, "job", self._check_job_id(receipt))
        self.assertEqual(job["working_directory"], str(self.target))
        self.assertEqual(job["dialect"], "cmd")
        self.assertNotIn("SystemExit(0)", job["command"])

    def test_lenovo_readiness_evidence_revision_is_verified(self) -> None:
        payload = bridge.fetch_result(
            str(self.queue), self.key, self.evidence_job,
            timeout_seconds=1, poll_seconds=0.01)
        components = readiness.worker_components(payload, self.config)
        checkout = next(item for item in components
                        if item["name"] == "checkout")
        self.assertEqual(checkout["state"], "confirmed")
        self.assertEqual(checkout["revisionEvidence"], "verified")
        self.assertEqual(checkout["observedRevision"], self.head)

    def test_lenovo_target_preamble_not_run_is_not_pass(self) -> None:
        worker = self._fake_worker({
            "schema": "lenovo-selected-check/1", "state": "NOT_RUN",
            "reason": "revision-stale", "exitCode": None,
            "observedRevision": "0" * 40}, status="error", exit_code=78)
        proc = self._run()
        worker.join(20)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        receipt = self._receipt(proc)
        self.assertEqual(receipt["outcome"], "NOT_RUN")
        self.assertEqual(receipt["observedHost"], "lenovo")
        self.assertTrue(receipt["hostBound"])
        self.assertEqual(receipt["revision"]["state"], "stale")
        self.assertEqual(receipt["preflight"]["state"], "confirmed")
        self.assertIsNone(receipt["execution"]["exitCode"])
        self.assertEqual(len(self._job_ids()), 2)

    def test_lenovo_nonzero_exit_is_fail(self) -> None:
        worker = self._fake_worker({
            "schema": "lenovo-selected-check/1", "state": "RUN",
            "reason": "completed", "exitCode": 9,
            "observedRevision": self.head}, status="error", exit_code=9)
        proc = self._run()
        worker.join(20)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        receipt = self._receipt(proc)
        self.assertEqual(receipt["outcome"], "FAIL")
        self.assertEqual(receipt["execution"]["exitCode"], 9)

    def test_lenovo_worker_timeout_is_unknown_and_not_pass(self) -> None:
        worker = self._fake_worker({
            "schema": "lenovo-selected-check/1", "state": "RUN",
            "reason": "completed", "exitCode": 0,
            "observedRevision": self.head}, status="error", exit_code=None,
            timed_out=True)
        proc = self._run()
        worker.join(20)
        self.assertEqual(proc.returncode, 3, proc.stderr)
        receipt = self._receipt(proc)
        self.assertEqual(receipt["outcome"], "UNKNOWN")
        self.assertFalse(receipt["execution"]["verified"])
        self.assertTrue(receipt["execution"]["timedOut"])

    def test_lenovo_missing_result_is_unknown_and_never_resubmits(self) -> None:
        self.descriptor["lenovo"]["waitSeconds"] = 0.5
        proc = self._run()
        self.assertEqual(proc.returncode, 3, proc.stderr)
        receipt = self._receipt(proc)
        self.assertEqual(receipt["outcome"], "UNKNOWN")
        self.assertIsNone(receipt["observedHost"])
        self.assertFalse(receipt["hostBound"])
        job_id = self._check_job_id(receipt)
        self.assertRegex(job_id, r"^[0-9a-f]{32}$")
        self.assertIn(job_id, receipt["nextAction"])
        self.assertEqual(len(self._job_ids()), 2, "unknown must not resubmit")

    def test_invalid_result_preserves_job_id_and_requests_owner_inspection(self) -> None:
        job_id = "c" * 32
        execution = selected_hosts.lenovo_result(job_id, {}, self.head)
        self.assertEqual(execution["jobId"], job_id)
        self.assertEqual(execution["state"], "unknown")
        action = selected._next_action("UNKNOWN", {}, execution)
        self.assertIn(job_id, action)
        self.assertIn("owner", action)
        self.assertIn("never resubmit", action)
        execution["reason"] = "worker-refused"
        self.assertIn("owner", selected._next_action("UNKNOWN", {}, execution))

    def test_lenovo_run_receipt_with_a_foreign_revision_is_not_pass(self) -> None:
        payload = bridge.build_result_payload(
            "c" * 32, "completed", exit_code=0,
            stdout=json.dumps({"schema": "lenovo-selected-check/1",
                               "state": "RUN", "reason": "completed",
                               "exitCode": 0,
                               "observedRevision": "0" * 40},
                              separators=(",", ":")))
        execution = selected_hosts.lenovo_result("c" * 32, payload, self.head)
        self.assertEqual(execution["revisionState"], "stale")
        self.assertEqual(execution["state"], "not_run")
        self.assertIsNone(execution["exitCode"])
        self.assertEqual(
            selected.decide("lenovo", selected_hosts._confirmed(self.head),
                            execution),
            "NOT_RUN")

    def test_unknown_result_recovers_by_same_job_id_without_resubmitting(
            self) -> None:
        self.descriptor["lenovo"]["waitSeconds"] = 0.5
        first = self._run()
        self.assertEqual(first.returncode, 3, first.stderr)
        receipt = self._receipt(first)
        self.assertEqual(receipt["outcome"], "UNKNOWN")
        job_id = self._check_job_id(receipt)
        self.assertEqual(len(self._job_ids()), 2)
        worker = self._fake_worker({
            "schema": "lenovo-selected-check/1", "state": "RUN",
            "reason": "completed", "exitCode": 0,
            "observedRevision": self.head}, job_id=job_id)
        second = self._run(extra=("--job-id", job_id, "--wait", "15"))
        worker.join(20)
        self.assertEqual(second.returncode, 0, second.stderr)
        receipt = self._receipt(second)
        self.assertEqual(receipt["outcome"], "PASS")
        self.assertEqual(receipt["execution"]["jobId"], job_id)
        self.assertEqual(len(self._job_ids()), 2,
                         "same-id recovery submitted another job")

    def test_unverifiable_result_keeps_the_job_id_in_the_receipt(self) -> None:
        worker = self._fake_worker({
            "schema": "lenovo-selected-check/1", "state": "RUN",
            "reason": "completed", "exitCode": 0,
            "observedRevision": self.head}, tamper=True)
        proc = self._run()
        worker.join(20)
        self.assertEqual(proc.returncode, 3, proc.stderr)
        receipt = self._receipt(proc)
        self.assertEqual(receipt["outcome"], "UNKNOWN")
        self.assertFalse(receipt["execution"]["verified"])
        self.assertFalse(receipt["hostBound"])
        job_id = self._check_job_id(receipt)
        self.assertRegex(job_id, r"^[0-9a-f]{32}$")
        self.assertIn(job_id, receipt["nextAction"])
        self.assertEqual(len(self._job_ids()), 2)
        self.assertNotIn(self.key.hex(), proc.stdout + proc.stderr)

    def test_lenovo_evidence_refusals_publish_nothing(self) -> None:
        mismatch = json.loads(json.dumps(self.descriptor))
        mismatch["check"]["cwd"] = str(self.temp / "other-target")
        worker_missing = json.loads(json.dumps(self.descriptor))
        worker_missing["lenovo"]["readinessJobId"] = "f" * 32
        tool_mismatch = json.loads(json.dumps(self.descriptor))
        tool_mismatch["check"]["argv"][0] = "other-python"
        for data, reason in ((mismatch, "cwd-unproven"),
                             (worker_missing, "evidence-unbound"),
                             (tool_mismatch, "tool-unproven")):
            with self.subTest(reason=reason):
                proc = self._run(data)
                self.assertEqual(proc.returncode, 1, proc.stderr)
                receipt = self._receipt(proc)
                self.assertEqual(receipt["outcome"], "NOT_RUN")
                self.assertEqual(receipt["preflight"]["reason"], reason)
                self.assertEqual(len(self._job_ids()), 1,
                                 "a refused preflight submitted a check job")

    def test_lenovo_evidence_revision_mismatch_refuses_before_submit(self) -> None:
        self._republish_evidence(revision="0" * 40)
        proc = self._run()
        self.assertEqual(proc.returncode, 1, proc.stderr)
        receipt = self._receipt(proc)
        self.assertEqual(receipt["outcome"], "NOT_RUN")
        self.assertEqual(receipt["preflight"]["reason"], "revision-stale")
        self.assertEqual(receipt["revision"]["state"], "stale")
        self.assertEqual(receipt["revision"]["observed"], "0" * 40)
        self.assertEqual(receipt["revision"]["source"], "readiness-evidence")
        self.assertIsNone(receipt["execution"]["exitCode"])
        self.assertEqual(len(self._job_ids()), 1,
                         "a stale target revision submitted a check job")

    def test_lenovo_evidence_missing_revision_refuses_before_submit(self) -> None:
        self._republish_evidence(revision=None)
        proc = self._run()
        self.assertEqual(proc.returncode, 1, proc.stderr)
        receipt = self._receipt(proc)
        self.assertEqual(receipt["outcome"], "NOT_RUN")
        self.assertEqual(receipt["preflight"]["reason"], "revision-unconfirmed")
        self.assertEqual(receipt["revision"]["state"], "unconfirmed")
        self.assertEqual(receipt["revision"]["source"], "readiness-evidence")
        self.assertIsNone(receipt["execution"]["exitCode"])
        self.assertEqual(len(self._job_ids()), 1,
                         "missing target revision submitted a check job")

    def test_lenovo_legacy_evidence_is_not_verified(self) -> None:
        self._republish_evidence(schema="lenovo-readiness-probe/1")
        proc = self._run()
        self.assertEqual(proc.returncode, 1, proc.stderr)
        receipt = self._receipt(proc)
        self.assertEqual(receipt["outcome"], "NOT_RUN")
        self.assertEqual(receipt["preflight"]["reason"], "evidence-untrusted")
        self.assertEqual(receipt["revision"]["state"], "unconfirmed")
        self.assertEqual(receipt["revision"]["source"], "readiness-evidence")
        self.assertEqual(len(self._job_ids()), 1,
                         "legacy evidence submitted a check job")

    def test_lenovo_untrusted_cwd_binding_is_not_verified(self) -> None:
        for kwargs in ({"cwd_digest": "f" * 64},
                       {"revision": "not-a-sha"}):
            with self.subTest(kwargs=kwargs):
                self._republish_evidence(**kwargs)
                proc = self._run()
                self.assertEqual(proc.returncode, 1, proc.stderr)
                receipt = self._receipt(proc)
                self.assertEqual(receipt["outcome"], "NOT_RUN")
                self.assertEqual(receipt["preflight"]["reason"],
                                 "evidence-untrusted")
                self.assertEqual(len(self._job_ids()), 1,
                                 "untrusted evidence submitted a check job")

    def test_lenovo_missing_or_tampered_evidence_never_passes(self) -> None:
        (self.queue / "results" / f"{self.evidence_job}.json").unlink()
        proc = self._run()
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(self._receipt(proc)["preflight"]["reason"],
                         "worker-unproven")
        self.assertEqual(len(self._job_ids()), 1)
        self._publish_evidence(tamper=True)
        proc = self._run()
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("bad mac", proc.stderr)
        self.assertEqual(len(self._job_ids()), 1)


if __name__ == "__main__":
    unittest.main()

