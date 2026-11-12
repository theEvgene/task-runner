"""Focused regression checks for the 459084f correction (#23/#24).

Fast, fixture-only: no network, no live queue, no reboot.
- Reboot: dead pid records (legacy + bound) recover without duplicates;
  a live foreign pid still refuses with nothing started.
- Invalid cwd/execution: a bounded sealed terminal error, never a crash
  and never a silent re-execution.
- Envelope: outputs + encoded return files stay under the 2 MiB signed
  envelope; a size refusal seals a minimal fallback instead of re-executing.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parents[1]
SCRIPTS = ROOT / "scripts" / "lenovo_worker"
BRIDGE = ROOT / "scripts" / "lan_bridge"

sys.path.insert(0, str(BRIDGE))
sys.path.insert(0, str(SCRIPTS))
import lan_bridge  # noqa: E402
import proc as launch_proc  # noqa: E402

WORKER = SCRIPTS / "worker.py"
LAUNCH = SCRIPTS / "launch.py"
PY = sys.executable


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run([str(p) for p in command], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=60, check=False, **kwargs)


class TempRoot:
    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="lenovo-fix-"))
        self.pids: list[int] = []

    def cleanup(self) -> None:
        import signal

        for pid in self.pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        for current, _dirs, files in os.walk(self.root):
            for name in files:
                try:
                    os.chmod(os.path.join(current, name), stat.S_IWRITE)
                except OSError:
                    pass
        shutil.rmtree(self.root, ignore_errors=True)

    def track_pids(self, state_dir: Path) -> None:
        for role in ("worker", "cleaner"):
            pid_file = state_dir / f"{role}.pid"
            if pid_file.is_file():
                try:
                    self.pids.append(int(json.loads(pid_file.read_text())["pid"]))
                except (ValueError, KeyError, OSError):
                    pass


def make_key(path: Path) -> bytes:
    key = os.urandom(32)
    path.write_bytes(key)
    return key


def make_queue(root: Path) -> Path:
    queue = root / "queue"
    (queue / "jobs").mkdir(parents=True)
    (queue / "results").mkdir(parents=True)
    return queue


def base_config(root: Path, queue: Path, shared: Path, key: Path,
                state: Path, trash: Path) -> dict:
    pw = root / "pw.txt"
    pw.write_bytes(b"fixture-pw-1\n")
    smb = {"fixtureLocalDir": os.fspath(shared),
           "passwordFile": os.fspath(pw)}
    return {"queueDir": os.fspath(queue), "sharedDir": os.fspath(shared),
            "keyFile": os.fspath(key), "keyFormat": "raw",
            "python": PY, "trashDir": os.fspath(trash),
            "stateDir": os.fspath(state), "smb": smb}


def launch_with(config: dict, root: Path) -> subprocess.CompletedProcess:
    cfg = root / "config.json"
    cfg.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return run([PY, LAUNCH, "--config", cfg])


class RebootRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TempRoot()
        self.addCleanup(self.tmp.cleanup)

    def test_dead_bound_records_recover_without_duplicate(self) -> None:
        root = self.tmp.root
        shared = root / "share"
        shared.mkdir()
        queue = make_queue(shared)
        key = root / "key.bin"
        make_key(key)
        state, trash = root / "state", root / "trash"
        state.mkdir(parents=True)
        stub = [PY, "-c", "import time; time.sleep(60)"]
        worker_cmd = stub
        cleaner_cmd = stub
        worker_binding = {"queue": os.fspath(queue), "key_file": os.fspath(key)}
        cleaner_binding = {"shared": os.fspath(shared), "trash_dir": os.fspath(trash)}
        for role, cmd, marker, binding in (
                ("worker", worker_cmd, "time.sleep(60)", worker_binding),
                ("cleaner", cleaner_cmd, "time.sleep(60)", cleaner_binding)):
            (state / f"{role}.pid").write_text(json.dumps(
                {"pid": 2147483000, "marker": marker, "started": "t",
                 "command": [str(p) for p in cmd], "binding": binding}),
                encoding="utf-8")
        config = base_config(root, queue, shared, key, state, trash)
        config.update({"workerCommand": worker_cmd, "cleanerCommand": cleaner_cmd,
                       "workerMarker": "time.sleep(60)",
                       "cleanerMarker": "time.sleep(60)"})
        completed = launch_with(config, root)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.tmp.track_pids(state)
        summary = json.loads(completed.stdout)
        self.assertEqual(summary["outcome"], "ready")
        for role in ("worker", "cleaner"):
            pid = int(json.loads((state / f"{role}.pid").read_text())["pid"])
            self.assertNotEqual(pid, 2147483000)
            self.assertTrue(launch_proc.is_alive(pid))
        # Second launch adopts the same live pids: no duplicates.
        again = launch_with(config, root)
        self.assertEqual(again.returncode, 0, again.stderr)
        roles = {r["role"]: r for r in json.loads(again.stdout)["roles"]}
        self.assertEqual(roles["worker"]["state"], "already-running")
        self.assertEqual(roles["cleaner"]["state"], "already-running")

    def test_live_foreign_pid_still_refuses(self) -> None:
        root = self.tmp.root
        shared = root / "share"
        shared.mkdir()
        queue = make_queue(shared)
        key = root / "key.bin"
        make_key(key)
        state, trash = root / "state", root / "trash"
        state.mkdir(parents=True)
        stub = [PY, "-c", "import time; time.sleep(60)"]
        config = base_config(root, queue, shared, key, state, trash)
        config.update({"workerCommand": stub, "cleanerCommand": stub,
                       "workerMarker": "time.sleep(60)",
                       "cleanerMarker": "time.sleep(60)"})
        first = launch_with(config, root)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.tmp.track_pids(state)
        live = int(json.loads((state / "worker.pid").read_text())["pid"])
        self.assertTrue(launch_proc.is_alive(live))
        changed = dict(config)
        changed["workerCommand"] = [PY, "-c", "import time; time.sleep(61)"]
        second = launch_with(changed, root)
        self.assertEqual(second.returncode, 2, second.stderr)
        self.assertIn("ambiguous worker state", second.stderr)
        self.assertEqual(
            int(json.loads((state / "worker.pid").read_text())["pid"]), live)


class InvalidCwdTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TempRoot()
        self.addCleanup(self.tmp.cleanup)

    def test_bad_cwd_seals_terminal_without_reexecution(self) -> None:
        root = self.tmp.root
        shared = root / "share"
        shared.mkdir()
        queue = make_queue(shared)
        key_path = root / "key.bin"
        key = make_key(key_path)
        job_id = os.urandom(16).hex()
        payload = lan_bridge.build_job_payload(
            job_id=job_id, command="echo hi",
            working_directory=os.fspath(root / "no-such-dir"))
        lan_bridge.publish(str(queue / "jobs"), job_id, lan_bridge.seal(payload, key))
        first = run([PY, WORKER, "--queue", queue, "--key-file", key_path,
                     "--shared", shared, "--once"])
        self.assertEqual(first.returncode, 0, first.stderr)
        raw = (queue / "results" / f"{job_id}.json").read_bytes()
        self.assertLessEqual(len(raw), 2 * 1024 * 1024)
        result = lan_bridge.open_envelope(raw, key, "result", job_id)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["exit_code"], 2)
        self.assertTrue(result["stderr"])
        self.assertLessEqual(len(result["stderr"]), 262144)
        # Sealed once: the next scan seals nothing new (no re-execution).
        sys.path.insert(0, str(SCRIPTS))
        import worker as worker_mod

        counts = worker_mod.run_once(queue, key)
        self.assertEqual(counts, {"sealed": 0, "skipped": 0, "refused": 0, "failed": 0})


class EnvelopeBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TempRoot()
        self.addCleanup(self.tmp.cleanup)

    def test_outputs_plus_files_fit_envelope(self) -> None:
        root = self.tmp.root
        shared = root / "share"
        shared.mkdir()
        queue = make_queue(shared)
        key_path = root / "key.bin"
        key = make_key(key_path)
        big = root / "big.bin"
        big.write_bytes(os.urandom(700 * 1024))
        job_id = os.urandom(16).hex()
        payload = lan_bridge.build_job_payload(
            job_id=job_id, command=f'"{PY}" -c "print(1)"',
            working_directory=os.fspath(root),
            return_files=[os.fspath(big)],
            max_output_bytes=262144)
        lan_bridge.publish(str(queue / "jobs"), job_id, lan_bridge.seal(payload, key))
        completed = run([PY, WORKER, "--queue", queue, "--key-file", key_path,
                         "--shared", shared, "--once"])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        raw = (queue / "results" / f"{job_id}.json").read_bytes()
        self.assertLessEqual(len(raw), 2 * 1024 * 1024)
        result = lan_bridge.open_envelope(raw, key, "result", job_id)
        self.assertTrue(result["output_truncated"])
        self.assertLessEqual(len(raw), 2 * 1024 * 1024 - 1024)

    def test_size_refusal_seals_fallback_without_reexecution(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        import worker as worker_mod

        root = self.tmp.root
        queue = make_queue(root)
        key_path = root / "key.bin"
        key = make_key(key_path)
        job_id = os.urandom(16).hex()
        payload = lan_bridge.build_job_payload(
            job_id=job_id, command="echo hi",
            working_directory=os.fspath(root))
        lan_bridge.publish(str(queue / "jobs"), job_id, lan_bridge.seal(payload, key))
        with mock.patch.object(worker_mod.bridge, "seal",
                               side_effect=[lan_bridge.ProtocolError("envelope too large"),
                                            lan_bridge.seal(
                                                {"kind": "result", "id": job_id,
                                                 "status": "error", "exit_code": 2},
                                                key)]):
            outcome = worker_mod._seal_result(queue, key, job_id,
                                              status="ok", exit_code=0,
                                              stdout="x" * 100, stderr="")
            self.assertEqual(outcome, "sealed")
            raw = (queue / "results" / f"{job_id}.json").read_bytes()
            result = lan_bridge.open_envelope(raw, key, "result", job_id)
            self.assertEqual(result["id"], job_id)


if __name__ == "__main__":
    unittest.main()
