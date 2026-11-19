"""Fast unit regression for the canonical mirror wrapper (live defect).

The signed mirror request rendered a direct python invocation, which
the Lenovo worker refuses (``executor refused: powershell launcher
refused``); the worker accepts the canonical PowerShell wrapper. The
signed exact-command verifier must parse and bind that same wrapper
without weakening the request digest, token, or driver identity, and
Windows backslash/UNC paths must survive byte-identical (POSIX
``shlex`` mangles them into different paths). Covers both the open
and merged drivers, tamper cases, legacy same-ID resumes, and the
``--expected-cwd`` binding. No network, no git, no queue, no live
writes: the request CLI runs against temp dirs and the verifier runs
against a stub bridge.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parents[1]
SCRIPTS = ROOT / "scripts" / "mirror_mr"
DELL_LAN = ROOT / "scripts" / "dell_lan"

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(DELL_LAN))

from mirror_mr_common import Stop, parse_mirror_command  # noqa: E402

TOKEN = "0123456789abcdef"
DIGEST = "ab" * 32


def run_request(*extra: str, shared: Path, root: Path,
                python: str, checkout: str, mode: str = "open") -> dict:
    mapping = root / "mapping.json"
    mapping.write_text(json.dumps({
        "version": "mirror-mapping/1",
        "mirrors": {"gitlab.fixture.invalid/group/project": "owner/private-mirror"},
    }), encoding="utf-8")
    command_file = root / f"cmd-{mode}.txt"
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "dell_mirror_request.py"),
         "--mr-url", "https://gitlab.fixture.invalid/group/project/-/merge_requests/7",
         "--mapping-file", str(mapping), "--token", TOKEN,
         "--shared", str(shared), "--lenovo-checkout", checkout,
         "--python", python, "--mode", mode,
         "--request-name", f"mirror-request-{TOKEN}.json",
         "--command-file", str(command_file)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False)
    if proc.returncode != 0:
        raise AssertionError(f"request refused: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def make_wrapper(python: str, driver: str, request_path: str,
                 digest: str = DIGEST, flow_dir: str | None = None) -> str:
    from shell_command import build_for_transport
    parts = [python, driver, "--request", request_path,
             "--request-sha256", digest]
    if flow_dir is not None:
        parts += ["--flow-dir", flow_dir]
    return build_for_transport(parts, "powershell")


class StubBridge:
    JobNotFoundError = type("JobNotFoundError", (Exception,), {})
    BridgeError = type("BridgeError", (Exception,), {})
    ProtocolError = type("ProtocolError", (Exception,), {})

    def __init__(self, job: dict):
        self.job = job

    def read(self, _path: str, _job_id: str) -> bytes:
        return b"stub-job-bytes"

    def open_envelope(self, _raw: bytes, _key: bytes,
                      _kind: str, _job_id: str) -> dict:
        return dict(self.job)


def verify_open(command: str, expected_cwd: str | None = None,
                working_dir: str | None = None) -> dict:
    import dell_mirror_import as open_import
    job = {"command": command,
           "working_directory": working_dir or r"C:\lenovo\checkout"}
    if expected_cwd is not None:
        job["expected_working_directory"] = expected_cwd
    return open_import.verify_mirror_job(
        StubBridge(job), Path(r"C:\queue"), b"0" * 32, "f" * 32)


class CanonicalWrapperTests(unittest.TestCase):
    def test_open_request_renders_canonical_wrapper(self) -> None:
        """The open request seals the worker-accepted launcher, bound digest."""

        with tempfile.TemporaryDirectory(prefix="mirror-canonical-") as tmp:
            root, shared = Path(tmp), Path(tmp) / "share"
            shared.mkdir()
            checkout = str(root / "lenovo-checkout")
            data = run_request(shared=shared, root=root,
                               python=sys.executable, checkout=checkout)
            command = data["command"]
            self.assertTrue(
                command.startswith(
                    'powershell.exe -NoProfile -NonInteractive -Command "& \''),
                command)
            self.assertIn("lenovo_mr_export.py", command)
            self.assertNotIn("lenovo_mr_history_export.py", command)
            self.assertEqual(data["dialect"], "powershell")
            self.assertEqual(data["expectedCwd"], checkout)
            raw = (shared / f"mirror-request-{TOKEN}.json").read_bytes()
            import hashlib
            self.assertIn(hashlib.sha256(raw).hexdigest(), command)
            parsed = parse_mirror_command(command, "lenovo_mr_export.py")
            self.assertEqual(parsed["requestToken"], TOKEN)
            self.assertEqual(parsed["digest"], hashlib.sha256(raw).hexdigest())

    def test_merged_request_renders_history_wrapper(self) -> None:
        """The merged mode binds the history driver, never the open one."""

        with tempfile.TemporaryDirectory(prefix="mirror-canonical-") as tmp:
            root, shared = Path(tmp), Path(tmp) / "share"
            shared.mkdir()
            checkout = str(root / "lenovo-checkout")
            data = run_request(shared=shared, root=root,
                               python=sys.executable, checkout=checkout,
                               mode="merged")
            self.assertIn("lenovo_mr_history_export.py", data["command"])
            parsed = parse_mirror_command(
                data["command"], "lenovo_mr_history_export.py")
            self.assertEqual(parsed["requestToken"], TOKEN)
            with self.assertRaises(Stop):
                parse_mirror_command(data["command"], "lenovo_mr_export.py")

    def test_windows_paths_and_unc_survive_byte_identical(self) -> None:
        """Drive-letter, spaced, quoted, and UNC bytes are preserved, bound."""

        python = r"C:\Lenovo Tools\py\python.exe"
        driver = r"C:\Lenovo Checkout\drivers\lenovo_mr_export.py"
        request_path = r"\\SERVER\lan share\mirror-request-0123456789abcdef.json"
        flow_dir = r"\\SERVER\lan share\flow dir"
        command = make_wrapper(python, driver, request_path, flow_dir=flow_dir)
        parsed = parse_mirror_command(command, "lenovo_mr_export.py")
        self.assertEqual(parsed["python"], python)
        self.assertEqual(parsed["driver"], driver)
        self.assertEqual(parsed["requestPath"], request_path)
        self.assertEqual(parsed["flowDir"], flow_dir)
        self.assertEqual(parsed["requestToken"], TOKEN)
        # POSIX shlex is the documented hazard: it collapses the UNC
        # leading pair, so the verifier must never use it for these
        # bytes while this parser preserves them byte-identical.
        mangled = shlex.split(command, posix=True)
        self.assertNotIn(r"\\SERVER", " ".join(mangled))
        self.assertIn(r"\\SERVER", request_path)

    def test_single_quote_in_path_survives_doubled(self) -> None:
        """An apostrophe doubles inside the wrapper and parses back singly."""

        python = r"C:\Lenovo's\python.exe"
        driver = r"C:\drv\lenovo_mr_export.py"
        request_path = r"C:\share\mirror-request-0123456789abcdef.json"
        parsed = parse_mirror_command(
            make_wrapper(python, driver, request_path),
            "lenovo_mr_export.py")
        self.assertEqual(parsed["python"], python)

    def test_tamper_refuses_without_weakening_identity(self) -> None:
        """Digest, token, driver, shape, and operator tamper all refuse."""

        good = make_wrapper(r"C:\py\python.exe",
                            r"C:\drv\lenovo_mr_export.py",
                            r"C:\share\mirror-request-0123456789abcdef.json")
        cases = {
            "flipped-digest": good[:-1] + ("0" if good[-1] != "0" else "1"),
            "non-hex-token-name": make_wrapper(
                r"C:\py\python.exe", r"C:\drv\lenovo_mr_export.py",
                r"C:\share\mirror-request-0123456789abcdeg.json"),
            "driver-swap": make_wrapper(
                r"C:\py\python.exe", r"C:\drv\lenovo_mr_history_export.py",
                r"C:\share\mirror-request-0123456789abcdef.json"),
            "extra-arg": make_wrapper(
                r"C:\py\python.exe", r"C:\drv\lenovo_mr_export.py",
                r"C:\share\mirror-request-0123456789abcdef.json") + "x",
            "operator-injection": good + " & echo pwned",
            "relative-driver": make_wrapper(
                r"C:\py\python.exe", r"drv\lenovo_mr_export.py",
                r"C:\share\mirror-request-0123456789abcdef.json"),
            "empty": "",
        }
        for label, command in cases.items():
            with self.subTest(tamper=label):
                with self.assertRaises(Stop) as ctx:
                    parse_mirror_command(command, "lenovo_mr_export.py")
                self.assertEqual(ctx.exception.reason, "queue-job-mismatch")
        # A valid-but-different token still parses with ITS name intact,
        # so the payload check binds a mismatch downstream as
        # request-malformed instead of silently adopting it.
        other = parse_mirror_command(make_wrapper(
            r"C:\py\python.exe", r"C:\drv\lenovo_mr_export.py",
            r"C:\share\mirror-request-ffffffffffffffff.json"),
            "lenovo_mr_export.py")
        self.assertEqual(other["requestToken"], "ffffffffffffffff")

    def test_legacy_direct_form_still_verifies_same_id(self) -> None:
        """Jobs sealed before the wrapper fix resume under the same id."""

        from dell_lan_common import command_line
        direct = command_line(
            [r"C:\py\python.exe", r"C:\drv\lenovo_mr_export.py",
             "--request", r"C:\share\mirror-request-0123456789abcdef.json",
             "--request-sha256", DIGEST])
        parsed = parse_mirror_command(direct, "lenovo_mr_export.py")
        self.assertEqual(parsed["requestToken"], TOKEN)
        self.assertEqual(parsed["digest"], DIGEST)

    def test_expected_cwd_binding(self) -> None:
        """A provable-but-wrong target refuses; an absent one stays bound."""

        command = make_wrapper(r"C:\py\python.exe",
                               r"C:\drv\lenovo_mr_export.py",
                               r"C:\share\mirror-request-0123456789abcdef.json")
        good = verify_open(command, expected_cwd=r"C:\lenovo\checkout",
                           working_dir=r"C:\lenovo\checkout")
        self.assertEqual(good["_requestFileToken"], TOKEN)
        cased = verify_open(command, expected_cwd=r"c:/LENOVO/checkout",
                            working_dir=r"C:\lenovo\checkout")
        self.assertEqual(cased["_requestFileToken"], TOKEN)
        with self.assertRaises(Stop):
            verify_open(command, expected_cwd=r"C:\other\checkout",
                        working_dir=r"C:\lenovo\checkout")
        legacy = verify_open(command)
        self.assertEqual(legacy["_requestFileToken"], TOKEN)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
