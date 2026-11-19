"""Fixture-only interop for LenovoJobRecovery.exe (no live queue, no secrets).

Builds temp queues + a temp 32-byte key, seals job/claim/start with the
canonical bridge, then drives the exe through --self-test, --verify-only,
dry-run proof writing, and --invoke, checking every artifact with the
canonical reader/worker. UNC custody refuses without network I/O.
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
WORKER_DIR = ROOT / "scripts" / "lenovo_worker"
BRIDGE_DIR = ROOT / "scripts" / "lan_bridge"
EXE = WORKER_DIR / "job_recovery" / "LenovoJobRecovery.exe"
WORKER_PY = WORKER_DIR / "worker.py"
sys.path.insert(0, str(BRIDGE_DIR))
sys.path.insert(0, str(WORKER_DIR))

import job_lifecycle  # noqa: E402
import job_ownership  # noqa: E402
import lan_bridge  # noqa: E402
import proc  # noqa: E402

JOB1 = "aa" * 16
JOB2 = "bb" * 16


def run_exe(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([os.fspath(EXE), *argv], capture_output=True,
                          text=True, timeout=180)


def receipt_of(completed: subprocess.CompletedProcess[str]) -> dict:
    lines = completed.stdout.strip().splitlines()
    if not lines:
        return {}
    return json.loads(lines[-1])


def dead_pid() -> int:
    started = subprocess.Popen([sys.executable, "-c", "pass"])
    started.wait(timeout=60)
    assert not proc.is_alive(started.pid)
    return started.pid


@unittest.skipUnless(EXE.exists(), "recovery exe not built")
class JobRecoveryExeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.key = bytes(range(32))
        self.key_file = self.root / "key.bin"
        self.key_file.write_bytes(self.key)

    def make_queue(self, job_id: str) -> tuple[Path, str, dict, int]:
        queue = self.root / ("queue-" + job_id[:2])
        (queue / "jobs").mkdir(parents=True)
        (queue / "results").mkdir()
        workdir = self.root / ("work-" + job_id[:2])
        workdir.mkdir(parents=True, exist_ok=True)
        payload = lan_bridge.build_job_payload(
            job_id=job_id, command="echo fixture",
            working_directory=os.fspath(workdir), timeout_seconds=60)
        sealed = lan_bridge.seal(payload, self.key)
        lan_bridge.publish(str(queue / "jobs"), job_id, sealed)
        resources = ["exclusive:global",
                     job_lifecycle.resource_identity(os.fspath(workdir))]
        owner = {"instance": "fixture-owner", "pid": dead_pid()}
        child = dead_pid()
        generation = hashlib.sha256(job_id.encode()).hexdigest()[:32]
        claim = {"schema": job_ownership.CLAIM_SCHEMA, "kind": "claim",
                 "id": job_id, "owner": owner, "generation": generation,
                 "claimed_at": "2026-01-01T00:00:00+00:00",
                 "resources": resources,
                 "job_envelope_sha256": hashlib.sha256(sealed).hexdigest(),
                 "state": "owned-unknown-until-signed-result"}
        lan_bridge.publish(str(queue / "claims"), job_id,
                           lan_bridge.seal(claim, self.key))
        start = {"schema": job_ownership.START_SCHEMA, "kind": "start",
                 "id": job_id, "owner": owner, "generation": generation,
                 "child_pid": child, "started_at": "2026-01-01T00:00:01+00:00",
                 "resources": resources,
                 "job_envelope_sha256": hashlib.sha256(sealed).hexdigest()}
        lan_bridge.publish(str(queue / "starts"), job_id,
                           lan_bridge.seal(start, self.key))
        return queue, generation, owner, child

    def config_for(self, queue: Path, name: str) -> Path:
        shared = self.root / "shared"
        shared.mkdir(exist_ok=True)
        cfg = {"queueDir": os.fspath(queue),
               "sharedDir": os.fspath(shared),
               "keyFile": os.fspath(self.key_file), "keyFormat": "raw",
               "python": sys.executable,
               "workerDir": os.fspath(WORKER_DIR)}
        path = self.root / name
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_self_test_passes(self) -> None:
        completed = run_exe("--self-test")
        self.assertEqual(completed.returncode, 0, completed.stderr[-500:])
        body = json.loads(completed.stdout.strip().splitlines()[0])
        self.assertEqual((body["passed"], body["failed"]), (13, 0))

    def test_job_id_required_no_default(self) -> None:
        queue, _, _, _ = self.make_queue(JOB1)
        cfg = self.config_for(queue, "config-noid.json")
        proof = self.root / "noid.proof.json"
        # No --job-id: must refuse before any queue/key/proof access.
        # The old build defaulted to a real id; the fixed build has none.
        missing = run_exe("--config", os.fspath(cfg),
                          "--proof-out", os.fspath(proof), "--verify-only")
        receipt = receipt_of(missing)
        self.assertEqual(missing.returncode, 2, missing.stderr[-500:])
        self.assertEqual(receipt.get("reason"), "job-id-invalid")
        self.assertFalse(proof.exists())
        self.assertFalse((queue / "results" / (JOB1 + ".json")).exists())
        # A garbage id refuses the same way.
        bad = run_exe("--config", os.fspath(cfg), "--job-id", "not-hex",
                      "--proof-out", os.fspath(proof), "--verify-only")
        self.assertEqual(bad.returncode, 2, bad.stderr[-500:])
        self.assertEqual(receipt_of(bad).get("reason"), "job-id-invalid")

    def test_verify_and_dry_run_settle_canonically(self) -> None:
        queue, generation, owner, child = self.make_queue(JOB1)
        cfg = self.config_for(queue, "config1.json")
        proof = self.root / (JOB1 + ".proof.json")

        verify = run_exe("--config", os.fspath(cfg), "--job-id", JOB1,
                         "--proof-out", os.fspath(proof), "--verify-only")
        receipt = receipt_of(verify)
        self.assertEqual(verify.returncode, 0, verify.stderr[-500:])
        self.assertEqual(receipt.get("outcome"), "ready-to-reconcile")
        self.assertNotIn(os.fspath(queue), verify.stdout)
        self.assertNotIn("keyFile", verify.stdout)

        dry = run_exe("--config", os.fspath(cfg), "--job-id", JOB1,
                      "--proof-out", os.fspath(proof))
        self.assertEqual(dry.returncode, 0, dry.stderr[-500:])
        self.assertTrue(proof.exists())

        opened = lan_bridge.open_envelope(
            proof.read_bytes(), self.key, "reconciliation", JOB1)
        self.assertEqual(opened.get("schema"),
                         "lenovo-job-reconciliation/2")
        self.assertEqual(opened.get("claim_generation"), generation)
        evidence = opened.get("evidence") or {}
        self.assertTrue(evidence.get("tree_absent"))
        self.assertEqual(evidence.get("child_pid"), child)
        self.assertEqual(evidence.get("owner"), owner)

        worker = subprocess.run(
            [sys.executable, os.fspath(WORKER_PY), "--queue",
             os.fspath(queue), "--key-file", os.fspath(self.key_file),
             "--reconcile-id", JOB1, "--reconcile-proof", os.fspath(proof)],
            capture_output=True, text=True, timeout=180)
        self.assertEqual(worker.returncode, 0, worker.stderr[-500:])
        result = lan_bridge.open_envelope(
            (queue / "results" / (JOB1 + ".json")).read_bytes(),
            self.key, "result", JOB1)
        self.assertEqual(result.get("status"), "error")
        self.assertEqual(result.get("termination"), "unknown")
        self.assertIsNone(result.get("exit_code"))
        self.assertEqual(result.get("reconciliation", {}).get(
            "outcome_derivation"), "unobserved-crash")
        release = lan_bridge.open_envelope(
            (queue / "releases" / (JOB1 + ".json")).read_bytes(),
            self.key, "release", JOB1)
        self.assertEqual(release.get("generation"), generation)
        self.assertEqual(release.get("owner"), owner)

    def test_invoke_reconciles_and_replays(self) -> None:
        queue, _, _, _ = self.make_queue(JOB2)
        cfg = self.config_for(queue, "config2.json")
        proof = self.root / (JOB2 + ".proof.json")

        first = run_exe("--config", os.fspath(cfg), "--job-id", JOB2,
                        "--proof-out", os.fspath(proof), "--invoke")
        receipt = receipt_of(first)
        self.assertEqual(first.returncode, 0,
                         first.stderr[-500:] + first.stdout[-500:])
        self.assertEqual(receipt.get("outcome"), "reconciled")
        before_result = (queue / "results" / (JOB2 + ".json")).read_bytes()
        before_release = (queue / "releases" / (JOB2 + ".json")).read_bytes()

        second = run_exe("--config", os.fspath(cfg), "--job-id", JOB2,
                         "--proof-out", os.fspath(proof), "--invoke")
        replay = receipt_of(second)
        self.assertEqual(second.returncode, 0, second.stderr[-500:])
        self.assertEqual(replay.get("outcome"), "reconciled")
        self.assertTrue(replay.get("evidence", {}).get("replay"))
        self.assertEqual(
            (queue / "results" / (JOB2 + ".json")).read_bytes(),
            before_result)
        self.assertEqual(
            (queue / "releases" / (JOB2 + ".json")).read_bytes(),
            before_release)

        again = subprocess.run(
            [sys.executable, os.fspath(WORKER_PY), "--queue",
             os.fspath(queue), "--key-file", os.fspath(self.key_file),
             "--reconcile-id", JOB2, "--reconcile-proof", os.fspath(proof)],
            capture_output=True, text=True, timeout=180)
        self.assertEqual(again.returncode, 0, again.stderr[-500:])

    def test_tampered_result_refused_not_replay(self) -> None:
        job_id = "dd" * 16
        queue, _, _, _ = self.make_queue(job_id)
        cfg = self.config_for(queue, "config-dd.json")
        proof = self.root / (job_id + ".proof.json")
        first = run_exe("--config", os.fspath(cfg), "--job-id", job_id,
                        "--proof-out", os.fspath(proof), "--invoke")
        self.assertEqual(first.returncode, 0,
                         first.stderr[-500:] + first.stdout[-500:])
        self.assertEqual(receipt_of(first).get("outcome"), "reconciled")
        # Authenticated tamper: same key re-seals a result with evil stdout.
        # Selected-field checks would still pass; the exact verifier must
        # refuse verify-failed and never report replay true.
        result_path = queue / "results" / (job_id + ".json")
        good = lan_bridge.open_envelope(
            result_path.read_bytes(), self.key, "result", job_id)
        good["stdout"] = "evil"
        result_path.write_bytes(lan_bridge.seal(good, self.key))
        second = run_exe("--config", os.fspath(cfg), "--job-id", job_id,
                         "--proof-out", os.fspath(proof), "--invoke")
        receipt = receipt_of(second)
        self.assertEqual(second.returncode, 2, second.stderr[-500:])
        # Either the canonical worker refuses the conflict (reconcile-failed)
        # or the exact post-verify refuses (verify-failed): both are
        # fail-closed. What must never happen is a replay/reconciled claim
        # over the tampered bytes.
        self.assertIn(receipt.get("reason"),
                      ("verify-failed", "reconcile-failed"))
        self.assertNotEqual(receipt.get("outcome"), "reconciled")
        self.assertFalse(receipt.get("evidence", {}).get("replay", False))

    def test_receipts_sanitize_absolute_paths(self) -> None:
        queue, _, _, _ = self.make_queue(JOB1)
        cfg = self.config_for(queue, "config-san.json")
        # Force a proof-write failure whose OS error embeds the absolute
        # --proof-out path: parent is an existing file, not a directory.
        blocker = self.root / "blocker"
        blocker.write_bytes(b"x")
        bad_proof = blocker / "child" / "proof.json"
        bad = run_exe("--config", os.fspath(cfg), "--job-id", JOB1,
                      "--proof-out", os.fspath(bad_proof))
        receipt = receipt_of(bad)
        self.assertEqual(bad.returncode, 2, bad.stderr[-500:])
        self.assertEqual(receipt.get("reason"), "proof-write-failed")
        combined = bad.stdout + bad.stderr
        self.assertNotIn(os.fspath(bad_proof), combined)
        self.assertNotIn(os.fspath(self.key_file), combined)
        self.assertNotIn(os.fspath(queue), combined)
        # UNC key refusal must name the custody rule, never the raw path.
        tag = "san-%d" % os.getpid()
        cfg3 = {"queueDir": os.fspath(queue),
                "sharedDir": os.fspath(self.root / "shared"),
                "keyFile": "\\\\127.0.0.1\\%s\\lan-queue.key" % tag,
                "keyFormat": "raw",
                "python": sys.executable,
                "workerDir": os.fspath(WORKER_DIR)}
        cfg3_file = self.root / "san-unc-config.json"
        cfg3_file.write_text(json.dumps(cfg3), encoding="utf-8")
        remote_key = run_exe("--config", os.fspath(cfg3_file),
                             "--job-id", JOB1, "--proof-out",
                             os.fspath(self.root / "san-p3.json"),
                             "--verify-only")
        rejected = receipt_of(remote_key)
        self.assertEqual(remote_key.returncode, 2, remote_key.stderr[-500:])
        self.assertEqual(rejected.get("reason"), "key-misplaced")
        self.assertNotIn("127.0.0.1", remote_key.stdout + remote_key.stderr)

    def test_live_owner_refused_with_no_result(self) -> None:
        job_id = "cc" * 16
        queue = self.root / "queue-cc"
        (queue / "jobs").mkdir(parents=True)
        (queue / "results").mkdir()
        workdir = self.root / "work-cc"
        workdir.mkdir(exist_ok=True)
        payload = lan_bridge.build_job_payload(
            job_id=job_id, command="echo fixture",
            working_directory=os.fspath(workdir), timeout_seconds=60)
        sealed = lan_bridge.seal(payload, self.key)
        lan_bridge.publish(str(queue / "jobs"), job_id, sealed)
        live_owner = {"instance": "fixture-owner", "pid": os.getpid()}
        generation = hashlib.sha256(job_id.encode()).hexdigest()[:32]
        resources = ["exclusive:global",
                     job_lifecycle.resource_identity(os.fspath(workdir))]
        claim = {"schema": job_ownership.CLAIM_SCHEMA, "kind": "claim",
                 "id": job_id, "owner": live_owner, "generation": generation,
                 "claimed_at": "2026-01-01T00:00:00+00:00",
                 "resources": resources,
                 "job_envelope_sha256": hashlib.sha256(sealed).hexdigest(),
                 "state": "owned-unknown-until-signed-result"}
        lan_bridge.publish(str(queue / "claims"), job_id,
                           lan_bridge.seal(claim, self.key))
        start = {"schema": job_ownership.START_SCHEMA, "kind": "start",
                 "id": job_id, "owner": live_owner, "generation": generation,
                 "child_pid": dead_pid(),
                 "started_at": "2026-01-01T00:00:01+00:00",
                 "resources": resources,
                 "job_envelope_sha256": hashlib.sha256(sealed).hexdigest()}
        lan_bridge.publish(str(queue / "starts"), job_id,
                           lan_bridge.seal(start, self.key))
        cfg = self.config_for(queue, "config3.json")
        refused = run_exe("--config", os.fspath(cfg), "--job-id", job_id,
                          "--proof-out",
                          os.fspath(self.root / "p3.json"), "--verify-only")
        receipt = receipt_of(refused)
        self.assertEqual(refused.returncode, 2, refused.stderr[-500:])
        self.assertEqual(receipt.get("reason"), "owner-active")
        self.assertFalse((queue / "results" / (job_id + ".json")).exists())

    def test_unc_custody_refuses_without_network(self) -> None:
        tag = "unc-%d" % os.getpid()
        proof = self.root / "proof.json"
        cfg = {"queueDir": "\\\\127.0.0.1\\%s\\queue" % tag,
               "sharedDir": "\\\\127.0.0.1\\%s" % tag,
               "keyFile": os.fspath(self.key_file), "keyFormat": "raw",
               "python": sys.executable,
               "workerDir": os.fspath(WORKER_DIR)}
        cfg_file = self.root / "unc-config.json"
        cfg_file.write_text(json.dumps(cfg), encoding="utf-8")
        missing = run_exe("--config", os.fspath(cfg_file), "--job-id", JOB1,
                          "--proof-out", os.fspath(proof), "--verify-only")
        self.assertEqual(missing.returncode, 2, missing.stderr[-500:])
        self.assertEqual(receipt_of(missing).get("reason"), "queue-missing")
        self.assertFalse(proof.exists())

        queue = self.root / "queue"
        (queue / "jobs").mkdir(parents=True)
        (queue / "results").mkdir()
        shared = self.root / "shared-inside"
        shared.mkdir(exist_ok=True)
        inside_key = shared / "lan-queue.key"
        inside_key.write_bytes(self.key)
        cfg2 = dict(cfg, queueDir=os.fspath(queue),
                    sharedDir=os.fspath(shared),
                    keyFile=os.fspath(inside_key))
        cfg2_file = self.root / "inside-config.json"
        cfg2_file.write_text(json.dumps(cfg2), encoding="utf-8")
        inside = run_exe("--config", os.fspath(cfg2_file), "--job-id", JOB1,
                         "--proof-out", os.fspath(self.root / "p2.json"),
                         "--verify-only")
        self.assertEqual(inside.returncode, 2, inside.stderr[-500:])
        self.assertEqual(receipt_of(inside).get("reason"), "key-misplaced")

        cfg3 = dict(cfg, queueDir=os.fspath(queue),
                    sharedDir=os.fspath(shared),
                    keyFile="\\\\127.0.0.1\\%s\\lan-queue.key" % tag)
        cfg3_file = self.root / "unc-key-config.json"
        cfg3_file.write_text(json.dumps(cfg3), encoding="utf-8")
        remote_key = run_exe("--config", os.fspath(cfg3_file),
                             "--job-id", JOB1, "--proof-out",
                             os.fspath(self.root / "p3.json"), "--verify-only")
        rejected = receipt_of(remote_key)
        self.assertEqual(remote_key.returncode, 2, remote_key.stderr[-500:])
        self.assertEqual(rejected.get("reason"), "key-misplaced")
        self.assertIn("local", str(rejected.get("evidence", "")))


if __name__ == "__main__":
    unittest.main()
