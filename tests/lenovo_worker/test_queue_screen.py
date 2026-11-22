"""Local queue/worker screen fixtures (issue #169, stdlib only).

Covers the read-only snapshot on temp dirs plus an ephemeral key: pending,
active (one and two), finished, refused and connection-lost states; start
confirmation; owner-liveness honesty; and privacy (no command, path, output
or key bytes in the snapshot or its text).

No live Lenovo run: the worker/owner pids here are fixture values, and an
alive-owner case uses this test process's own pid only as an observable
local fact. Live screen validation on an isolated Lenovo queue stays manual
(see docs/lenovo-queue-screen.md).
"""

import hashlib
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from launch_fixtures import (  # noqa: E402
    BRIDGE, SCRIPTS, TempRoot, make_key, make_queue, seal_job)
sys.path.insert(0, str(BRIDGE))
sys.path.insert(0, str(SCRIPTS))

import lan_bridge  # noqa: E402
import queue_status as screen  # noqa: E402

SCREEN_CLI = SCRIPTS / "queue_screen.py"
SECRET_COMMAND = "fixture-secret-command-proof-9z"


def job_digest(queue: Path, job_id: str) -> str:
    return hashlib.sha256(
        (queue / "jobs" / f"{job_id}.json").read_bytes()).hexdigest()


def publish_claim(queue: Path, key: bytes, job_id: str, pid: int,
                  generation: str = "ab" * 16) -> dict:
    claim = {"schema": "lenovo-job-claim/1", "kind": "claim", "id": job_id,
             "owner": {"instance": "fixture-owner", "pid": pid},
             "generation": generation, "claimed_at": "2026-11-22T00:00:00+00:00",
             "resources": [], "job_envelope_sha256": job_digest(queue, job_id),
             "state": "owned-unknown-until-signed-result"}
    lan_bridge.publish(str(queue / "claims"), job_id,
                       lan_bridge.seal(claim, key))
    return claim


def publish_start(queue: Path, key: bytes, job_id: str, claim: dict,
                  child_pid: int = 424242) -> dict:
    record = {"schema": "lenovo-job-start/1", "kind": "start", "id": job_id,
              "owner": dict(claim["owner"]), "generation": claim["generation"],
              "child_pid": child_pid, "started_at": "2026-11-22T00:00:00+00:00",
              "resources": [], "job_envelope_sha256": claim["job_envelope_sha256"]}
    lan_bridge.publish(str(queue / "starts"), job_id,
                       lan_bridge.seal(record, key))
    return record


def publish_result(queue: Path, key: bytes, job_id: str, **fields) -> None:
    payload = lan_bridge.build_result_payload(job_id, **fields)
    lan_bridge.publish(str(queue / "results"), job_id,
                       lan_bridge.seal(payload, key))


class QueueScreenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = TempRoot()
        self.addCleanup(self.root.cleanup)
        self.key_file = self.root.root / "key.bin"
        self.key = make_key(self.key_file)
        self.queue = make_queue(self.root.root)
        self.state = self.root.root / "state"
        self.state.mkdir()

    def snapshot(self, **kwargs) -> dict:
        return screen.build_snapshot(self.queue, self.key, self.state, **kwargs)

    def assert_private(self, snapshot: dict, text: str) -> None:
        blob = json.dumps(snapshot) + text
        self.assertNotIn(SECRET_COMMAND, blob)
        self.assertNotIn(self.key.hex(), blob)
        self.assertNotIn("fixture-secret", blob)
        self.assertNotIn("running", text.lower())

    def test_pending_job_is_unknown_not_healthy(self) -> None:
        job_id = seal_job(self.queue, self.key, command=SECRET_COMMAND)
        snapshot = self.snapshot()
        self.assertEqual(snapshot["connection"]["state"], "ok")
        self.assertEqual(snapshot["counts"]["unknown"], 1)
        self.assertEqual(snapshot["counts"]["active"], 0)
        entry = snapshot["pending"][0]
        self.assertEqual((entry["id"], entry["outcome"]), (job_id, "unknown"))
        self.assertFalse(entry["confirmed"])
        text = screen.render_text(snapshot)
        self.assertIn(job_id, text)
        self.assert_private(snapshot, text)

    def test_active_job_is_waiting_with_unconfirmed_start(self) -> None:
        job_id = seal_job(self.queue, self.key, command=SECRET_COMMAND)
        publish_claim(self.queue, self.key, job_id, pid=os.getpid())
        snapshot = self.snapshot()
        self.assertEqual(snapshot["counts"]["active"], 1)
        entry = snapshot["active"][0]
        self.assertEqual(entry["outcome"], "waiting")
        self.assertFalse(entry["confirmed"])
        self.assertFalse(entry["startConfirmed"])
        self.assertTrue(entry["ownerAlive"])
        self.assertIn("ownership, not liveness", entry["detail"])
        text = screen.render_text(snapshot)
        self.assertIn("UNCONFIRMED", text)
        self.assert_private(snapshot, text)

    def test_two_active_jobs_with_confirmed_starts(self) -> None:
        ids = [seal_job(self.queue, self.key, command=SECRET_COMMAND)
               for _ in range(2)]
        for job_id in ids:
            claim = publish_claim(self.queue, self.key, job_id,
                                  pid=os.getpid())
            publish_start(self.queue, self.key, job_id, claim)
        snapshot = self.snapshot()
        self.assertEqual(snapshot["counts"]["active"], 2)
        self.assertEqual(sorted(e["id"] for e in snapshot["active"]), sorted(ids))
        self.assertTrue(all(e["startConfirmed"] for e in snapshot["active"]))
        text = screen.render_text(snapshot)
        for job_id in ids:
            self.assertIn(job_id, text)
        self.assert_private(snapshot, text)

    def test_dead_owner_stays_unknown_never_success(self) -> None:
        job_id = seal_job(self.queue, self.key, command=SECRET_COMMAND)
        publish_claim(self.queue, self.key, job_id, pid=999999999)
        entry = self.snapshot()["active"][0]
        self.assertEqual(entry["outcome"], "waiting")
        self.assertFalse(entry["ownerAlive"])
        self.assertIn("outcome still unknown", entry["detail"])

    def test_finished_outcomes_verified_only(self) -> None:
        ok_id = seal_job(self.queue, self.key, command=SECRET_COMMAND)
        publish_result(self.queue, self.key, ok_id, status="completed",
                       exit_code=0)
        fail_id = seal_job(self.queue, self.key, command=SECRET_COMMAND)
        publish_result(self.queue, self.key, fail_id, status="completed",
                       exit_code=1)
        timeout_id = seal_job(self.queue, self.key, command=SECRET_COMMAND)
        publish_result(self.queue, self.key, timeout_id, status="error",
                       exit_code=124, timed_out=True)
        snapshot = self.snapshot()
        by_id = {entry["id"]: entry for entry in snapshot["recent"]}
        self.assertEqual(by_id[ok_id]["outcome"], "success")
        self.assertEqual(by_id[fail_id]["outcome"], "worker_failure")
        self.assertEqual(by_id[timeout_id]["outcome"], "worker_timeout")
        self.assertTrue(all(entry["confirmed"] for entry in by_id.values()))
        text = screen.render_text(snapshot)
        self.assert_private(snapshot, text)

    def test_unverifiable_result_is_refused_not_finished(self) -> None:
        job_id = seal_job(self.queue, self.key, command=SECRET_COMMAND)
        (self.queue / "results" / f"{job_id}.json").write_bytes(
            b"not-a-signed-envelope")
        snapshot = self.snapshot()
        refused = [entry for entry in snapshot["recent"]
                   if entry["id"] == job_id]
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0]["outcome"], "refused")
        self.assertFalse(refused[0]["confirmed"])
        self.assertIn(job_id, screen.render_text(snapshot))

    def test_missing_queue_is_connection_lost(self) -> None:
        gone = self.root.root / "no-queue"
        snapshot = screen.build_snapshot(gone, self.key, self.state)
        self.assertEqual(snapshot["connection"]["state"], "lost")
        self.assertEqual(snapshot["counts"]["active"], 0)
        text = screen.render_text(snapshot)
        self.assertIn("LOST", text)
        self.assertIn("nothing below is health evidence", text)

    def test_stale_worker_pid_is_stopped_not_up(self) -> None:
        (self.state / "worker.pid").write_text(
            json.dumps({"pid": 999999999, "marker": "worker.py",
                        "started": "t"}), encoding="utf-8")
        snapshot = self.snapshot()
        self.assertEqual(snapshot["worker"]["state"], "stopped")
        self.assertIn("stale record", snapshot["worker"]["detail"])

    def test_cli_once_prints_screen_and_bad_key_refuses(self) -> None:
        job_id = seal_job(self.queue, self.key, command=SECRET_COMMAND)
        run = subprocess.run(
            [sys.executable, os.fspath(SCREEN_CLI), "--queue",
             os.fspath(self.queue), "--key-file", os.fspath(self.key_file),
             "--once"], capture_output=True, text=True, timeout=60,
            check=False)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn(job_id, run.stdout)
        self.assertNotIn(SECRET_COMMAND, run.stdout)
        bad = self.root.root / "bad.bin"
        bad.write_bytes(b"short")
        refused = subprocess.run(
            [sys.executable, os.fspath(SCREEN_CLI), "--queue",
             os.fspath(self.queue), "--key-file", os.fspath(bad),
             "--once"], capture_output=True, text=True, timeout=60,
            check=False)
        self.assertEqual(refused.returncode, 2)


if __name__ == "__main__":
    unittest.main()
