"""Integration tests for the signed LAN command bridge (fixture only).

Everything runs against temporary directories with an ephemeral key; no
live Lenovo queue, no network. The interop vector below is the
published non-secret MVP fixture (fake key, fixed payload/MAC).
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "lan_bridge" / "lan_bridge.py"
if str(SCRIPT.parent) not in sys.path:
    sys.path.insert(0, str(SCRIPT.parent))

import lan_bridge as bridge  # noqa: E402

# Published non-secret interop vector (LanTaskRunnerMVP tests fixture).
VECTOR_KEY_HEX = "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
VECTOR_PAYLOAD = {"kind": "ping",
                  "id": "0123456789abcdef0123456789abcdef", "msg": "h"}
VECTOR_B64 = ("eyJraW5kIjoicGluZyIsImlkIjoiMDEyMzQ1Njc4OWFiY2RlZjAxMjM0"
              "NTY3ODlhYmNkZWYiLCJtc2ciOiJoIn0=")
VECTOR_MAC = "9a290974e53b1a2bd31d761e7cbc8e7e656179946d349c3fdc1f9146ce3a8328"


def make_key_file(directory: Path, fmt: str = "raw") -> Path:
    raw = os.urandom(32)
    path = directory / ("key.bin" if fmt == "raw" else "key.b64")
    if fmt == "raw":
        path.write_bytes(raw)
    else:
        path.write_text(base64.b64encode(raw).decode("ascii"), encoding="ascii")
    return path


class InteropVectorTests(unittest.TestCase):
    def test_seal_reproduces_published_vector(self):
        key = bytes.fromhex(VECTOR_KEY_HEX)
        envelope = json.loads(bridge.seal(dict(VECTOR_PAYLOAD), key).decode())
        self.assertEqual(envelope["payload_b64"], VECTOR_B64)
        self.assertEqual(envelope["mac"], VECTOR_MAC)

    def test_open_accepts_published_vector(self):
        key = bytes.fromhex(VECTOR_KEY_HEX)
        payload_bytes = base64.b64decode(VECTOR_B64)
        envelope = {"v": 1, "kind": "ping",
                    "id": VECTOR_PAYLOAD["id"],
                    "payload_b64": VECTOR_B64, "mac": VECTOR_MAC}
        raw = json.dumps(envelope, separators=(",", ":")).encode()
        self.assertEqual(bridge.open_envelope(raw, key, "ping",
                                              VECTOR_PAYLOAD["id"])["msg"], "h")
        self.assertEqual(payload_bytes, json.dumps(
            VECTOR_PAYLOAD, ensure_ascii=False,
            separators=(",", ":")).encode("utf-8"))

    def test_binding_before_trust(self):
        key = bytes.fromhex(VECTOR_KEY_HEX)
        other = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        payload = dict(VECTOR_PAYLOAD)
        payload["id"] = other
        raw = bridge.seal(payload, key)
        # Valid MAC for another id must not open as the requested one.
        with self.assertRaises(bridge.ProtocolError):
            bridge.open_envelope(raw, key, "ping", VECTOR_PAYLOAD["id"])

    def test_tampered_payload_rejected(self):
        key = bytes.fromhex(VECTOR_KEY_HEX)
        raw = bytearray(bridge.seal(dict(VECTOR_PAYLOAD), key))
        raw[40] ^= 0x01
        with self.assertRaises(bridge.ProtocolError):
            bridge.open_envelope(bytes(raw), key, "ping", VECTOR_PAYLOAD["id"])

    def test_wrong_key_rejected(self):
        raw = bridge.seal(dict(VECTOR_PAYLOAD), bytes.fromhex(VECTOR_KEY_HEX))
        with self.assertRaises(bridge.ProtocolError):
            bridge.open_envelope(raw, os.urandom(32), "ping", VECTOR_PAYLOAD["id"])

    def test_bad_kind_version_id_rejected(self):
        key = bytes.fromhex(VECTOR_KEY_HEX)
        base = json.loads(bridge.seal(dict(VECTOR_PAYLOAD), key).decode())
        for mutate, field in ((lambda e: e.update(kind="job"), "bad kind"),
                              (lambda e: e.update(v=2), "bad version"),
                              (lambda e: e.update(id="0" * 32), "bad id")):
            env = dict(base)
            mutate(env)
            with self.assertRaises(bridge.ProtocolError, msg=field):
                bridge.open_envelope(json.dumps(env).encode(), key,
                                     "ping", VECTOR_PAYLOAD["id"])


class ProducerValidationTests(unittest.TestCase):
    def test_job_ids_unique_and_shaped(self):
        ids = {bridge.generate_job_id() for _ in range(50)}
        self.assertEqual(len(ids), 50)
        for job_id in ids:
            self.assertRegex(job_id, r"^[0-9a-f]{32}$")

    def test_ranges_reject_bool_and_bounds(self):
        for bad in (True, False, 0, -1, 3601, "60", 1.5):
            with self.assertRaises((ValueError, TypeError)):
                bridge.validate_timeout(bad)
        for bad in (True, 0, -5, 262145):
            with self.assertRaises((ValueError, TypeError)):
                bridge.validate_max_output_bytes(bad)

    def test_job_payload_carries_command(self):
        payload = bridge.build_job_payload(
            job_id="a" * 32, command="python x.py read --issue DEMO-1 --full",
            working_directory="C:\\skill", timeout_seconds=300,
            max_output_bytes=65536)
        self.assertEqual(payload["command"],
                         "python x.py read --issue DEMO-1 --full")
        self.assertEqual(payload["working_directory"], "C:\\skill")

    def test_empty_command_and_cwd_refused(self):
        with self.assertRaises(ValueError):
            bridge.build_job_payload("a" * 32, "   ", "C:\\x")
        with self.assertRaises(ValueError):
            bridge.build_job_payload("a" * 32, "cmd", "")


class FileQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.jobs = str(Path(self.tmp.name) / "jobs")

    def tearDown(self):
        self.tmp.cleanup()

    def test_publish_read_round_trip(self):
        job_id = bridge.generate_job_id()
        with self.assertRaises(bridge.JobNotFoundError):
            bridge.read(self.jobs, job_id)
        bridge.publish(self.jobs, job_id, b'{"a":1}')
        self.assertEqual(bridge.read(self.jobs, job_id), b'{"a":1}')

    def test_no_duplicate_publish(self):
        job_id = bridge.generate_job_id()
        bridge.publish(self.jobs, job_id, b"first")
        with self.assertRaises(bridge.JobExistsError):
            bridge.publish(self.jobs, job_id, b"second")
        self.assertEqual(bridge.read(self.jobs, job_id), b"first")

    def test_invalid_ids_rejected(self):
        for bad in ["", "ABC", "a" * 31, "A" * 32, "../evil", "a/b",
                    "a" * 32 + "\n", 123, None]:
            with self.assertRaises((bridge.InvalidJobIdError,
                                    bridge.UnsafePathError), msg=repr(bad)):
                bridge.publish(self.jobs, bad, b"x")

    def test_size_limit(self):
        job_id = bridge.generate_job_id()
        bridge.publish(self.jobs, job_id, b"0123456789")
        with self.assertRaises(bridge.SizeLimitError):
            bridge.read(self.jobs, job_id, max_bytes=4)

    def test_tmp_files_ignored(self):
        job_id = bridge.generate_job_id()
        (Path(self.jobs)).mkdir(parents=True, exist_ok=True)
        (Path(self.jobs) / f".{job_id}.abc.tmp").write_bytes(b"tmp")
        with self.assertRaises(bridge.JobNotFoundError):
            bridge.read(self.jobs, job_id)


class KeyHandlingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_raw_and_base64_keys_load(self):
        raw = os.urandom(32)
        p_raw = self.root / "k.raw"
        p_raw.write_bytes(raw)
        self.assertEqual(bridge.load_key(str(p_raw), "raw",
                                         str(self.root / "q")), raw)
        p_b64 = self.root / "k.b64"
        p_b64.write_text(base64.b64encode(raw).decode("ascii"), encoding="ascii")
        self.assertEqual(bridge.load_key(str(p_b64), "base64",
                                         str(self.root / "q")), raw)

    def test_key_inside_queue_refused(self):
        queue = self.root / "queue"
        (queue).mkdir()
        key = queue / "key.bin"
        key.write_bytes(os.urandom(32))
        with self.assertRaises(bridge.BridgeError):
            bridge.load_key(str(key), "raw", str(queue))

    def test_short_key_refused(self):
        key = self.root / "short.bin"
        key.write_bytes(b"short")
        with self.assertRaises(bridge.BridgeError):
            bridge.load_key(str(key), "raw", str(self.root / "q"))


class EndToEndFixtureTests(unittest.TestCase):
    """Submit -> fake worker -> fetch over temp dirs with an ephemeral key."""

    MANAGED_STDOUT = json.dumps({"state": "ok", "issue": "DEMO-123",
                                 "parent": {"presence": "observed"}},
                                ensure_ascii=False)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.queue = str(self.root / "queue")
        self.key_file = make_key_file(self.root)
        self.key = self.key_file.read_bytes()
        self.cmd = self.root / "cmd.txt"
        self.cmd.write_text("python ..\\example-workflow\\scripts\\example_task.py"
                            " read --issue DEMO-123 --full", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _run_worker_once(self, job_id: str, expected_in_command: str | None = None) -> None:
        """Fake worker: verify job envelope, execute nothing, seal result."""
        raw = bridge.read(os.path.join(self.queue, "jobs"), job_id)
        job = bridge.open_envelope(raw, self.key, "job", job_id)
        self.assertTrue(job["command"].strip())
        if expected_in_command is not None:
            self.assertIn(expected_in_command, job["command"])
        result = bridge.build_result_payload(
            job_id, "completed", exit_code=0, stdout=self.MANAGED_STDOUT,
            stderr="", started_at="t0", finished_at="t1")
        bridge.publish(os.path.join(self.queue, "results"), job_id,
                       bridge.seal(result, self.key))

    def test_submit_worker_fetch_round_trip(self):
        job_id = bridge.submit_job(self.queue, self.key,
                                   self.cmd.read_text(encoding="utf-8"),
                                   "C:\\lenovo-skill", 300, 65536)
        self.assertRegex(job_id, r"^[0-9a-f]{32}$")
        self._run_worker_once(job_id, "--issue DEMO-123 --full")
        payload = bridge.fetch_result(self.queue, self.key, job_id,
                                      timeout_seconds=5, poll_seconds=0.1)
        self.assertEqual(payload["id"], job_id)
        self.assertEqual(payload["exit_code"], 0)
        body = json.loads(payload["stdout"])
        self.assertEqual(body["parent"]["presence"], "observed")

    def test_result_bound_to_requested_id(self):
        first = bridge.submit_job(self.queue, self.key, "cmd-one", "C:\\s")
        self._run_worker_once(first)
        other = bridge.submit_job(self.queue, self.key, "cmd-two", "C:\\s")
        # No result for `other` yet: only its own id verifies.
        with self.assertRaises(bridge.FetchTimeout):
            bridge.fetch_result(self.queue, self.key, other,
                                timeout_seconds=1, poll_seconds=0.2)
        payload = bridge.fetch_result(self.queue, self.key, first,
                                      timeout_seconds=5, poll_seconds=0.1)
        self.assertEqual(payload["id"], first)

    def test_cli_submit_fetch_over_real_files(self):
        submit = subprocess.run(
            [sys.executable, str(SCRIPT), "submit", "--queue", self.queue,
             "--key-file", str(self.key_file), "--command-file", str(self.cmd),
             "--cwd", "C:\\lenovo-skill", "--timeout", "300"],
            capture_output=True, text=True)
        self.assertEqual(submit.returncode, 0, submit.stderr)
        job_id = submit.stdout.strip().split("JOBID=")[1].strip().split()[0]
        self.assertRegex(job_id, r"^[0-9a-f]{32}$")
        self._run_worker_once(job_id, "--issue DEMO-123 --full")
        receipt = self.root / "receipt.json"
        fetch = subprocess.run(
            [sys.executable, str(SCRIPT), "fetch", "--queue", self.queue,
             "--key-file", str(self.key_file), "--id", job_id,
             "--timeout", "30", "--poll", "0.2",
             "--receipt-out", str(receipt)],
            capture_output=True, text=True)
        self.assertEqual(fetch.returncode, 0, fetch.stderr)
        payload = json.loads(fetch.stdout.strip())
        self.assertEqual(payload["id"], job_id)
        self.assertTrue(receipt.is_file())

    def test_cli_fetch_timeout_without_result(self):
        job_id = bridge.submit_job(self.queue, self.key, "cmd", "C:\\s")
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "fetch", "--queue", self.queue,
             "--key-file", str(self.key_file), "--id", job_id,
             "--timeout", "1", "--poll", "0.2"],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 3)

    def test_cli_refusals(self):
        empty = self.root / "empty.txt"
        empty.write_text("  \n", encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "submit", "--queue", self.queue,
             "--key-file", str(self.key_file), "--command-file", str(empty),
             "--cwd", "C:\\s"],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "fetch", "--queue", self.queue,
             "--key-file", str(self.key_file), "--id", "not-an-id"],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main()
