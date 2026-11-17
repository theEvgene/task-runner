"""Fast contract tests for the Dell-side Lenovo default-playback mute route.

Hermetic: an ephemeral in-memory key seals and re-opens every envelope, so
every ``decide`` input is MAC-bound exactly like ``fetch`` provides it. No
queue dir, host, share, network, or live audio change is ever touched: the
``read`` path is covered through sealed fixtures, and the ``mute`` path never
executes here.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parents[1]
AUDIO_DIR = ROOT / "scripts" / "lenovo_audio"
AUDIO_CLI = AUDIO_DIR / "audio.py"
BRIDGE_DIR = ROOT / "scripts" / "lan_bridge"

sys.path.insert(0, str(AUDIO_DIR))
sys.path.insert(0, str(BRIDGE_DIR))

import audio as audio_mod  # noqa: E402
import lan_bridge as bridge  # noqa: E402

try:
    import syntax as syntax_mod  # noqa: E402
except ImportError:  # pragma: no cover - syntax gate always ships with the bridge
    syntax_mod = None

JOB_ID = "0123456789abcdef0123456789abcdef"
DISPATCH_RE = re.compile(
    r"if\(\$o -eq 'mute'\)\{\$m=\[LenovoAudioEndpoint\]::SetMute\(\$true\)\}"
    r"else\{\$m=\[LenovoAudioEndpoint\]::GetMute\(\)\}")


def seal_result(key: bytes, job_id: str, stdout: str, status: str = "ok",
                exit_code: int = 0, timed_out: bool = False,
                truncated: bool = False) -> dict:
    payload = bridge.build_result_payload(
        job_id, status, exit_code, stdout=stdout, stderr="",
        started_at="t0", finished_at="t1",
        timed_out=timed_out, output_truncated=truncated)
    return bridge.open_envelope(bridge.seal(payload, key), key, "result", job_id)


def receipt(operation: str, muted: bool = True, ok: bool = True,
            error: str = "audio-operation-failed") -> str:
    body: dict = {"schema": "lenovo-audio-state/1", "operation": operation,
                  "ok": ok}
    if ok:
        body["muted"] = muted
    else:
        body["error"] = error
    return json.dumps(body, separators=(",", ":"))


class BuildTests(unittest.TestCase):
    def test_both_operations_render_one_launch_line(self):
        for operation in ("mute", "read"):
            with self.subTest(operation=operation):
                command = audio_mod.build(operation)
                self.assertTrue(command.startswith(
                    "powershell.exe -NoProfile -NonInteractive -Command \""))
                self.assertTrue(command.endswith("\""))
                self.assertEqual(command.count("\""), 2)
                self.assertNotIn("\n", command)
                self.assertNotIn("\r", command)
                self.assertNotIn("\x00", command)

    def test_build_is_deterministic_and_carries_no_paths_or_secrets(self):
        command = audio_mod.build("mute")
        self.assertEqual(command, audio_mod.build("mute"))
        for marker in ("C:\\", "C:/", "Users", "key", "token", "cookie"):
            self.assertNotIn(marker, command)

    def test_mute_pins_mute_and_read_pins_read(self):
        self.assertIn("$o='mute'", audio_mod.render_inner("mute"))
        self.assertIn("$o='read'", audio_mod.render_inner("read"))

    def test_dispatch_sets_unconditionally_and_read_never_sets(self):
        for operation in ("mute", "read"):
            with self.subTest(operation=operation):
                inner = audio_mod.render_inner(operation)
                self.assertRegex(inner, DISPATCH_RE)
        template_sets = audio_mod.INNER_TEMPLATE.count("SetMute(")
        self.assertEqual(template_sets, 1)

    def test_command_fits_the_worker_cmd_line_budget(self):
        # The worker executes through cmd.exe (8191 chars per line).
        for operation in ("mute", "read"):
            with self.subTest(operation=operation):
                self.assertLess(len(audio_mod.build(operation)), 8191)

    def test_no_device_constant_matches_signed_e_notfound(self):
        self.assertEqual(audio_mod.NO_DEVICE_HRESULT,
                         ctypes.c_int32(0x80070490).value)
        self.assertIn(str(audio_mod.NO_DEVICE_HRESULT),
                      audio_mod.render_inner("mute"))

    def test_embedded_csharp_names_the_default_render_endpoint(self):
        source = base64.b64decode(audio_mod.csharp_base64()).decode("utf-8")
        self.assertEqual(source, audio_mod.CSHARP_SOURCE)
        self.assertIn("GetDefaultAudioEndpoint", source)
        self.assertIn("DataFlowRender = 0", source)
        self.assertIn("RoleMultimedia = 1", source)
        self.assertIn("SetMute", source)

    def test_com_guids_are_pinned_byte_exact(self):
        pinned = (
            "BCDE0395-E52F-467C-8E3D-C4579291692E",  # CLSID_MMDeviceEnumerator
            "A95664D2-9614-4F35-A746-DE8DB63617E6",  # IID_IMMDeviceEnumerator
            "D666063F-1587-4E43-81F1-B948E807363F",  # IID_IMMDevice
            "5CDF2C82-841E-4546-9722-0CF74078229A",  # IID_IAudioEndpointVolume
        )
        for guid in pinned:
            with self.subTest(guid=guid):
                self.assertIn(f'Guid("{guid}")', audio_mod.CSHARP_SOURCE)
        # The one historical misrecall (tail ...3688E6 instead of ...3617E6)
        # broke activation with E_NOINTERFACE; it must never return.
        for operation in ("mute", "read"):
            with self.subTest(operation=operation):
                inner = audio_mod.render_inner(operation)
                self.assertNotIn("3688E6", inner)
                self.assertNotIn("3688e6", inner)

    def test_build_refuses_an_unknown_operation(self):
        with self.assertRaises(ValueError):
            audio_mod.build("toggle")

    def test_rendered_command_passes_the_powershell_syntax_gate(self):
        if syntax_mod is None:  # pragma: no cover
            self.skipTest("syntax gate module is unavailable")
        for operation in ("mute", "read"):
            with self.subTest(operation=operation):
                try:
                    verdict = syntax_mod.validate(audio_mod.build(operation),
                                                  "powershell")
                except Exception as error:
                    name = type(error).__name__
                    self.assertIn(name, ("ValidatorUnavailable",),
                                  f"unexpected syntax backend failure: {error}")
                    continue
                self.assertTrue(verdict.ok, verdict.message)

    def test_cli_build_writes_only_the_command_file(self):
        root = Path(tempfile.mkdtemp(prefix="lenovo-audio-cli-"))
        self.addCleanup(shutil.rmtree, root, True)
        out = root / "audio-mute.txt"
        proc = subprocess.run(
            [sys.executable, str(AUDIO_CLI), "build",
             "--operation", "mute", "--out", str(out)],
            capture_output=True, text=True, check=False, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertEqual(data["schema"], "dell-lenovo-audio-command/1")
        self.assertEqual(data["operation"], "mute")
        self.assertEqual(data["dialect"], "powershell")
        self.assertEqual(out.read_text(encoding="utf-8"), audio_mod.build("mute"))

    def test_cli_build_refuses_an_unknown_operation(self):
        root = Path(tempfile.mkdtemp(prefix="lenovo-audio-cli-"))
        self.addCleanup(shutil.rmtree, root, True)
        proc = subprocess.run(
            [sys.executable, str(AUDIO_CLI), "build",
             "--operation", "toggle", "--out", str(root / "cmd.txt")],
            capture_output=True, text=True, check=False, timeout=60)
        self.assertEqual(proc.returncode, 2)
        self.assertFalse((root / "cmd.txt").exists())


class ParseTests(unittest.TestCase):
    def test_valid_mute_and_read_receipts_parse(self):
        self.assertTrue(audio_mod.parse_stdout(receipt("mute", True), "mute")["ok"])
        parsed = audio_mod.parse_stdout(receipt("read", False), "read")
        self.assertTrue(parsed["ok"])
        self.assertFalse(parsed["muted"])

    def test_unusable_stdout_is_never_success_and_never_echoes(self):
        marker = "SECRET-MARKER-9f8e7d6c5b4a"
        cases = [None, "", "   ", "not json {", "[1, 2]",
                 json.dumps({"schema": "other/1", "operation": "mute",
                             "ok": True, "muted": True}),
                 json.dumps({"schema": "lenovo-audio-state/1",
                             "operation": "read", "ok": True, "muted": True}),
                 json.dumps({"schema": "lenovo-audio-state/1",
                             "operation": "mute", "ok": "yes", "muted": True}),
                 json.dumps({"schema": "lenovo-audio-state/1",
                             "operation": "mute", "ok": True, "muted": 1}),
                 json.dumps({"schema": "lenovo-audio-state/1",
                             "operation": "mute", "ok": False,
                             "error": "some-new-shape"}),
                 receipt("mute", True) + " trailing-noise",
                 f"prefix-noise {receipt('mute', True)}",
                 receipt("mute", True, ok=False,
                         error=f"audio-operation-failed {marker}")]
        for stdout in cases:
            with self.subTest(stdout=str(stdout)[:40]):
                with self.assertRaises(audio_mod.ReceiptError) as raised:
                    audio_mod.parse_stdout(stdout, "mute")
                self.assertNotIn(marker, str(raised.exception))


class DecideTests(unittest.TestCase):
    def setUp(self) -> None:
        self.key = os.urandom(32)

    def test_verified_mute_and_read_successes(self):
        payload = seal_result(self.key, JOB_ID, receipt("mute", True))
        verdict = audio_mod.decide("mute", payload)
        self.assertEqual(verdict["outcome"], "success")
        self.assertTrue(verdict["confirmed"])
        payload = seal_result(self.key, JOB_ID, receipt("read", False))
        verdict = audio_mod.decide("read", payload)
        self.assertEqual(verdict["outcome"], "success")
        self.assertTrue(verdict["confirmed"])

    def test_mute_is_not_success_until_muted_is_true(self):
        payload = seal_result(self.key, JOB_ID, receipt("mute", False))
        verdict = audio_mod.decide("mute", payload)
        self.assertEqual(verdict["outcome"], "worker_failure")
        self.assertNotEqual(verdict["outcome"], "success")

    def test_lenovo_error_receipt_is_terminal_never_success(self):
        payload = seal_result(self.key, JOB_ID,
                              receipt("mute", ok=False,
                                      error="no-default-playback-device"))
        verdict = audio_mod.decide("mute", payload)
        self.assertEqual(verdict["outcome"], "worker_failure")
        self.assertIn("no-default-playback-device", verdict["detail"])
        self.assertIn("NEW job id", verdict["action"])

    def test_garbage_stdout_is_unknown_receipt_never_success(self):
        payload = seal_result(self.key, JOB_ID, "worker printed noise")
        verdict = audio_mod.decide("read", payload)
        self.assertEqual(verdict["outcome"], "unknown_receipt")
        self.assertTrue(verdict["confirmed"])
        self.assertNotEqual(verdict["outcome"], "success")

    def test_missing_stdout_is_unknown_receipt_never_success(self):
        payload = seal_result(self.key, JOB_ID, "")
        payload = dict(payload)
        del payload["stdout"]
        verdict = audio_mod.decide("mute", payload)
        self.assertEqual(verdict["outcome"], "unknown_receipt")

    def test_transport_failures_are_never_success(self):
        cases = [
            ({"exit_code": 1}, "worker_failure"),
            ({"status": "error"}, "unknown_status"),
            ({"status": "weird"}, "unknown_status"),
            ({"timed_out": True}, "worker_timeout"),
            ({"truncated": True}, "truncated"),
        ]
        for changes, outcome in cases:
            with self.subTest(changes=changes):
                payload = seal_result(self.key, JOB_ID, receipt("mute", True),
                                      **changes)
                verdict = audio_mod.decide("mute", payload)
                self.assertEqual(verdict["outcome"], outcome)
                self.assertNotEqual(verdict["outcome"], "success")

    def test_failed_transport_keeps_the_lenovo_error_token(self):
        payload = seal_result(self.key, JOB_ID,
                              receipt("mute", ok=False,
                                      error="no-default-playback-device"),
                              status="error", exit_code=1)
        verdict = audio_mod.decide("mute", payload)
        self.assertNotEqual(verdict["outcome"], "success")
        self.assertIn("no-default-playback-device", verdict["detail"])

    def test_tampered_envelope_never_reaches_decide(self):
        payload = bridge.build_result_payload(JOB_ID, "ok", 0,
                                              stdout=receipt("mute", True))
        sealed = bytearray(bridge.seal(payload, self.key))
        sealed[-3] ^= 1
        with self.assertRaises(bridge.ProtocolError):
            bridge.open_envelope(bytes(sealed), self.key, "result", JOB_ID)

    def test_decide_rejects_garbage_inputs_fail_closed(self):
        with self.assertRaises(ValueError):
            audio_mod.decide("toggle", {})
        verdict = audio_mod.decide("mute", {"not": "an envelope"})
        self.assertNotEqual(verdict["outcome"], "success")


if __name__ == "__main__":
    unittest.main()
