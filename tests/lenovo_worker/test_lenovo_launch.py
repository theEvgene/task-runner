"""Fixture-only tests for the Lenovo one-shortcut launch (stdlib, unittest).

No network, no live queue, no real SMB: the share is a local temp dir via
``smb.fixtureLocalDir`` and all secrets are fixture strings. Each case uses
a fresh temp root and kills the stub processes it starts.
"""

from __future__ import annotations

import contextlib
import io
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
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parents[1]
SCRIPTS = ROOT / "scripts" / "lenovo_worker"
BRIDGE = ROOT / "scripts" / "lan_bridge"

sys.path.insert(0, str(BRIDGE))
sys.path.insert(0, str(SCRIPTS))
import lan_bridge  # noqa: E402
import launch as launch_mod  # noqa: E402
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
        # The receipt is a launch status only; it never claims a job result.
        self.assertEqual(summary["scope"], "worker-launch")
        self.assertEqual(summary["jobOutcome"], "not-evaluated")
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

    def test_unc_launcher_cwd_refuses_before_any_spawn(self) -> None:
        root = self.tmp.root
        shared = root / "share"
        shared.mkdir()
        queue = make_queue(shared)
        key = root / "key.bin"
        make_key(key)
        state, trash = root / "state", root / "trash"
        config = base_config(root, queue, shared, key, state, trash)
        cfg = write_config(root / "config.json", **config)
        out = io.StringIO()
        # A launcher started from a share is unverifiable as a host cwd: it
        # refuses before the SMB mapping, the launch lock, or any spawn.
        with mock.patch.object(launch_mod.os, "getcwd",
                               return_value="\\\\HOST\\lan-share\\cwd"), \
                mock.patch.object(launch_mod.proc, "spawn_detached") as spawn, \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            code = launch_mod.main(["--config", str(cfg)])
        self.assertEqual(code, 2)
        spawn.assert_not_called()
        summary = json.loads(out.getvalue())
        self.assertEqual(summary["outcome"], "refused")
        self.assertEqual(summary["reason"], "launch-cwd-remote")
        self.assertEqual(summary["jobOutcome"], "not-evaluated")
        self.assertTrue(summary["nextAction"])
        self.assertNotIn("HOST", json.dumps(summary))
        self.assertFalse((state / "worker.pid").exists())
        self.assertFalse((state / "cleaner.pid").exists())
        self.assertFalse(trash.exists())

    def test_unc_worker_cwd_refuses_without_spawning(self) -> None:
        root = self.tmp.root
        shared = root / "share"
        shared.mkdir()
        queue = make_queue(shared)
        key = root / "key.bin"
        make_key(key)
        state, trash = root / "state", root / "trash"
        config = base_config(root, queue, shared, key, state, trash)
        config["workerCwd"] = "\\\\HOST\\lan-share\\work"
        completed = self._launch(config)
        self.assertEqual(completed.returncode, 2, completed.stderr)
        summary = json.loads(completed.stdout)
        self.assertEqual(summary["reason"], "launch-cwd-remote")
        self.assertIn("worker cwd", summary["detail"])
        self.assertNotIn("HOST", completed.stdout + completed.stderr)
        self.assertFalse((state / "worker.pid").exists())
        self.assertFalse((state / "cleaner.pid").exists())

    def test_missing_worker_cwd_refuses_with_one_receipt(self) -> None:
        """Regression: an absent workerCwd must not escape as an OSError traceback."""
        root = self.tmp.root
        shared = root / "share"
        shared.mkdir()
        queue = make_queue(shared)
        key = root / "key.bin"
        make_key(key)
        state, trash = root / "state", root / "trash"
        config = base_config(root, queue, shared, key, state, trash)
        missing = root / "does-not-exist"
        config["workerCwd"] = os.fspath(missing)
        completed = self._launch(config)
        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertEqual(len(completed.stdout.strip().splitlines()), 1, completed.stdout)
        summary = json.loads(completed.stdout)
        self.assertEqual(summary["outcome"], "refused")
        self.assertEqual(summary["reason"], "launch-cwd-invalid")
        self.assertEqual(summary["scope"], "worker-launch")
        self.assertEqual(summary["jobOutcome"], "not-evaluated")
        self.assertTrue(summary["nextAction"])
        # The classifier is fed by the real observation, not by a guess.
        self.assertIn("workerCwdPresent=False", summary["evidence"])
        self.assertIn("launch refused:", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)
        self.assertNotIn(str(root), completed.stdout + completed.stderr)
        self.assertFalse(missing.exists())
        self.assertFalse(state.exists())  # nothing was created before the refusal
        self.assertFalse(trash.exists())

    def test_relative_worker_cwd_refuses_as_launch_cwd(self) -> None:
        """The documented launch-cwd-relative code is reachable from the config."""
        root = self.tmp.root
        shared = root / "share"
        shared.mkdir()
        queue = make_queue(shared)
        key = root / "key.bin"
        make_key(key)
        state, trash = root / "state", root / "trash"
        config = base_config(root, queue, shared, key, state, trash)
        config["workerCwd"] = os.path.join("relative-dir", "work")
        completed = self._launch(config)
        self.assertEqual(completed.returncode, 2, completed.stderr)
        summary = json.loads(completed.stdout)
        self.assertEqual(summary["reason"], "launch-cwd-relative")
        self.assertIn("worker cwd", summary["detail"])
        self.assertNotIn(str(root), completed.stdout + completed.stderr)
        self.assertFalse((state / "worker.pid").exists())

    def test_worker_cwd_change_under_live_pid_refuses(self) -> None:
        """The effective cwd is part of the recorded role identity."""
        root = self.tmp.root
        shared = root / "share"
        shared.mkdir()
        queue = make_queue(shared)
        key = root / "key.bin"
        make_key(key)
        state, trash = root / "state", root / "trash"
        first_cwd, second_cwd = root / "cwd-a", root / "cwd-b"
        first_cwd.mkdir()
        second_cwd.mkdir()
        stub = self._stub()
        config = base_config(root, queue, shared, key, state, trash)
        config.update({"workerCommand": stub, "cleanerCommand": stub,
                       "workerMarker": "time.sleep(60)", "cleanerMarker": "time.sleep(60)",
                       "workerCwd": os.fspath(first_cwd)})
        first = self._launch(config)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.tmp.track_pids(state)
        pids = [r["pid"] for r in json.loads(first.stdout)["roles"]]
        changed = dict(config)
        changed["workerCwd"] = os.fspath(second_cwd)
        second = self._launch(changed)
        self.assertEqual(second.returncode, 2, second.stderr)
        summary = json.loads(second.stdout)
        self.assertEqual(summary["reason"], "role-ambiguous")
        self.assertIn("ambiguous worker state", second.stderr)
        for pid in pids:
            self.assertTrue(launch_proc.is_alive(pid))  # never killed
        recorded = [int(json.loads((state / f"{role}.pid").read_text())["pid"])
                    for role in ("worker", "cleaner")]
        self.assertEqual(recorded, pids)  # never replaced or duplicated
        self.assertNotIn(str(first_cwd), second.stdout + second.stderr)

    def test_mapped_share_with_wrong_shared_dir_refuses(self) -> None:
        """A wrong sharedDir is a config fact, not a share contradiction."""
        root = self.tmp.root
        shared = root / "share"
        shared.mkdir()
        queue = make_queue(shared)
        key = root / "key.bin"
        make_key(key)
        state, trash = root / "state", root / "trash"
        wrong_shared = root / "not-the-share"
        config = base_config(root, queue, shared, key, state, trash)
        config["sharedDir"] = os.fspath(wrong_shared)  # smb fixture still maps `shared`
        completed = self._launch(config)
        self.assertEqual(completed.returncode, 2, completed.stderr)
        summary = json.loads(completed.stdout)
        self.assertEqual(summary["reason"], "shared-dir-invalid")
        # Real probe token plus real filesystem fact, both kept separate.
        self.assertIn("shareStatus=MAPPED", summary["evidence"])
        self.assertIn("sharePathPresent=False", summary["evidence"])
        self.assertNotIn("its path is absent", summary["detail"])
        self.assertIn("sharedDir", summary["nextAction"])
        self.assertNotIn(str(root), completed.stdout + completed.stderr)
        self.assertFalse((state / "worker.pid").exists())
        self.assertFalse((state / "cleaner.pid").exists())

    def test_foreign_command_line_refuses_without_killing(self) -> None:
        root = self.tmp.root
        shared = root / "share"
        shared.mkdir()
        queue = make_queue(shared)
        key = root / "key.bin"
        make_key(key)
        state, trash = root / "state", root / "trash"
        state.mkdir(parents=True)
        foreign = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def stop() -> None:
            try:
                foreign.terminate()
                foreign.wait(5)
            except OSError:
                pass

        self.addCleanup(stop)
        config = base_config(root, queue, shared, key, state, trash)
        worker_cmd = [sys.executable, str(WORKER), "--queue", str(queue),
                      "--key-file", str(key), "--key-format", "raw",
                      "--shared", str(shared), "--poll", "5"]
        record = {"pid": foreign.pid, "marker": "worker.py", "started": "t",
                  "command": worker_cmd,
                  "binding": {"queue": str(queue), "key_file": str(key)}}
        pid_file = state / "worker.pid"
        original = json.dumps(record)
        pid_file.write_text(original, encoding="utf-8")
        completed = self._launch(config)
        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertIn("ambiguous worker state", completed.stderr)
        summary = json.loads(completed.stdout)
        self.assertEqual(summary["reason"], "role-ambiguous")
        self.assertTrue(summary["nextAction"])
        # Nothing is killed, replaced, or duplicated.
        self.assertIsNone(foreign.poll())
        self.assertEqual(pid_file.read_text(encoding="utf-8"), original)
        self.assertFalse((state / "cleaner.pid").exists())
        self.assertNotIn(str(root), completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
