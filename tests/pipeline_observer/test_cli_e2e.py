"""End-to-end CLI tests: real entry points, real shells, real bridge.

- The exact one-line command produced by request.py is executed through
  PowerShell (the worker-side parser), with ``$`` and backticks inside
  owner paths, against a fixture GitLab client dir. No live GitLab.
- The probe stdout is sealed and verified through readback, using the
  real signed bridge client with a base64 key whenever it is installed;
  otherwise the fixture double. No corporate access, all files local.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from testkit import (SCRIPTS, SHA1, URL, FixtureBridge, find_real_bridge_dir,
                     passed_client_fixture, powershell_exe, profile_file,
                     write_fixture_client)
import readback
import request as request_mod


def _weird(root: Path, name: str) -> Path:
    target = root / name
    target.mkdir(parents=True, exist_ok=True)
    return target


class GeneratedCommandTests(unittest.TestCase):
    """request -> exact command -> PowerShell -> probe -> seal -> verify."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ps = powershell_exe()
        if self.ps is None:
            self.skipTest("no PowerShell on PATH")

    def _run_case(self, bridge, key: bytes, tag: str) -> dict:
        # Owner paths containing PowerShell-active characters.
        tricky = _weird(self.root, "weird$dir`with`ticks")
        client = write_fixture_client(tricky / "client",
                                      passed_client_fixture())
        env_key = "PIPELINE_OBSERVER_FIXTURE_JSON"
        old = os.environ.get(env_key)
        os.environ[env_key] = str(client / "fixture.json")
        self.addCleanup(lambda: (
            os.environ.pop(env_key, None),
            old is not None and os.environ.__setitem__(env_key, old)))
        profile = profile_file(tricky)
        req_file = self.root / f"{tag}.request.json"
        cmd_file = self.root / f"{tag}.command.txt"
        out_file = tricky / f"{tag}.receipt.json"
        code = request_mod.main([
            "--mr-url", URL, "--expected-sha", SHA1,
            "--profile", str(profile),
            "--python", sys.executable,
            "--lenovo-flow-dir", str(SCRIPTS),
            "--lenovo-checkout", str(self.root),
            "--lenovo-client-dir", str(client),
            "--request-file", str(req_file),
            "--command-file", str(cmd_file)])
        self.assertEqual(code, 0)
        line = cmd_file.read_text(encoding="utf-8").strip("\n")
        self.assertNotIn("\n", line)
        # The exact generated line, through the real worker-side parser.
        proc = subprocess.run(
            [self.ps, "-NoProfile", "-Command", line],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120, check=False,
            cwd=str(self.root))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr[:500])
        stdout = proc.stdout or ""
        receipt = json.loads(stdout)
        self.assertEqual(receipt["verdict"], "passed")
        self.assertEqual(receipt["expected_sha"], SHA1)
        # Seal exactly like a worker, then verify the bound result.
        job_id = os.urandom(16).hex()
        payload = bridge.build_result_payload(
            job_id, "ok", 0, stdout=stdout, stderr="")
        bridge.publish(str(self.root / "queue" / "results"), job_id,
                       bridge.seal(payload, key))
        fetched = bridge.fetch_result(
            str(self.root / "queue"), key, job_id, timeout_seconds=10,
            poll_seconds=0.05)
        verified = readback.verify(fetched, job_id, URL, SHA1)
        self.assertEqual(verified["verdict"], "passed")
        out_file.write_text(json.dumps(verified, ensure_ascii=False),
                            encoding="utf-8")
        return verified

    def test_exact_command_with_dollar_and_backtick(self) -> None:
        bridge = FixtureBridge()
        key = os.urandom(32)
        verified = self._run_case(bridge, key, "ps")
        self.assertEqual(verified["probe"]["expected_sha"], SHA1)

    def test_ps_quoting_keeps_values_literal(self) -> None:
        _, line = request_mod.build_request(
            URL, SHA1, "C:\\prof.json", "C:\\py\\python.exe",
            "C:\\fl ow", "C:\\out")
        self.assertTrue(line.startswith("& '"))
        quoted = request_mod._ps_quote("a$b`c\"d'e")
        self.assertEqual(quoted, "'a$b`c\"d''e'")
        with self.assertRaises(Exception):
            request_mod.build_request(
                URL, SHA1, "p", "C:\\py", "C:\\f", "C:\\o\nx")


class RealBridgeReadbackTests(unittest.TestCase):
    """readback.main against the installed signed bridge, base64 key."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bridge_dir = find_real_bridge_dir()
        if self.bridge_dir is None:
            self.skipTest("no installed signed bridge client")

    def test_main_with_real_bridge_and_base64_key(self) -> None:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "real_bridge", str(self.bridge_dir / "lan_bridge.py"))
        assert spec is not None and spec.loader is not None
        real = importlib.util.module_from_spec(spec)
        sys.modules["real_bridge"] = real
        spec.loader.exec_module(real)
        queue = str(self.root / "queue")
        raw_key = os.urandom(32)
        key_file = self.root / "lane.b64"
        key_file.write_text(base64.b64encode(raw_key).decode("ascii"),
                            encoding="ascii")
        key = real.load_key(str(key_file), "base64", queue)
        from testkit import passed_probe_receipt
        job_id = real.generate_job_id()
        payload = real.build_result_payload(
            job_id, "completed", 0,
            stdout=json.dumps(passed_probe_receipt()), stderr="")
        real.publish(str(Path(queue) / "results"), job_id,
                     real.seal(payload, key))
        out = self.root / "receipt.json"
        code = readback.main([
            "--queue", queue, "--key-file", str(key_file),
            "--key-format", "base64", "--job-id", job_id,
            "--expected-mr-url", URL, "--expected-sha", SHA1,
            "--bridge-dir", str(self.bridge_dir),
            "--receipt-out", str(out),
            "--timeout", "10", "--poll", "0.05"])
        self.assertEqual(code, 0)
        receipt = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(receipt["verdict"], "passed")
        self.assertEqual(receipt["probe"]["expected_sha"], SHA1)


if __name__ == "__main__":
    unittest.main()
