"""Fast fixture tests for the installer-environment preflight (issue #160).

A present shell is not proof that an installer command resolves in its
process (PAT rollout: PowerShell present, ``Get-FileHash`` absent until
the process imported its module). These tests prove the extended Lenovo
probe checks commands in that effective context and runtimes against an
owner minimum, and that the Dell gate turns every non-confirmed entry
into ``not-ready`` without running a dependent install. No host, share,
key leak, network, or corporate access; one bounded fake signed queue.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
READINESS_DIR = ROOT / "scripts" / "readiness"
BRIDGE_DIR = ROOT / "scripts" / "lan_bridge"
sys.path.insert(0, str(READINESS_DIR))
sys.path.insert(0, str(BRIDGE_DIR))

import check  # noqa: E402
import installer_env  # noqa: E402
import lan_bridge as bridge  # noqa: E402
import remote_probe  # noqa: E402

PY = sys.executable

FAKE_SHELL = """import os, sys
argv = sys.argv[1:]
code = argv[argv.index("-Command") + 1] if "-Command" in argv else ""
mode = os.environ.get("FAKE_INSTALLER_SHELL", "all-present")
if mode == "all-present":
    sys.exit(0)
if mode == "import-fixes":
    sys.exit(0 if ("Import-Module" in code or "Get-Module" in code) else 1)
if mode == "import-broken":
    sys.exit(0 if ("Get-Module" in code and "Import-Module" not in code) else 1)
sys.exit(1)
"""


def _probe_body(config: dict, env_extra: dict | None = None) -> dict:
    data = base64.b64encode(json.dumps(config).encode("utf-8")).decode("ascii")
    runtime = dict(os.environ)
    runtime.update(env_extra or {})
    proc = subprocess.run(
        [PY, "-c", remote_probe.PROGRAM, data],
        capture_output=True, text=True, timeout=60, env=runtime)
    if proc.returncode != 0:
        raise AssertionError(f"probe crashed: {proc.stderr}")
    return json.loads(proc.stdout)


def _env_config(shell) -> dict:
    return {"scenario": "lan-bridge", "components": [], "tools": [],
            "environment": shell}


class ProbeProgramTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="installer-env-"))
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.shell = self.temp / "fake_shell.py"
        self.shell.write_text(FAKE_SHELL, encoding="utf-8")
        self.not_executable = self.temp / "plain.txt"
        self.not_executable.write_text("no version here", encoding="utf-8")

    def _run(self, environment: dict, extra: dict | None = None) -> dict:
        body = _probe_body(_env_config(environment), extra)
        self.assertEqual(body["schema"], "lenovo-readiness-probe/2")
        return {item["name"]: item for item in body["components"]}

    def test_runtime_matrix_and_secret_free_output(self):
        found = self._run({"runtimes": [
            {"name": "py-ok", "command": PY, "minimum": "1.0"},
            {"name": "py-old", "command": PY, "minimum": "99.0"},
            {"name": "py-any", "command": PY},
            {"name": "gone", "command": "definitely-absent-runtime-xyz"},
            {"name": "opaque", "command": str(self.not_executable)},
        ]})
        self.assertEqual(
            [(found[name]["state"], found[name]["detail"]) for name in
             ("py-ok", "py-old", "py-any", "gone", "opaque")],
            [("confirmed", "meets-minimum"), ("stale", "below-minimum"),
             ("confirmed", "no-requirement"), ("missing", "absent"),
             ("unconfirmed", "unreadable")])
        for item in found.values():
            self.assertEqual(item["kind"], "runtime")
            self.assertEqual(set(item),
                             {"name", "location", "state", "reason",
                              "nextAction", "kind", "detail"})

    def test_command_contexts_through_fake_installer_shell(self):
        shell = [PY, str(self.shell)]
        cases = (
            ("all-present",
             [{"name": "Get-FileHash", "module": "Microsoft.PowerShell.Utility"}],
             ("confirmed", "available")),
            ("import-fixes",
             [{"name": "Get-FileHash", "module": "Microsoft.PowerShell.Utility"}],
             ("missing", "module-available")),
            ("module-missing",
             [{"name": "Get-FileHash", "module": "Microsoft.PowerShell.Utility"}],
             ("missing", "module-missing")),
            ("module-missing", [{"name": "Get-FileHash"}],
             ("missing", "command-absent")),
        )
        for mode, commands, expected in cases:
            with self.subTest(mode=mode, commands=commands):
                found = self._run(
                    {"shell": shell, "commands": commands},
                    {"FAKE_INSTALLER_SHELL": mode})
                item = found["Get-FileHash"]
                self.assertEqual((item["state"], item["detail"]), expected)
                self.assertEqual(item["kind"], "command")

    def test_missing_shell_is_unknown_without_probe(self):
        empty = self.temp / "empty-path"
        empty.mkdir()
        found = self._run(
            {"commands": [{"name": "Get-FileHash",
                           "module": "Microsoft.PowerShell.Utility"}]},
            {"PATH": str(empty)})
        item = found["Get-FileHash"]
        self.assertEqual((item["state"], item["detail"]),
                         ("unconfirmed", "shell-unavailable"))

    def _observed(self, text: str):
        program = remote_probe.PROGRAM
        block = program[program.index("VERSION_RE="):
                        program.index('for item in env.get("commands")')]
        namespace: dict = {}
        exec("import re\n" + block, namespace)
        return namespace["parse_observed_version"](text)

    def test_bare_year_does_not_mask_old_dotted_version(self):
        self.assertEqual(self._observed("Copyright 2024\nv18.0.0\n"),
                         (18, 0, 0))
        self.assertLess(self._observed("Copyright 2024\nv18.0.0\n"),
                        (20, 0, 0))

    def test_bare_year_only_is_unknown(self):
        self.assertIsNone(self._observed("Copyright 2024\nAll rights reserved\n"))

    def test_ambiguous_versions_are_unknown(self):
        self.assertIsNone(self._observed("20.0.0\n21.0.0\n"))

    def test_failed_import_is_not_module_available(self):
        shell = [PY, str(self.shell)]
        found = self._run(
            {"shell": shell, "commands": [
                {"name": "Get-FileHash",
                 "module": "Microsoft.PowerShell.Utility"}]},
            {"FAKE_INSTALLER_SHELL": "import-broken"})
        item = found["Get-FileHash"]
        self.assertEqual((item["state"], item["detail"]),
                         ("missing", "command-absent"))


class ConfigValidationTests(unittest.TestCase):
    def _base(self) -> dict:
        return {"schema": "lan-readiness-config/1", "scenario": "lan-bridge",
                "queue": "q", "keyFile": "k",
                "local": {"components": [], "tools": [{"name": "git",
                                                       "command": "git"}]},
                "lenovo": {"python": "python", "cwd": "cwd",
                           "components": [], "tools": []}}

    def test_accepts_full_environment(self):
        raw = self._base()
        raw["lenovo"]["environment"] = {
            "shell": "powershell",
            "commands": [{"name": "Get-FileHash",
                          "module": "Microsoft.PowerShell.Utility"}],
            "runtimes": [{"name": "node", "command": "node",
                          "minimum": "20.0.0"}]}
        config = check.validate_config(raw)
        self.assertEqual(config["lenovo"]["environment"]["shell"], "powershell")

    def test_refuses_bad_shapes(self):
        bad_envs = (
            {"commands": [{"name": "Bad Name!"}]},
            {"commands": [{"name": "Get-FileHash", "module": "a;b"}]},
            {"commands": [{"name": "Get-FileHash", "extra": 1}]},
            {"runtimes": [{"name": "node", "command": "node",
                           "minimum": "soon"}]},
            {"runtimes": [{"name": "node"}]},
            {"shell": []},
            {"unknown": []},
            ["commands"],
        )
        for env in bad_envs:
            with self.subTest(env=env):
                raw = self._base()
                raw["lenovo"]["environment"] = env
                with self.assertRaises(ValueError):
                    check.validate_config(raw)

    def test_refuses_duplicate_names_across_scopes(self):
        raw = self._base()
        raw["lenovo"]["tools"] = [{"name": "node", "command": "node"}]
        raw["lenovo"]["environment"] = {
            "runtimes": [{"name": "node", "command": "node"}]}
        with self.assertRaises(check.Refusal):
            check.validate_config(raw)

    def test_absent_environment_stays_absent(self):
        self.assertNotIn("environment",
                         check.validate_config(self._base())["lenovo"])
        self.assertEqual(installer_env.all_names(None), [])


class GateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="installer-gate-"))
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.queue = self.temp / "queue"
        self.queue.mkdir()
        self.key_path = self.temp / "k.key"
        self.key = bytes(range(32))
        self.key_path.write_bytes(self.key)
        self.config_path = self.temp / "config.json"
        self.config = {
            "schema": "lan-readiness-config/1", "scenario": "lan-bridge",
            "queue": str(self.queue), "keyFile": str(self.key_path),
            "local": {"components": [], "tools": []},
            "lenovo": {
                "python": PY, "cwd": str(self.temp),
                "components": [], "tools": [],
                "environment": {
                    "commands": [{"name": "Get-FileHash",
                                  "module": "Microsoft.PowerShell.Utility"}],
                    "runtimes": [{"name": "node", "command": "node",
                                  "minimum": "20.0"}],
                },
            },
        }
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")

    def _command(self, *extra: str) -> list[str]:
        return [PY, str(ROOT / "scripts" / "readiness" / "check.py"),
                "--config", str(self.config_path),
                "--wait", "0.35", "--poll", "0.03", *extra]

    def _env_item(self, name: str, state: str,
                  kind: str, detail: str) -> dict:
        return {"name": name, "location": "lenovo", "state": state,
                "reason": "fixture worker text is never relayed",
                "nextAction": "fixture worker text is never relayed",
                "kind": kind, "detail": detail}

    def _result(self, job_id: str, env: list[dict]) -> dict:
        body = {"schema": "lenovo-readiness-probe/2",
                "scenario": "lan-bridge", "components": env}
        return bridge.build_result_payload(
            job_id, "completed", exit_code=0,
            stdout=json.dumps(body, separators=(",", ":")))

    def _wait_job_ids(self) -> list[str]:
        deadline = time.monotonic() + 5
        jobs = self.queue / "jobs"
        while time.monotonic() < deadline:
            ids = sorted(p.stem for p in jobs.glob("*.json")) if jobs.exists() else []
            if ids:
                return ids
            time.sleep(0.01)
        self.fail("fake worker received no job")

    def _worker(self, env: list[dict]) -> threading.Thread:
        def run() -> None:
            for job_id in self._wait_job_ids():
                raw = bridge.read(self.queue / "jobs", job_id)
                bridge.open_envelope(raw, self.key, "job", job_id)
                bridge.publish(self.queue / "results", job_id,
                               bridge.seal(self._result(job_id, env), self.key))

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    def _job_count(self) -> int:
        jobs = self.queue / "jobs"
        return len(list(jobs.glob("*.json"))) if jobs.exists() else 0

    def test_module_available_case_names_process_import_fix(self):
        worker = self._worker([
            self._env_item("Get-FileHash", "missing", "command",
                           "module-available"),
            self._env_item("node", "confirmed", "runtime", "meets-minimum")])
        proc = subprocess.run(self._command("--authorize-lenovo-check"),
                              capture_output=True, text=True, check=False)
        worker.join(2)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["outcome"], "not-ready")
        failing = next(item for item in report["components"]
                       if item["name"] == "Get-FileHash")
        self.assertIn("import", failing["nextAction"])
        self.assertIn("Microsoft.PowerShell.Utility", failing["nextAction"])
        self.assertNotIn("never relayed", proc.stdout)
        self.assertEqual(self._job_count(), 1)

    def test_stale_runtime_and_unknown_shell_are_not_ready(self):
        for env, needle in (
                ([self._env_item("Get-FileHash", "confirmed", "command",
                                 "available"),
                  self._env_item("node", "stale", "runtime", "below-minimum")],
                 "20.0"),
                ([self._env_item("Get-FileHash", "unconfirmed", "command",
                                 "shell-unavailable"),
                  self._env_item("node", "confirmed", "runtime",
                                 "meets-minimum")],
                 "installer shell")):
            with self.subTest(env=env):
                shutil.rmtree(self.queue / "jobs", ignore_errors=True)
                shutil.rmtree(self.queue / "results", ignore_errors=True)
                worker = self._worker(env)
                proc = subprocess.run(
                    self._command("--authorize-lenovo-check"),
                    capture_output=True, text=True, check=False)
                worker.join(2)
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertEqual(json.loads(proc.stdout)["outcome"], "not-ready")
                self.assertIn(needle, proc.stdout)
                self.assertEqual(self._job_count(), 1)

    def test_confirmed_environment_is_ready_and_secret_free(self):
        worker = self._worker([
            self._env_item("Get-FileHash", "confirmed", "command", "available"),
            self._env_item("node", "confirmed", "runtime", "meets-minimum")])
        proc = subprocess.run(self._command("--authorize-lenovo-check"),
                              capture_output=True, text=True, check=False)
        worker.join(2)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["outcome"], "ready")
        self.assertNotIn(self.key.hex(), proc.stdout + proc.stderr)

    def test_environment_change_breaks_same_id_checklist(self):
        proc = subprocess.run(self._command("--authorize-lenovo-check"),
                              capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 3, proc.stderr)
        job_id = json.loads(proc.stdout)["jobId"]
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        del config["lenovo"]["environment"]
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        retry = subprocess.run(self._command("--job-id", job_id),
                               capture_output=True, text=True, check=False)
        self.assertEqual(retry.returncode, 2, retry.stdout + retry.stderr)
        self.assertIn("checklist", retry.stderr)
        self.assertEqual(self._job_count(), 1)


if __name__ == "__main__":
    unittest.main()
