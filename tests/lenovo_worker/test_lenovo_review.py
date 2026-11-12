"""Review-correction regression tests for the Lenovo shortcut (stdlib, unittest).

Fast, fixture-only, isolated: no network, no live queue, no real SMB, no
reboot. Each case proves one corrected failure mode and nothing else.
"""

from __future__ import annotations

import contextlib
import io
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
CLEANER = SCRIPTS / "cleaner.py"
LAUNCH = SCRIPTS / "launch.py"
SECRET = "fixture-secret-pw-9z"
PY = sys.executable


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run([str(p) for p in command], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=60, check=False, **kwargs)


class TempRoot:
    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="lenovo-review-"))
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
                state: Path, trash: Path, **smb_extra) -> dict:
    pw = root / "pw.txt"
    pw.write_bytes((SECRET + "\r\n").encode("utf-8"))  # CRLF on purpose
    smb = {"fixtureLocalDir": os.fspath(shared), "passwordFile": os.fspath(pw)}
    smb.update(smb_extra)
    return {"queueDir": os.fspath(queue), "sharedDir": os.fspath(shared),
            "keyFile": os.fspath(key), "keyFormat": "raw",
            "python": PY, "trashDir": os.fspath(trash),
            "stateDir": os.fspath(state), "smb": smb}


def launch_with(config: dict, root: Path, **env_extra) -> subprocess.CompletedProcess:
    cfg = root / "config.json"
    cfg.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    env = dict(os.environ)
    env.update(env_extra)
    return run([PY, LAUNCH, "--config", cfg], env=env)


class PasswordNewlineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TempRoot()
        self.addCleanup(self.tmp.cleanup)

    def test_crlf_password_matches_and_never_leaks(self) -> None:
        root = self.tmp.root
        shared = root / "share"
        shared.mkdir()
        queue = make_queue(shared)
        key = root / "key.bin"
        make_key(key)
        state, trash = root / "state", root / "trash"
        stub = [PY, "-c", "import time; time.sleep(60)"]
        config = base_config(root, queue, shared, key, state, trash,
                             fixtureRequirePassword=SECRET)
        config.update({"workerCommand": stub, "cleanerCommand": stub,
                       "workerMarker": "time.sleep(60)",
                       "cleanerMarker": "time.sleep(60)"})
        completed = launch_with(config, root)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.tmp.track_pids(state)
        blob = completed.stdout + completed.stderr
        self.assertNotIn(SECRET, blob)


class AmbiguousStartsNothingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TempRoot()
        self.addCleanup(self.tmp.cleanup)

    def test_stale_cleaner_pid_starts_no_worker(self) -> None:
        root = self.tmp.root
        shared = root / "share"
        shared.mkdir()
        queue = make_queue(shared)
        key = root / "key.bin"
        make_key(key)
        state, trash = root / "state", root / "trash"
        state.mkdir(parents=True)
        (state / "cleaner.pid").write_text(
            json.dumps({"pid": 2147483000, "marker": "cleaner.py",
                        "started": "t"}),
            encoding="utf-8")
        config = base_config(root, queue, shared, key, state, trash)
        completed = launch_with(config, root)
        self.assertEqual(completed.returncode, 2)
        self.assertIn("ambiguous cleaner state", completed.stderr)
        self.assertFalse((state / "worker.pid").exists())  # nothing spawned

    def test_changed_command_refuses_without_duplicate(self) -> None:
        root = self.tmp.root
        shared = root / "share"
        shared.mkdir()
        queue = make_queue(shared)
        key = root / "key.bin"
        make_key(key)
        state, trash = root / "state", root / "trash"
        stub = [PY, "-c", "import time; time.sleep(60)"]
        config = base_config(root, queue, shared, key, state, trash)
        config.update({"workerCommand": stub, "cleanerCommand": stub,
                       "workerMarker": "time.sleep(60)",
                       "cleanerMarker": "time.sleep(60)"})
        first = launch_with(config, root)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.tmp.track_pids(state)
        old_pid = int(json.loads((state / "worker.pid").read_text())["pid"])
        sentinel = root / "spawned-b.txt"
        changed = dict(config)
        changed["workerCommand"] = [
            PY, "-c",
            f"import time, pathlib; pathlib.Path({str(sentinel)!r}).touch(); time.sleep(60)",
        ]
        changed["workerMarker"] = "spawned-b-marker"
        second = launch_with(changed, root)
        self.assertEqual(second.returncode, 2, second.stderr)
        self.assertIn("ambiguous worker state", second.stderr)
        self.assertFalse(sentinel.exists())  # changed command never spawned
        self.assertEqual(
            int(json.loads((state / "worker.pid").read_text())["pid"]), old_pid)
        self.assertTrue(launch_proc.is_alive(old_pid))


class LockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TempRoot()
        self.addCleanup(self.tmp.cleanup)

    def test_held_lock_refuses_without_spawning(self) -> None:
        root = self.tmp.root
        shared = root / "share"
        shared.mkdir()
        queue = make_queue(shared)
        key = root / "key.bin"
        make_key(key)
        state, trash = root / "state", root / "trash"
        state.mkdir(parents=True)
        lock = state / ".launch.lock"
        lock.mkdir()
        (lock / "owner.json").write_text(
            json.dumps({"pid": os.getpid(), "started": "t"}), encoding="utf-8")
        config = base_config(root, queue, shared, key, state, trash)
        completed = launch_with(
            config, root, TASK_RUNNER_LAUNCH_LOCK_WAIT_S="0")
        self.assertEqual(completed.returncode, 2)
        self.assertIn("another launch is in progress", completed.stderr)
        self.assertFalse((state / "worker.pid").exists())
        self.assertFalse((state / "cleaner.pid").exists())


class LayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TempRoot()
        self.addCleanup(self.tmp.cleanup)

    def test_trash_inside_queue_refused_without_creation(self) -> None:
        root = self.tmp.root
        shared = root / "share"
        shared.mkdir()
        queue = make_queue(shared)
        key = root / "key.bin"
        make_key(key)
        state = root / "state"
        trash = queue / "trash-new"
        config = base_config(root, queue, shared, key, state, trash)
        completed = launch_with(config, root)
        self.assertEqual(completed.returncode, 2)
        self.assertIn("must be outside", completed.stderr)
        self.assertFalse(trash.exists())

    def test_state_inside_shared_refused(self) -> None:
        root = self.tmp.root
        shared = root / "share"
        shared.mkdir()
        queue = make_queue(shared)
        key = root / "key.bin"
        make_key(key)
        config = base_config(root, queue, shared, key,
                             shared / "state-new", root / "trash")
        completed = launch_with(config, root)
        self.assertEqual(completed.returncode, 2)
        self.assertIn("must be outside", completed.stderr)


class CleanerScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TempRoot()
        self.addCleanup(self.tmp.cleanup)

    def test_custom_queue_dirname_excluded(self) -> None:
        root = self.tmp.root
        shared = root / "share"
        custom = shared / "q2"
        (custom / "jobs").mkdir(parents=True)
        (custom / "results").mkdir(parents=True)
        trash = root / "trash"
        stale_top = shared / "stale.tmp"
        stale_top.write_text("x", encoding="utf-8")
        queued = custom / "jobs" / "old.tmp"
        queued.write_text("q", encoding="utf-8")
        old = os.path.getmtime(stale_top) - 7200
        os.utime(stale_top, (old, old))
        os.utime(queued, (old, old))
        completed = run([PY, CLEANER, "--shared", shared,
                         "--queue-dir", custom, "--trash-dir", trash,
                         "--max-age", "60", "--once"])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(stale_top.exists())
        self.assertTrue(queued.exists())  # configured queue untouched

    def test_trash_inside_shared_refused(self) -> None:
        root = self.tmp.root
        shared = root / "share"
        shared.mkdir()
        trash = shared / "trash-new"
        completed = run([PY, CLEANER, "--shared", shared,
                         "--trash-dir", trash, "--once"])
        self.assertEqual(completed.returncode, 2)
        self.assertIn("must be outside", completed.stderr)
        self.assertFalse(trash.exists())


class WorkerContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TempRoot()
        self.addCleanup(self.tmp.cleanup)

    def test_return_file_bounded_and_flagged(self) -> None:
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
            return_files=[os.fspath(big)])
        lan_bridge.publish(str(queue / "jobs"), job_id, lan_bridge.seal(payload, key))
        completed = run([PY, WORKER, "--queue", queue, "--key-file", key_path,
                         "--shared", shared, "--once"])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = lan_bridge.open_envelope(
            (queue / "results" / f"{job_id}.json").read_bytes(),
            key, "result", job_id)
        self.assertTrue(result["output_truncated"])
        import base64

        raw = base64.b64decode(result["files"][os.fspath(big)])
        self.assertLessEqual(len(raw), 262144)
        self.assertEqual(raw, big.read_bytes()[:262144])

    def test_seal_failure_reports_failed_and_batch_exits_2(self) -> None:
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
        with mock.patch.object(worker_mod.bridge, "publish",
                               side_effect=OSError("disk gone")):
            counts = worker_mod.run_once(queue, key)
            self.assertEqual(counts["failed"], 1)
            self.assertEqual(counts["sealed"], 0)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = worker_mod.main(["--queue", os.fspath(queue),
                                        "--key-file", os.fspath(key_path),
                                        "--once"])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(buf.getvalue())["failed"], 1)


@unittest.skipUnless(os.name == "nt", "batch-file propagation needs cmd.exe")
class BatPropagationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TempRoot()
        self.addCleanup(self.tmp.cleanup)

    def test_bat_propagates_refusal_exit(self) -> None:
        root = self.tmp.root
        shared = root / "share"
        shared.mkdir()
        queue = root / "no-such-queue"
        key = root / "key.bin"
        make_key(key)
        config = base_config(root, queue, shared, key, root / "state", root / "trash")
        cfg = root / "config.json"
        cfg.write_text(json.dumps(config), encoding="utf-8")
        import shutil as _shutil

        env = dict(os.environ)
        env["PATH"] = os.path.dirname(PY) + os.pathsep + env.get("PATH", "")
        bat = SCRIPTS / "Launch-Worker.bat"
        completed = subprocess.run(
            ["cmd", "/c", str(bat), str(cfg)], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60, check=False, env=env,
            executable=_shutil.which("cmd"))
        self.assertEqual(completed.returncode, 2, completed.stderr)


if __name__ == "__main__":
    unittest.main()
