"""Fixture-only tests for the Lenovo one-shortcut launch (stdlib, unittest).

No network, no live queue, no real SMB: the share is a local temp dir via
``smb.fixtureLocalDir`` and all secrets are fixture strings. Each case uses
a fresh temp root and kills the stub processes it starts.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parents[1]
SCRIPTS = ROOT / "scripts" / "lenovo_worker"
BRIDGE = ROOT / "scripts" / "lan_bridge"

sys.path.insert(0, str(BRIDGE))
sys.path.insert(0, str(SCRIPTS))
import lan_bridge  # noqa: E402
import proc as launch_proc  # noqa: E402

WORKER = SCRIPTS / "worker.py"
CLEANER = SCRIPTS / "cleaner.py"
LAUNCH = SCRIPTS / "launch.py"
SECRET = "fixture-secret-pw-9z"


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run([str(p) for p in command], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=60, check=False, **kwargs)


class TempRoot:
    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="lenovo-launch-"))
        self.pids: list[int] = []

    def cleanup(self) -> None:
        for pid in self.pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        for current, dirs, files in os.walk(self.root):
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


def seal_job(queue: Path, key: bytes, command: str = "echo hi",
             workdir: str | None = None) -> str:
    job_id = os.urandom(16).hex()
    payload = lan_bridge.build_job_payload(
        job_id=job_id, command=command,
        working_directory=workdir or os.fspath(queue.parent))
    lan_bridge.publish(str(queue / "jobs"), job_id, lan_bridge.seal(payload, key))
    return job_id


def write_config(path: Path, **fields) -> Path:
    path.write_text(json.dumps(fields, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def base_config(root: Path, queue: Path, shared: Path, key: Path,
                state: Path, trash: Path, **smb_extra) -> dict:
    pw = root / "pw.txt"
    pw.write_text(SECRET + "\n", encoding="utf-8")
    smb = {"fixtureLocalDir": os.fspath(shared), "passwordFile": os.fspath(pw)}
    smb.update(smb_extra)
    return {"queueDir": os.fspath(queue), "sharedDir": os.fspath(shared),
            "keyFile": os.fspath(key), "keyFormat": "raw",
            "python": sys.executable,
            "trashDir": os.fspath(trash), "stateDir": os.fspath(state),
            "smb": smb}


class WorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TempRoot()
        self.addCleanup(self.tmp.cleanup)

    def test_once_seals_result_and_skips_sealed(self) -> None:
        root = self.tmp.root
        shared = root / "share"
        shared.mkdir()
        queue = make_queue(shared)
        key_path = root / "key.bin"
        key = make_key(key_path)
        first = seal_job(queue, key)
        lan_bridge.publish(
            str(queue / "results"), first,
            lan_bridge.seal(lan_bridge.build_result_payload(first, "ok", 0), key))
        sealed_bytes = (queue / "results" / f"{first}.json").read_bytes()
        second = seal_job(queue, key)
        completed = run([sys.executable, WORKER, "--queue", queue,
                         "--key-file", key_path, "--shared", shared, "--once"])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        # Foreign (pre-sealed) result byte-identical: never touched.
        self.assertEqual((queue / "results" / f"{first}.json").read_bytes(), sealed_bytes)
        payload = lan_bridge.open_envelope(
            (queue / "results" / f"{second}.json").read_bytes(), key, "result", second)
        self.assertEqual(payload["status"], "ok")

    def test_missing_queue_refuses_without_creating(self) -> None:
        root = self.tmp.root
        key_path = root / "key.bin"
        make_key(key_path)
        missing = root / "no-such-queue"
        completed = run([sys.executable, WORKER, "--queue", missing,
                         "--key-file", key_path, "--once"])
        self.assertEqual(completed.returncode, 2)
        self.assertIn("queue unavailable", completed.stderr)
        self.assertFalse(missing.exists())

    def test_key_inside_queue_or_share_refuses(self) -> None:
        root = self.tmp.root
        queue = make_queue(root)
        for bad in (queue / "key.bin", root / "share-key.bin"):
            bad.write_bytes(os.urandom(32))
            shared = root  # contains the second bad key path
            completed = run([sys.executable, WORKER, "--queue", queue,
                             "--key-file", bad, "--shared", shared, "--once"])
            self.assertEqual(completed.returncode, 2, bad)
            self.assertIn("key", completed.stderr, bad)


class CleanerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TempRoot()
        self.addCleanup(self.tmp.cleanup)

    def test_moves_only_tmp_top_level(self) -> None:
        root = self.tmp.root
        shared = root / "share"
        queue = make_queue(shared)
        trash = root / "trash"
        (shared / "stale.tmp").write_text("x", encoding="utf-8")
        old = os.path.getmtime(shared / "stale.tmp") - 7200
        os.utime(shared / "stale.tmp", (old, old))
        (shared / "fresh.tmp").write_text("y", encoding="utf-8")
        (shared / "request.json").write_text("{}", encoding="utf-8")
        (queue / "jobs" / "j.tmp").write_text("q", encoding="utf-8")
        completed = run([sys.executable, CLEANER, "--shared", shared,
                         "--trash-dir", trash, "--max-age", "60", "--once"])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse((shared / "stale.tmp").exists())
        self.assertEqual(len(list(trash.glob("stale.tmp.*"))), 1)
        self.assertTrue((shared / "fresh.tmp").exists())
        self.assertTrue((shared / "request.json").exists())
        self.assertTrue((queue / "jobs" / "j.tmp").exists())


class LaunchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TempRoot()
        self.addCleanup(self.tmp.cleanup)

    def _stub(self) -> list[str]:
        return [sys.executable, "-c", "import time; time.sleep(60)"]

    def _launch(self, config: dict) -> subprocess.CompletedProcess:
        cfg = self.tmp.root / "config.json"
        write_config(cfg, **config)
        return run([sys.executable, LAUNCH, "--config", cfg])

    def test_success_and_repeat_is_idempotent(self) -> None:
        root = self.tmp.root
        shared = root / "share"
        shared.mkdir()
        queue = make_queue(shared)
        key = root / "key.bin"
        make_key(key)
        state, trash = root / "state", root / "trash"
        stub = self._stub()
        config = base_config(root, queue, shared, key, state, trash)
        config.update({"workerCommand": stub, "cleanerCommand": stub,
                       "workerMarker": "time.sleep(60)", "cleanerMarker": "time.sleep(60)"})
        first = self._launch(config)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.tmp.track_pids(state)
        summary = json.loads(first.stdout)
        self.assertEqual(summary["outcome"], "ready")
        pids = [r["pid"] for r in summary["roles"]]
        second = self._launch(config)
        self.assertEqual(second.returncode, 0, second.stderr)
        again = [r["pid"] for r in json.loads(second.stdout)["roles"]]
        self.assertEqual(pids, again)  # no duplicates
        for pid in pids:
            self.assertTrue(launch_proc.is_alive(pid))

    def test_stale_pid_recovers_after_reboot(self) -> None:
        root = self.tmp.root
        shared = root / "share"
        shared.mkdir()
        queue = make_queue(shared)
        key = root / "key.bin"
        make_key(key)
        state, trash = root / "state", root / "trash"
        state.mkdir(parents=True)
        (state / "worker.pid").write_text(
            json.dumps({"pid": 2147483000, "marker": "worker.py", "started": "t"}),
            encoding="utf-8")
        stub = self._stub()
        config = base_config(root, queue, shared, key, state, trash)
        config.update({"workerCommand": stub, "cleanerCommand": stub,
                       "workerMarker": "time.sleep(60)", "cleanerMarker": "time.sleep(60)"})
        completed = self._launch(config)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.tmp.track_pids(state)
        summary = json.loads(completed.stdout)
        self.assertEqual(summary["outcome"], "ready")
        new_pid = int(json.loads((state / "worker.pid").read_text())["pid"])
        self.assertNotEqual(new_pid, 2147483000)
        self.assertTrue(launch_proc.is_alive(new_pid))

    def test_unavailable_share_refuses(self) -> None:
        root = self.tmp.root
        shared = root / "no-such-share"
        queue = root / "no-such-queue"
        key = root / "key.bin"
        make_key(key)
        state, trash = root / "state", root / "trash"
        config = base_config(root, queue, shared, key, state, trash)
        config["smb"] = {"fixtureLocalDir": os.fspath(shared),
                         "passwordFile": os.fspath(root / "pw.txt")}
        completed = self._launch(config)
        self.assertEqual(completed.returncode, 2)
        self.assertIn("share unavailable", completed.stderr)
        self.assertFalse(queue.exists())  # no new empty queue
        self.assertFalse((state / "worker.pid").exists())

    def test_bad_credentials_refuse(self) -> None:
        root = self.tmp.root
        shared = root / "share"
        shared.mkdir()
        queue = make_queue(shared)
        key = root / "key.bin"
        make_key(key)
        state, trash = root / "state", root / "trash"
        config = base_config(root, queue, shared, key, state, trash,
                             fixtureRequirePassword="expected-secret")
        completed = self._launch(config)
        self.assertEqual(completed.returncode, 2)
        self.assertIn("bad credentials", completed.stderr)
        self.assertFalse((state / "worker.pid").exists())

    def test_missing_queue_never_created(self) -> None:
        root = self.tmp.root
        shared = root / "share"
        shared.mkdir()
        queue = shared / "queue"  # absent on purpose
        key = root / "key.bin"
        make_key(key)
        state, trash = root / "state", root / "trash"
        config = base_config(root, queue, shared, key, state, trash)
        completed = self._launch(config)
        self.assertEqual(completed.returncode, 2)
        self.assertIn("queue unavailable", completed.stderr)
        self.assertFalse(queue.exists())

    def test_secret_never_leaks(self) -> None:
        root = self.tmp.root
        shared = root / "share"
        shared.mkdir()
        queue = make_queue(shared)
        key = root / "key.bin"
        make_key(key)
        state, trash = root / "state", root / "trash"
        stub = self._stub()
        config = base_config(root, queue, shared, key, state, trash)
        config.update({"workerCommand": stub, "cleanerCommand": stub,
                       "workerMarker": "time.sleep(60)", "cleanerMarker": "time.sleep(60)"})
        completed = self._launch(config)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.tmp.track_pids(state)
        blob = completed.stdout + completed.stderr
        for log in state.glob("*.log"):
            blob += log.read_text(encoding="utf-8", errors="replace")
        self.assertNotIn(SECRET, blob)
        for pid in self.tmp.pids:
            self.assertNotIn(SECRET, launch_proc.cmdline(pid) or "")


if __name__ == "__main__":
    unittest.main()
