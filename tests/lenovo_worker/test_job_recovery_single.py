"""Single-exe interactive + worker-compat tests (fixture-only).

No live queue, no secrets: temp queues/keys, canonical bridge sealing,
dead pids only. Covers the double-click contract: one prompt, verify
then recover only when allowed, plain outcome plus Enter wait, CLI with
args unchanged, outdated worker diagnosed.
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

JOB1 = "ee" * 16


def run_cli(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([os.fspath(EXE), *argv], capture_output=True,
                          text=True, timeout=180)


def last_receipt(completed: subprocess.CompletedProcess[str]) -> dict:
    for line in reversed(completed.stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return {}


def dead_pid() -> int:
    started = subprocess.Popen([sys.executable, "-c", "pass"])
    started.wait(timeout=60)
    assert not proc.is_alive(started.pid)
    return started.pid


@unittest.skipUnless(EXE.exists(), "recovery exe not built")
class SingleExeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.key = bytes(range(32))
        self.key_file = self.root / "key.bin"
        self.key_file.write_bytes(self.key)

    def make_queue(self, job_id: str) -> Path:
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
        return queue

    def install_localappdata(self, queue: Path, worker_dir: Path | None = None) -> Path:
        local = self.root / "localappdata"
        (local / "task-runner").mkdir(parents=True)
        shared = self.root / "shared"
        shared.mkdir(exist_ok=True)
        cfg = {"queueDir": os.fspath(queue),
               "sharedDir": os.fspath(shared),
               "keyFile": os.fspath(self.key_file), "keyFormat": "raw",
               "python": sys.executable,
               "workerDir": os.fspath(worker_dir or WORKER_DIR)}
        (local / "task-runner" / "lenovo-worker.json").write_text(
            json.dumps(cfg), encoding="utf-8")
        return local

    def run_interactive(self, typed: str, local: Path | None = None,
                        tempdir: Path | None = None) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        if local is not None:
            env["LOCALAPPDATA"] = os.fspath(local)
        scope = os.fspath(tempdir or self.root)
        env["TEMP"] = scope
        env["TMP"] = scope
        env["TMPDIR"] = scope
        return subprocess.run([os.fspath(EXE)], input=typed,
                              capture_output=True, text=True,
                              timeout=180, env=env)

    def test_interactive_bad_id_refuses_and_waits(self) -> None:
        completed = self.run_interactive("not-hex\n\n")
        self.assertEqual(completed.returncode, 2, completed.stderr[-500:])
        self.assertEqual(last_receipt(completed).get("reason"),
                         "job-id-invalid")
        self.assertIn("NOT recovered", completed.stderr)
        self.assertIn("Press Enter", completed.stderr)

    def test_interactive_reconciles_only_typed_job(self) -> None:
        other = "ff" * 16
        queue = self.make_queue(JOB1)
        self.make_queue(other)
        # Second job shares the first queue layout root for simplicity:
        # move its envelopes into the same queue.
        for sub in ("jobs", "claims", "starts"):
            src = self.root / ("queue-" + other[:2]) / sub / (other + ".json")
            dst = queue / sub / (other + ".json")
            if src.exists() and not dst.exists():
                dst.write_bytes(src.read_bytes())
        local = self.install_localappdata(queue)
        completed = self.run_interactive(JOB1 + "\n\n", local=local)
        self.assertEqual(completed.returncode, 0,
                         completed.stderr[-500:] + completed.stdout[-500:])
        self.assertEqual(last_receipt(completed).get("outcome"), "reconciled")
        self.assertIn("RECOVERED", completed.stderr)
        self.assertIn("Press Enter", completed.stderr)
        self.assertTrue((queue / "results" / (JOB1 + ".json")).exists())
        self.assertFalse((queue / "results" / (other + ".json")).exists())

    def test_interactive_refusal_stops_before_invoke(self) -> None:
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
        local = self.install_localappdata(queue)
        completed = self.run_interactive(job_id + "\n\n", local=local)
        self.assertEqual(completed.returncode, 2, completed.stderr[-500:])
        self.assertIn("NOT recovered", completed.stderr)
        self.assertFalse((queue / "results" / (job_id + ".json")).exists())

    def test_cli_with_args_unchanged_no_prompt(self) -> None:
        queue = self.make_queue(JOB1)
        shared = self.root / "shared-cli"
        shared.mkdir(exist_ok=True)
        cfg = {"queueDir": os.fspath(queue),
               "sharedDir": os.fspath(shared),
               "keyFile": os.fspath(self.key_file), "keyFormat": "raw",
               "python": sys.executable,
               "workerDir": os.fspath(WORKER_DIR)}
        cfg_file = self.root / "cli-config.json"
        cfg_file.write_text(json.dumps(cfg), encoding="utf-8")
        proof = self.root / "cli.proof.json"
        verify = run_cli("--config", os.fspath(cfg_file), "--job-id", JOB1,
                         "--proof-out", os.fspath(proof), "--verify-only")
        self.assertEqual(verify.returncode, 0, verify.stderr[-500:])
        self.assertEqual(last_receipt(verify).get("outcome"),
                         "ready-to-reconcile")
        self.assertNotIn("Press Enter", verify.stderr + verify.stdout)

    def test_outdated_worker_refused_on_invoke_only(self) -> None:
        queue = self.make_queue(JOB1)
        staged_root = self.root / "staged-repo"
        staged = staged_root / "scripts" / "lenovo_worker"
        staged.mkdir(parents=True)
        (staged / "worker.py").write_text(
            'WORKER_SCHEMA = "lenovo-worker/1"\n--reconcile-id --reconcile-proof\n',
            encoding="utf-8")
        (staged / "job_reconciliation.py").write_text(
            'PROOF_SCHEMA = "lenovo-job-reconciliation/1"\n',
            encoding="utf-8")
        shared = self.root / "shared-old"
        shared.mkdir(exist_ok=True)
        cfg = {"queueDir": os.fspath(queue),
               "sharedDir": os.fspath(shared),
               "keyFile": os.fspath(self.key_file), "keyFormat": "raw",
               "python": sys.executable,
               "workerDir": os.fspath(staged)}
        cfg_file = self.root / "old-config.json"
        cfg_file.write_text(json.dumps(cfg), encoding="utf-8")
        proof = self.root / "old.proof.json"
        verify = run_cli("--config", os.fspath(cfg_file), "--job-id", JOB1,
                         "--proof-out", os.fspath(proof), "--verify-only")
        self.assertEqual(verify.returncode, 0, verify.stderr[-500:])
        invoke = run_cli("--config", os.fspath(cfg_file), "--job-id", JOB1,
                         "--proof-out", os.fspath(proof), "--invoke")
        receipt = last_receipt(invoke)
        self.assertEqual(invoke.returncode, 2, invoke.stderr[-500:])
        self.assertEqual(receipt.get("reason"), "worker-incompatible")
        self.assertFalse((queue / "results" / (JOB1 + ".json")).exists())


if __name__ == "__main__":
    unittest.main()
