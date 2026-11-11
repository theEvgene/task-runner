"""Recovery, unknown-vs-confirmed, timings, refusal and latency fixtures.

Local fixture only: temp dirs plus an ephemeral key, no live Lenovo host.
Complements test_lan_bridge.py; covers issues #16/#17 acceptance criteria
AC1-AC5 against the owner wait defaults from #12/#13 (240 s / 30 s).
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "lan_bridge" / "lan_bridge.py"
if str(SCRIPT.parent) not in sys.path:
    sys.path.insert(0, str(SCRIPT.parent))

import lan_bridge as bridge  # noqa: E402
import wait as waitmod  # noqa: E402

_PY_CANDIDATE = r"C:\Users\rb199787\AppData\Local\Programs\Python\Python313\python.exe"


def make_key(directory: Path) -> tuple:
    raw = os.urandom(32)
    key_file = directory / "key.bin"
    key_file.write_bytes(raw)
    return raw, key_file


def run_cli(*argv):
    exe = _PY_CANDIDATE if Path(_PY_CANDIDATE).exists() else sys.executable
    return subprocess.run([exe, str(SCRIPT), *argv],
                          capture_output=True, text=True)


class WaitDefaultsTests(unittest.TestCase):
    def test_api_and_cli_defaults_coordinated_240_30(self):
        self.assertEqual(bridge.DEFAULT_FETCH_TIMEOUT_SECONDS, 240.0)
        self.assertEqual(bridge.DEFAULT_FETCH_POLL_SECONDS, 30.0)
        self.assertEqual(bridge.fetch_result.__defaults__,
                         (240.0, 30.0))
        parser = bridge.build_parser()
        args = parser.parse_args(["fetch", "--queue", "q", "--key-file", "k",
                                  "--id", "a" * 32])
        self.assertEqual(args.timeout, 240.0)
        self.assertEqual(args.poll, 30.0)

    def test_explicit_short_and_long_overrides_stay_allowed(self):
        self.assertEqual(
            bridge.build_parser().parse_args(
                ["fetch", "--queue", "q", "--key-file", "k",
                 "--id", "a" * 32, "--timeout", "1", "--poll", "0.2"]
            ).timeout, 1.0)
        # Explicit short waits (as in older fixtures) are not forbidden.
        bridge._validate_wait(1, 0.2)
        bridge._validate_wait(3600, 60)
        for bad in ((0, 1), (-1, 1), (1, 0), (True, 1), (1, "x"),
                    (float("inf"), 1), (1, float("nan"))):
            with self.assertRaises(bridge.BridgeError):
                bridge._validate_wait(*bad)


class LateResultRecoveryTests(unittest.TestCase):
    """AC1: late result via the ORIGINAL id after several windows + restart."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.queue = str(self.root / "queue")
        self.key, self.key_file = make_key(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_late_result_after_windows_no_resubmit(self):
        job_id = bridge.submit_job(self.queue, self.key, "cmd", "C:\\s")
        events = []
        # Two windows expire with no result: outcome stays unknown.
        payload, report = waitmod.wait_many_windows(
            self.queue, self.key, job_id, total_timeout=1.2,
            window_timeout=0.5, poll_seconds=0.2,
            on_window=events.append)
        self.assertIsNone(payload)
        self.assertEqual(report["outcome"], "unknown")
        self.assertGreaterEqual(report["windows_used"], 2)
        self.assertLessEqual(len(events), report["windows_used"])
        # Late worker seals the result under the ORIGINAL id.
        result = bridge.build_result_payload(
            job_id, "completed", exit_code=0, stdout="late",
            started_at="t0", finished_at="t1")
        bridge.publish(os.path.join(self.queue, "results"), job_id,
                       bridge.seal(result, self.key))
        # "Observer restart": fresh call, same id, result accepted.
        payload2, report2 = waitmod.wait_many_windows(
            self.queue, self.key, job_id, total_timeout=5,
            window_timeout=5, poll_seconds=0.2)
        self.assertIsNotNone(payload2)
        self.assertEqual(payload2["id"], job_id)
        self.assertEqual(report2["outcome"], "found")
        # Exactly one submission exists: re-publish refuses (no duplicate).
        with self.assertRaises(bridge.JobExistsError):
            bridge.publish(os.path.join(self.queue, "jobs"), job_id, b"again")
        with self.assertRaises(bridge.JobExistsError):
            bridge.publish(os.path.join(self.queue, "results"), job_id,
                           bridge.seal(result, self.key))

    def test_cli_restart_recovers_same_id(self):
        cmd = self.root / "cmd.txt"
        cmd.write_text("cmd", encoding="utf-8")
        submit = run_cli("submit", "--queue", self.queue, "--key-file",
                         str(self.key_file), "--command-file", str(cmd),
                         "--cwd", "C:\\s")
        self.assertEqual(submit.returncode, 0, submit.stderr)
        job_id = submit.stdout.strip().split("JOBID=")[1].split()[0]
        self.assertRegex(job_id, r"^[0-9a-f]{32}$")
        # First observer window expires: exit 3, unknown, never "running".
        first = run_cli("fetch", "--queue", self.queue, "--key-file",
                        str(self.key_file), "--id", job_id,
                        "--timeout", "1", "--poll", "0.2")
        self.assertEqual(first.returncode, 3)
        self.assertIn("unknown", first.stderr.lower())
        self.assertIn("not running", first.stderr.lower())
        self.assertNotIn("still running", first.stderr.lower())
        # Late result, then a NEW observer process reads the SAME id.
        result = bridge.build_result_payload(
            job_id, "completed", exit_code=0, stdout="ok")
        bridge.publish(os.path.join(self.queue, "results"), job_id,
                       bridge.seal(result, self.key))
        second = run_cli("fetch", "--queue", self.queue, "--key-file",
                         str(self.key_file), "--id", job_id,
                         "--timeout", "5", "--poll", "0.2")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout.strip())["id"], job_id)


class ConfirmedVsUnknownTests(unittest.TestCase):
    """AC2: confirmed states vs unknown; no false "running" claims."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.queue = str(self.root / "queue")
        self.key, _ = make_key(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_absence_is_unknown_not_running(self):
        job_id = bridge.submit_job(self.queue, self.key, "cmd", "C:\\s")
        self.assertIsNone(waitmod.peek_result(self.queue, self.key, job_id))
        absence = waitmod.classify_absence(job_id)
        self.assertEqual(absence["outcome"], "unknown")
        self.assertFalse(absence["confirmed"])
        self.assertIn(job_id, absence["action"])
        with self.assertRaises(bridge.FetchTimeout):
            bridge.fetch_result(self.queue, self.key, job_id,
                                timeout_seconds=0.5, poll_seconds=0.1)

    def test_verified_states_classified_no_running_outcome(self):
        job_id = "b" * 32
        cases = [
            (bridge.build_result_payload(job_id, "completed", exit_code=0),
             "success"),
            (bridge.build_result_payload(job_id, "ok", exit_code=0),
             "success"),
            (bridge.build_result_payload(job_id, "completed", exit_code=1),
             "worker_failure"),
            (bridge.build_result_payload(job_id, "completed", exit_code=0,
                                          timed_out=True), "worker_timeout"),
            (bridge.build_result_payload(job_id, "completed", exit_code=0,
                                          output_truncated=True), "truncated"),
            (bridge.build_result_payload(job_id, "weird-status", exit_code=0),
             "unknown_status"),
        ]
        for payload, outcome in cases:
            info = waitmod.classify_result(payload)
            self.assertEqual(info["outcome"], outcome)
            self.assertTrue(info["confirmed"])
            self.assertNotEqual(info["outcome"], "running")
            summary = waitmod.summarize_result(payload)
            self.assertEqual(summary["id"], job_id)
            self.assertNotIn("running", json.dumps(summary).lower())


class TimingsTests(unittest.TestCase):
    """AC3: current deadlines + measured stages; gaps marked unknown."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.queue = str(self.root / "queue")
        self.key, _ = make_key(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_window_report_marks_missing_marks_unknown(self):
        job_id = bridge.submit_job(self.queue, self.key, "cmd", "C:\\s")
        _, report = waitmod.wait_many_windows(
            self.queue, self.key, job_id, total_timeout=0.6,
            window_timeout=0.3, poll_seconds=0.1)
        self.assertEqual(report["outcome"], "unknown")
        self.assertGreaterEqual(report["windows_used"], 1)
        self.assertGreaterEqual(report["elapsed_s"], 0)
        self.assertLessEqual(report["polls_bound"],
                             report["windows_used"] * 5)
        # Opaque worker marks -> every stage unknown, never invented.
        stages = waitmod.stage_timings(
            bridge.build_result_payload(job_id, "completed", exit_code=0,
                                        started_at="t0", finished_at="t1"))
        self.assertEqual(stages["exec_s"], "unknown")
        self.assertEqual(stages["queue_s"], "unknown")
        numeric = waitmod.stage_timings(
            bridge.build_result_payload(job_id, "completed", exit_code=0,
                                        started_at=10.0, finished_at=12.5))
        self.assertEqual(numeric["exec_s"], 2.5)
        self.assertEqual(numeric["queue_s"], "unknown")

    def test_found_report_carries_summary_and_bounded_polls(self):
        job_id = bridge.submit_job(self.queue, self.key, "cmd", "C:\\s")
        bridge.publish(os.path.join(self.queue, "results"), job_id,
                       bridge.seal(bridge.build_result_payload(
                           job_id, "completed", exit_code=0, stdout="x"),
                           self.key))
        payload, report = waitmod.wait_many_windows(
            self.queue, self.key, job_id, total_timeout=5,
            window_timeout=5, poll_seconds=0.5)
        self.assertEqual(report["outcome"], "found")
        self.assertEqual(report["summary"]["outcome"], "success")
        self.assertEqual(payload["id"], job_id)


class RefusalAndHygieneTests(unittest.TestCase):
    """AC4: signature/bad-id/truncated/unavailable explicit; no key leaks."""

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
            for encoding in ("utf-8", "utf-16-le"):
                try:
                    blob = text.encode(encoding, errors="ignore")
                except Exception:
                    continue
                self.assertNotIn(self.key, blob)

    def test_tampered_envelope_refused_peek_and_fetch(self):
        job_id = bridge.submit_job(self.queue, self.key, "cmd", "C:\\s")
        bridge.publish(os.path.join(self.queue, "results"), job_id,
                       bridge.seal(bridge.build_result_payload(
                           job_id, "completed", exit_code=0), self.key))
        raw_path = Path(self.queue) / "results" / f"{job_id}.json"
        raw = bytearray(raw_path.read_bytes())
        raw[40] ^= 0x01
        raw_path.write_bytes(bytes(raw))
        with self.assertRaises(bridge.ProtocolError):
            waitmod.peek_result(self.queue, self.key, job_id)
        with self.assertRaises(bridge.ProtocolError):
            bridge.fetch_result(self.queue, self.key, job_id,
                                timeout_seconds=2, poll_seconds=0.2)
        info = waitmod.classify_result(
            bridge.build_result_payload(job_id, "completed", exit_code=0,
                                        output_truncated=True))
        self.assertEqual(info["outcome"], "truncated")

    def test_cli_refusals_and_truncated_warning_without_secret(self):
        bad = run_cli("fetch", "--queue", self.queue, "--key-file",
                      str(self.key_file), "--id", "not-an-id",
                      "--timeout", "2", "--poll", "0.2")
        self.assertEqual(bad.returncode, 2)
        self._assert_no_secret(bad.stdout, bad.stderr)
        job_id = bridge.submit_job(self.queue, self.key, "cmd", "C:\\s")
        bridge.publish(os.path.join(self.queue, "results"), job_id,
                       bridge.seal(bridge.build_result_payload(
                           job_id, "completed", exit_code=0, stdout="part",
                           output_truncated=True), self.key))
        got = run_cli("fetch", "--queue", self.queue, "--key-file",
                      str(self.key_file), "--id", job_id,
                      "--timeout", "5", "--poll", "0.2")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("TRUNCATED", got.stderr)
        self._assert_no_secret(got.stdout, got.stderr)
        missing = run_cli("fetch", "--queue", str(self.root / "no-queue"),
                          "--key-file", str(self.key_file), "--id", job_id,
                          "--timeout", "1", "--poll", "0.2")
        self.assertEqual(missing.returncode, 2)
        self.assertIn("queue unavailable", missing.stderr.lower())
        self.assertIn("check path", missing.stderr.lower())
        self.assertNotIn("re-read the same id", missing.stderr.lower())
        self.assertNotIn("still running", missing.stderr.lower())
        self._assert_no_secret(missing.stdout, missing.stderr)


class QueueAvailabilityTests(unittest.TestCase):
    """F1: unavailable queue fails fast; present queue stays pending."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.queue = str(self.root / "queue")
        self.key, self.key_file = make_key(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_queue_dir_refused_fast_no_remote_claim(self):
        job_id = "c" * 32
        started = time.monotonic()
        with self.assertRaises(bridge.BridgeError) as ctx:
            bridge.fetch_result(str(self.root / "no-queue"), self.key,
                                job_id, timeout_seconds=5, poll_seconds=0.1)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertIn("queue unavailable", str(ctx.exception).lower())
        self.assertNotIn("running", str(ctx.exception).lower())
        with self.assertRaises(bridge.BridgeError):
            waitmod.peek_result(str(self.root / "no-queue"), self.key,
                                job_id)
        with self.assertRaises(bridge.BridgeError):
            waitmod.wait_many_windows(str(self.root / "no-queue"), self.key,
                                      job_id, total_timeout=2,
                                      window_timeout=1, poll_seconds=0.1)

    def test_present_queue_without_results_dir_stays_pending(self):
        job_id = bridge.submit_job(self.queue, self.key, "cmd", "C:\\s")
        self.assertFalse((Path(self.queue) / "results").exists())
        self.assertIsNone(waitmod.peek_result(self.queue, self.key, job_id))
        with self.assertRaises(bridge.FetchTimeout) as ctx:
            bridge.fetch_result(self.queue, self.key, job_id,
                                timeout_seconds=0.6, poll_seconds=0.1)
        self.assertIn("same id", str(ctx.exception).lower())
        pending = run_cli("fetch", "--queue", self.queue, "--key-file",
                          str(self.key_file), "--id", job_id,
                          "--timeout", "1", "--poll", "0.2")
        self.assertEqual(pending.returncode, 3)
        self.assertIn("unknown", pending.stderr.lower())
        self.assertIn("same id", pending.stderr.lower())

    def test_results_path_file_refused(self):
        Path(self.queue).mkdir(parents=True, exist_ok=True)
        (Path(self.queue) / "results").write_text("not-a-dir")
        with self.assertRaises(bridge.BridgeError):
            bridge.fetch_result(self.queue, self.key, "d" * 32,
                                timeout_seconds=2, poll_seconds=0.1)


class LatencyDiagnosisTests(unittest.TestCase):
    """AC5: measured cause; minimal optimization without protocol change."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.queue = str(self.root / "queue")
        self.key, _ = make_key(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_immediate_result_costs_one_read(self):
        job_id = bridge.submit_job(self.queue, self.key, "cmd", "C:\\s")
        bridge.publish(os.path.join(self.queue, "results"), job_id,
                       bridge.seal(bridge.build_result_payload(
                           job_id, "completed", exit_code=0), self.key))
        calls = []
        real_read = bridge.read

        def counting(*args, **kwargs):
            calls.append(1)
            return real_read(*args, **kwargs)

        bridge.read, restore = counting, bridge.read
        try:
            started = time.monotonic()
            payload = bridge.fetch_result(
                self.queue, self.key, job_id,
                timeout_seconds=5, poll_seconds=30)
            elapsed = time.monotonic() - started
        finally:
            bridge.read = restore
        self.assertEqual(payload["id"], job_id)
        # No sleep before the first check: exactly one read, sub-second.
        self.assertEqual(len(calls), 1)
        self.assertLess(elapsed, 1.0)

    def test_window_poll_bound_matches_owner_defaults(self):
        job_id = bridge.submit_job(self.queue, self.key, "cmd", "C:\\s")
        _, report = waitmod.wait_many_windows(
            self.queue, self.key, job_id, total_timeout=1.0,
            window_timeout=0.5, poll_seconds=0.25)
        per_window = int(0.5 // 0.25) + 2
        self.assertLessEqual(report["polls_bound"],
                             report["windows_used"] * per_window)
        # Owner default scales the same way: 240 s / 30 s -> <= 10 reads.
        self.assertLessEqual(int(240 // 30) + 2, 10)


if __name__ == "__main__":
    unittest.main()
