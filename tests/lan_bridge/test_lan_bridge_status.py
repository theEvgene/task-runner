"""One verified job status: finished/waiting/unknown/refused for one id.

Covers issue #59: a single ``lan-job-status/1`` readback with an owner, a
deadline and an occupancy flag only from signed queue data; UNKNOWN keeps a
signed lease held and never suggests a resubmit; every refusal carries at
most one safe next action and no secret or absolute host path.

Local fixture only: temp dirs plus an ephemeral key, no live second host.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
BRIDGE_DIR = HERE.parents[1] / "scripts" / "lan_bridge"
GET_SCRIPT = BRIDGE_DIR / "get.py"
if str(BRIDGE_DIR) not in sys.path:
    sys.path.insert(0, str(BRIDGE_DIR))

import lan_bridge as bridge  # noqa: E402
import wait as waitmod  # noqa: E402

_PY_CANDIDATE = r"C:\Users\rb199787\AppData\Local\Programs\Python\Python313\python.exe"

REQUIRED = {"schema", "jobId", "outcome", "nextAction", "proof"}
OPTIONAL = {"observedOwner", "occupied", "deadline", "terminal"}


def make_key(directory: Path) -> tuple:
    raw = os.urandom(32)
    key_file = directory / "key.bin"
    key_file.write_bytes(raw)
    return raw, key_file


def run_cli(*argv):
    exe = _PY_CANDIDATE if Path(_PY_CANDIDATE).exists() else sys.executable
    return subprocess.run([exe, str(GET_SCRIPT), *argv],
                          capture_output=True, text=True)


def job_digest(queue: str, job_id: str) -> str:
    return hashlib.sha256(bridge.read(Path(queue) / "jobs", job_id)).hexdigest()


def claim_record(job_id: str, envelope_sha256: str,
                 instance: str = "lenovo-worker-1", pid: int = 4242) -> dict:
    return {"schema": "lenovo-job-claim/1", "kind": "claim", "id": job_id,
            "owner": {"instance": instance, "pid": pid},
            "generation": "ab" * 16, "claimed_at": "t0", "resources": [],
            "job_envelope_sha256": envelope_sha256,
            "state": "owned-unknown-until-signed-result"}


def publish_claim(queue: str, key: bytes, job_id: str,
                  instance: str = "lenovo-worker-1", pid: int = 4242,
                  envelope_sha256: str | None = None) -> dict:
    claim = claim_record(job_id, envelope_sha256 or job_digest(queue, job_id),
                         instance=instance, pid=pid)
    bridge.publish(os.path.join(queue, "claims"), job_id,
                   bridge.seal(claim, key))
    return claim


class StatusCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.queue = str(self.root / "queue")
        self.key, self.key_file = make_key(self.root)
        self.key_hex = self.key.hex()

    def tearDown(self):
        self.tmp.cleanup()

    def submit(self) -> str:
        return bridge.submit_job(self.queue, self.key, "cmd", "C:\\s",
                                 timeout_seconds=300)

    def publish_result(self, job_id: str, **kwargs) -> None:
        payload = bridge.build_result_payload(job_id, "completed", **kwargs)
        bridge.publish(os.path.join(self.queue, "results"), job_id,
                       bridge.seal(payload, self.key))

    def assert_single_shape(self, status: dict) -> None:
        self.assertTrue(REQUIRED <= set(status), status)
        self.assertLessEqual(set(status), REQUIRED | OPTIONAL)
        self.assertEqual(set(status["proof"]), {"result", "job", "lease"})
        self.assertEqual(status["schema"], "lan-job-status/1")

    def assert_hygiene(self, status: dict) -> None:
        blob = json.dumps(status)
        self.assertNotIn(self.key_hex, blob)
        self.assertNotIn(str(self.root), blob)
        self.assertNotIn("C:\\", blob)
        self.assertNotIn("key.bin", blob)
        self.assertNotIn("running", blob.lower())


class FinishedTests(StatusCase):
    def test_success_is_finished_with_terminal_evidence(self):
        job_id = self.submit()
        self.publish_result(job_id, exit_code=0, stdout="ok")
        status = waitmod.job_status(self.queue, self.key, job_id)
        self.assert_single_shape(status)
        self.assert_hygiene(status)
        self.assertEqual(status["outcome"], "finished")
        self.assertEqual(status["jobId"], job_id)
        self.assertEqual(status["proof"]["result"], "signed")
        self.assertEqual(status["terminal"]["outcome"], "success")
        self.assertEqual(status["terminal"]["exitCode"], 0)
        self.assertNotIn("observedOwner", status)
        self.assertNotIn("occupied", status)
        self.assertNotIn("deadline", status)

    def test_verified_worker_timeout_is_never_success(self):
        job_id = self.submit()
        self.publish_result(job_id, exit_code=0, timed_out=True)
        status = waitmod.job_status(self.queue, self.key, job_id)
        self.assert_single_shape(status)
        self.assertEqual(status["outcome"], "finished")
        self.assertEqual(status["terminal"]["outcome"], "worker_timeout")
        self.assertNotEqual(status["terminal"]["outcome"], "success")


class AbsenceTests(StatusCase):
    def test_signed_lease_is_held_and_never_resubmitted(self):
        job_id = self.submit()
        publish_claim(self.queue, self.key, job_id, pid=777)
        claim_path = Path(self.queue) / "claims" / f"{job_id}.json"
        before = claim_path.read_bytes()
        status = waitmod.job_status(self.queue, self.key, job_id)
        self.assert_single_shape(status)
        self.assert_hygiene(status)
        self.assertEqual(status["outcome"], "waiting")
        self.assertTrue(status["occupied"])
        self.assertEqual(status["observedOwner"],
                         {"instance": "lenovo-worker-1", "pid": 777})
        self.assertEqual(status["proof"]["lease"], "signed")
        self.assertEqual(status["proof"]["job"], "signed")
        self.assertEqual(status["deadline"]["seconds"], 300)
        self.assertIn(job_id, status["nextAction"])
        self.assertIn("never resubmit", status["nextAction"])
        # Lease and job stay untouched: no release, no second submission.
        self.assertEqual(claim_path.read_bytes(), before)
        self.assertEqual(
            len(list((Path(self.queue) / "jobs").glob("*.json"))), 1)
        self.assertFalse((Path(self.queue) / "results").exists())

    def test_no_lease_is_unknown_without_owner(self):
        job_id = self.submit()
        status = waitmod.job_status(self.queue, self.key, job_id)
        self.assert_single_shape(status)
        self.assert_hygiene(status)
        self.assertEqual(status["outcome"], "unknown")
        self.assertNotIn("observedOwner", status)
        self.assertNotIn("occupied", status)
        self.assertEqual(status["proof"]["lease"], "absent")
        self.assertIn(job_id, status["nextAction"])
        self.assertIn("never resubmit", status["nextAction"])
        self.assertEqual(status["deadline"]["seconds"], 300)

    def test_lease_with_foreign_binding_is_not_an_owner(self):
        job_id = self.submit()
        publish_claim(self.queue, self.key, job_id, envelope_sha256="f" * 64)
        status = waitmod.job_status(self.queue, self.key, job_id)
        self.assertEqual(status["outcome"], "unknown")
        self.assertEqual(status["proof"]["lease"], "unverifiable")
        self.assertNotIn("observedOwner", status)

    def test_claim_sealed_for_another_id_is_not_an_owner(self):
        job_id = self.submit()
        other = self.submit()
        foreign = claim_record(other, job_digest(self.queue, other))
        bridge.publish(os.path.join(self.queue, "claims"), job_id,
                       bridge.seal(foreign, self.key))
        status = waitmod.job_status(self.queue, self.key, job_id)
        self.assertEqual(status["outcome"], "unknown")
        self.assertEqual(status["proof"]["lease"], "unverifiable")
        self.assertNotIn("observedOwner", status)

    def test_unsubmitted_id_is_unknown_without_invented_deadline(self):
        # The owner-provided queue mount exists; only this id was never
        # submitted, so nothing may be reported for it.
        Path(self.queue).mkdir(parents=True, exist_ok=True)
        status = waitmod.job_status(self.queue, self.key, "a" * 32)
        self.assert_single_shape(status)
        self.assertEqual(status["outcome"], "unknown")
        self.assertEqual(status["proof"]["job"], "absent")
        self.assertNotIn("deadline", status)
        self.assertNotIn("observedOwner", status)
        self.assertIn("never resubmit", status["nextAction"])


class RefusalTests(StatusCase):
    def _assert_one_action(self, status: dict) -> None:
        action = status["nextAction"].lower()
        self.assertEqual(status["outcome"], "refused")
        for forbidden in ("re-read", "resubmit", "re-run", "new job id",
                          "release"):
            self.assertNotIn(forbidden, action)
        parts = [part.strip() for part in action.split(";")]
        self.assertEqual(len(parts), 2, status["nextAction"])
        self.assertTrue(parts[0])
        self.assertTrue(parts[1].startswith("never"), status["nextAction"])

    def test_tampered_result_is_refused_not_finished(self):
        job_id = self.submit()
        self.publish_result(job_id, exit_code=0)
        raw_path = Path(self.queue) / "results" / f"{job_id}.json"
        raw = bytearray(raw_path.read_bytes())
        raw[40] ^= 0x01
        raw_path.write_bytes(bytes(raw))
        status = waitmod.job_status(self.queue, self.key, job_id)
        self.assert_single_shape(status)
        self.assert_hygiene(status)
        self._assert_one_action(status)
        self.assertEqual(status["proof"]["result"], "unverifiable")
        self.assertNotIn("terminal", status)

    def test_result_of_another_id_is_refused(self):
        job_id = self.submit()
        foreign = "e" * 32
        bridge.publish(os.path.join(self.queue, "results"), job_id,
                       bridge.seal(bridge.build_result_payload(
                           foreign, "completed", exit_code=0), self.key))
        status = waitmod.job_status(self.queue, self.key, job_id)
        self._assert_one_action(status)
        self.assertEqual(status["proof"]["result"], "unverifiable")

    def test_unavailable_queue_refusal_names_no_path(self):
        missing = str(self.root / "no-queue")
        with self.assertRaises(bridge.BridgeError) as ctx:
            waitmod.job_status(missing, self.key, "c" * 32)
        message = str(ctx.exception)
        self.assertIn("queue unavailable", message.lower())
        self.assertNotIn(missing, message)
        self.assertNotIn(self.key_hex, message)


class ReportReuseTests(StatusCase):
    def test_waiter_reports_carry_the_same_shape(self):
        job_id = self.submit()
        publish_claim(self.queue, self.key, job_id)
        _, report = waitmod.get_result(
            self.queue, self.key, job_id, window_timeout=0.2,
            poll_seconds=0.05, max_windows=1)
        status = report["status"]
        self.assert_single_shape(status)
        self.assert_hygiene(status)
        self.assertEqual(status["outcome"], "waiting")
        self.assertEqual(report["action"], status["nextAction"])
        self.assertNotIn("summary", report)  # one job-state object only
        _, windows_report = waitmod.wait_many_windows(
            self.queue, self.key, job_id, total_timeout=0.2,
            window_timeout=0.1, poll_seconds=0.05)
        self.assertEqual(set(windows_report["status"]), set(status))
        self.publish_result(job_id, exit_code=0, stdout="late")
        payload, found = waitmod.get_result(
            self.queue, self.key, job_id, window_timeout=2,
            poll_seconds=0.05, max_windows=2)
        self.assertEqual(payload["id"], job_id)
        self.assertEqual(found["status"], waitmod.status_from_result(payload))
        self.assertEqual(found["status"]["outcome"], "finished")
        self.assertNotIn("summary", found)

    def test_status_cli_exit_codes_and_no_secret(self):
        job_id = self.submit()
        publish_claim(self.queue, self.key, job_id)
        waiting = run_cli("--queue", self.queue, "--key-file",
                          str(self.key_file), "--id", job_id, "--status")
        self.assertEqual(waiting.returncode, 3, waiting.stderr)
        self.assertEqual(json.loads(waiting.stdout)["outcome"], "waiting")
        self.assertNotIn(self.key_hex, waiting.stdout + waiting.stderr)
        self.publish_result(job_id, exit_code=0)
        done = run_cli("--queue", self.queue, "--key-file",
                       str(self.key_file), "--id", job_id, "--status")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["outcome"], "finished")
        Path(self.queue, "results", f"{job_id}.json").write_bytes(b"not-json")
        refused = run_cli("--queue", self.queue, "--key-file",
                          str(self.key_file), "--id", job_id, "--status")
        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertEqual(json.loads(refused.stdout)["outcome"], "refused")
        self.assertNotIn(self.key_hex, refused.stdout + refused.stderr)


if __name__ == "__main__":
    unittest.main()
