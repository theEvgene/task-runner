"""Continuous same-ID get-result: late results, restart recovery, blockers.

Local fixture only: temp dirs plus an ephemeral key, no live second host.
Complements test_lan_bridge_recovery.py; covers the remaining #16 gap: one
agent-facing get-result that observes the SAME id across short windows and
interruptions until a confirmed terminal result or a concrete blocker --
never a resubmission, never an unsupported "running" claim.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
BRIDGE_SCRIPT = HERE.parents[1] / "scripts" / "lan_bridge" / "lan_bridge.py"
GET_SCRIPT = HERE.parents[1] / "scripts" / "lan_bridge" / "get.py"
if str(HERE.parents[1] / "scripts" / "lan_bridge") not in sys.path:
    sys.path.insert(0, str(HERE.parents[1] / "scripts" / "lan_bridge"))

import lan_bridge as bridge  # noqa: E402
import wait as waitmod  # noqa: E402

_PY_CANDIDATE = r"C:\Users\rb199787\AppData\Local\Programs\Python\Python313\python.exe"


def make_key(directory: Path) -> tuple:
    raw = os.urandom(32)
    key_file = directory / "key.bin"
    key_file.write_bytes(raw)
    return raw, key_file


def run_cli(script: Path, *argv):
    exe = _PY_CANDIDATE if Path(_PY_CANDIDATE).exists() else sys.executable
    return subprocess.run([exe, str(script), *argv],
                          capture_output=True, text=True)


def seal_completed(key: bytes, queue: str, job_id: str, **marks) -> None:
    result = bridge.build_result_payload(job_id, "completed", exit_code=0,
                                         stdout="late", **marks)
    bridge.publish(os.path.join(queue, "results"), job_id,
                   bridge.seal(result, key))


class ContinuousSameIdTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.queue = str(self.root / "queue")
        self.key, _ = make_key(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_late_result_found_across_windows_no_resubmit(self):
        job_id = bridge.submit_job(self.queue, self.key, "cmd", "C:\\s")
        sealed = []

        def on_window(event):
            if event["window"] >= 2 and not sealed:
                seal_completed(self.key, self.queue, job_id)
                sealed.append(event["window"])

        payload, report = waitmod.get_result(
            self.queue, self.key, job_id, window_timeout=0.4,
            poll_seconds=0.05, max_windows=5, on_window=on_window)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["id"], job_id)
        self.assertEqual(report["outcome"], "found")
        self.assertTrue(report["confirmed"])
        self.assertGreaterEqual(report["windows_used"], 2)
        self.assertLessEqual(report["probes"], report["polls_bound"])
        blob = json.dumps(report).lower()
        self.assertNotIn("running", blob)
        self.assertNotIn("running", json.dumps(payload).lower())
        # Exactly one submission exists: the queue refuses any duplicate.
        self.assertEqual(len(list((Path(self.queue) / "jobs").glob("*.json"))),
                         1)
        with self.assertRaises(bridge.JobExistsError):
            bridge.publish(os.path.join(self.queue, "jobs"), job_id, b"again")

    def test_bounded_expiry_unknown_then_restart_recovers(self):
        job_id = bridge.submit_job(self.queue, self.key, "cmd", "C:\\s")
        payload, report = waitmod.get_result(
            self.queue, self.key, job_id, window_timeout=0.3,
            poll_seconds=0.05, max_windows=1)
        self.assertIsNone(payload)
        self.assertEqual(report["outcome"], "unknown")
        self.assertFalse(report["confirmed"])
        self.assertIn(job_id, report["action"])
        blob = json.dumps(report).lower()
        self.assertIn("unknown", blob)
        self.assertIn("not evidence the job is running", blob)
        self.assertNotIn("still running", blob)
        # Late result, then a fresh call (observer restart) reads SAME id.
        seal_completed(self.key, self.queue, job_id)
        payload2, report2 = waitmod.get_result(
            self.queue, self.key, job_id, window_timeout=2,
            poll_seconds=0.05, max_windows=3)
        self.assertIsNotNone(payload2)
        self.assertEqual(payload2["id"], job_id)
        self.assertEqual(report2["outcome"], "found")

    def test_unbounded_with_immediate_result_returns_at_once(self):
        job_id = bridge.submit_job(self.queue, self.key, "cmd", "C:\\s")
        seal_completed(self.key, self.queue, job_id)
        started = time.monotonic()
        payload, report = waitmod.get_result(
            self.queue, self.key, job_id, window_timeout=30,
            poll_seconds=5, max_windows=0)
        elapsed = time.monotonic() - started
        self.assertIsNotNone(payload)
        self.assertEqual(report["windows_used"], 1)
        self.assertEqual(report["probes"], 1)
        self.assertLess(elapsed, 1.0)


class BlockerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.queue = str(self.root / "queue")
        self.key, _ = make_key(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_tampered_result_raises_at_once(self):
        job_id = bridge.submit_job(self.queue, self.key, "cmd", "C:\\s")
        seal_completed(self.key, self.queue, job_id)
        raw_path = Path(self.queue) / "results" / f"{job_id}.json"
        raw = bytearray(raw_path.read_bytes())
        raw[40] ^= 0x01
        raw_path.write_bytes(bytes(raw))
        started = time.monotonic()
        with self.assertRaises(bridge.ProtocolError):
            waitmod.get_result(self.queue, self.key, job_id,
                               window_timeout=5, poll_seconds=0.1,
                               max_windows=0)
        # Concrete blocker: no window is ever waited out.
        self.assertLess(time.monotonic() - started, 2.0)

    def test_missing_queue_fails_fast_even_unbounded(self):
        started = time.monotonic()
        with self.assertRaises(bridge.BridgeError) as ctx:
            waitmod.get_result(str(self.root / "no-queue"), self.key,
                               "c" * 32, window_timeout=60,
                               poll_seconds=0.1, max_windows=0)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertIn("queue unavailable", str(ctx.exception).lower())
        self.assertNotIn("running", str(ctx.exception).lower())

    def test_bad_id_and_bad_windows_refused(self):
        with self.assertRaises(bridge.FileQueueError):
            waitmod.get_result(self.queue, self.key, "not-an-id")
        with self.assertRaises(bridge.BridgeError):
            waitmod.get_result(self.queue, self.key, "c" * 32,
                               max_windows=-1)
        with self.assertRaises(bridge.BridgeError):
            waitmod.get_result(self.queue, self.key, "c" * 32,
                               window_timeout=0)

    def test_verified_unknown_status_returned_not_success(self):
        job_id = bridge.submit_job(self.queue, self.key, "cmd", "C:\\s")
        result = bridge.build_result_payload(job_id, "weird-status",
                                             exit_code=0)
        bridge.publish(os.path.join(self.queue, "results"), job_id,
                       bridge.seal(result, self.key))
        payload, report = waitmod.get_result(
            self.queue, self.key, job_id, window_timeout=2,
            poll_seconds=0.05, max_windows=2)
        self.assertEqual(report["outcome"], "found")
        self.assertTrue(report["confirmed"])
        self.assertEqual(report["terminal_outcome"], "unknown_status")
        self.assertEqual(report["summary"]["outcome"], "unknown_status")
        self.assertNotEqual(report["summary"]["outcome"], "success")


class BeforeAfterTests(unittest.TestCase):
    """Measured saving: one queue resolve in get_result vs one per window."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.queue = str(self.root / "queue")
        self.key, _ = make_key(self.root)

    def tearDown(self):
        self.tmp.cleanup()
        bridge.read = self._real_read
        bridge._require_queue = self._real_require

    def _counting(self):
        self._real_read = bridge.read
        self._real_require = bridge._require_queue
        counts = {"reads": 0, "resolves": 0}
        real_read, real_require = self._real_read, self._real_require

        def read(*args, **kwargs):
            counts["reads"] += 1
            return real_read(*args, **kwargs)

        def require(*args, **kwargs):
            counts["resolves"] += 1
            return real_require(*args, **kwargs)

        bridge.read = read
        bridge._require_queue = require
        return counts

    def test_single_resolve_vs_per_window_resolve(self):
        counts = self._counting()
        # BEFORE: the agent re-invokes one bounded fetch per short window.
        before_id = bridge.submit_job(self.queue, self.key, "cmd", "C:\\s")
        with self.assertRaises(bridge.FetchTimeout):
            bridge.fetch_result(self.queue, self.key, before_id,
                                timeout_seconds=0.4, poll_seconds=0.05)
        resolves_before_first = counts["resolves"]
        seal_completed(self.key, self.queue, before_id)
        found = bridge.fetch_result(self.queue, self.key, before_id,
                                    timeout_seconds=2, poll_seconds=0.05)
        resolves_before = counts["resolves"]
        reads_before = counts["reads"]
        self.assertEqual(found["id"], before_id)
        self.assertEqual(resolves_before_first, 1)
        self.assertEqual(resolves_before, 2)
        # AFTER: one get_result holds the validated inputs across windows.
        counts["reads"] = 0
        counts["resolves"] = 0
        after_id = bridge.submit_job(self.queue, self.key, "cmd", "C:\\s")
        sealed = []

        def on_window(event):
            if event["window"] >= 2 and not sealed:
                seal_completed(self.key, self.queue, after_id)
                sealed.append(True)

        payload, report = waitmod.get_result(
            self.queue, self.key, after_id, window_timeout=0.4,
            poll_seconds=0.05, max_windows=5, on_window=on_window)
        self.assertEqual(payload["id"], after_id)
        self.assertEqual(counts["resolves"], 1)
        self.assertLess(counts["resolves"], resolves_before)
        self.assertGreaterEqual(reads_before, 2)
        self.assertLessEqual(report["probes"], report["polls_bound"])

    def test_immediate_result_costs_one_probe(self):
        counts = self._counting()
        job_id = bridge.submit_job(self.queue, self.key, "cmd", "C:\\s")
        seal_completed(self.key, self.queue, job_id)
        counts["reads"] = 0
        started = time.monotonic()
        payload, _ = waitmod.get_result(self.queue, self.key, job_id,
                                        window_timeout=5, poll_seconds=1,
                                        max_windows=3)
        elapsed = time.monotonic() - started
        self.assertEqual(payload["id"], job_id)
        self.assertEqual(counts["reads"], 1)
        self.assertLess(elapsed, 1.0)


class GetCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.queue = str(self.root / "queue")
        self.key, self.key_file = make_key(self.root)
        self.key_hex = self.key.hex()

    def tearDown(self):
        self.tmp.cleanup()

    def _assert_no_secret(self, *texts):
        for text in texts:
            self.assertNotIn(self.key_hex, text)

    def _submit(self):
        cmd = self.root / "cmd.txt"
        cmd.write_text("cmd", encoding="utf-8")
        submitted = run_cli(BRIDGE_SCRIPT, "submit", "--queue", self.queue,
                            "--key-file", str(self.key_file),
                            "--command-file", str(cmd), "--cwd", "C:\\s")
        self.assertEqual(submitted.returncode, 0, submitted.stderr)
        return submitted.stdout.strip().split("JOBID=")[1].split()[0]

    def test_cli_bounded_unknown_then_recovery_same_id(self):
        job_id = self._submit()
        first = run_cli(GET_SCRIPT, "--queue", self.queue, "--key-file",
                        str(self.key_file), "--id", job_id,
                        "--window", "0.5", "--poll", "0.1",
                        "--max-windows", "1")
        self.assertEqual(first.returncode, 3)
        first_blob = (first.stdout + first.stderr).lower()
        self.assertIn("unknown", first_blob)
        self.assertIn("same id", first_blob)
        self.assertIn("not evidence the job is running", first_blob)
        self.assertNotIn("still running", first_blob)
        self._assert_no_secret(first.stdout, first.stderr)
        seal_completed(self.key, self.queue, job_id)
        second = run_cli(GET_SCRIPT, "--queue", self.queue, "--key-file",
                         str(self.key_file), "--id", job_id,
                         "--window", "2", "--poll", "0.1")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout.strip())["id"], job_id)
        self.assertIn("verified", second.stderr.lower())
        self.assertNotIn("running", (second.stdout + second.stderr).lower())
        self._assert_no_secret(second.stdout, second.stderr)

    def test_cli_missing_queue_refused_fast(self):
        started = time.monotonic()
        missing = run_cli(GET_SCRIPT, "--queue", str(self.root / "no-queue"),
                          "--key-file", str(self.key_file),
                          "--id", "c" * 32, "--window", "30")
        self.assertEqual(missing.returncode, 2)
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertIn("queue unavailable", missing.stderr.lower())
        self.assertNotIn("running", missing.stderr.lower())
        self._assert_no_secret(missing.stdout, missing.stderr)


if __name__ == "__main__":
    unittest.main()
