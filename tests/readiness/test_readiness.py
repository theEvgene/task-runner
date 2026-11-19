"""Fast fake-worker tests for the selected LAN readiness command."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "readiness" / "check.py"
BRIDGE_DIR = ROOT / "scripts" / "lan_bridge"
sys.path.insert(0, str(BRIDGE_DIR))
import lan_bridge as bridge  # noqa: E402


def _revision(path: str) -> str | None:
    try:
        value = subprocess.check_output(
            ["git", "-C", path, "rev-parse", "HEAD"],
            text=True, stderr=subprocess.DEVNULL).strip().lower()
    except (OSError, subprocess.CalledProcessError):
        return None
    return value if len(value) == 40 else None


class ReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="readiness-fixture-"))
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.queue = self.temp / "queue"
        self.queue.mkdir()
        self.key_path = self.temp / "fixture-secret-name.key"
        self.key = bytes(range(32))
        self.key_path.write_bytes(self.key)
        self.marker = self.temp / "component.txt"
        self.marker.write_text("fixture", encoding="utf-8")
        self.config_path = self.temp / "config.json"
        self.config = {
            "schema": "lan-readiness-config/1",
            "scenario": "lan-bridge",
            "queue": str(self.queue),
            "keyFile": str(self.key_path),
            "local": {"components": [], "tools": []},
            "lenovo": {
                "python": sys.executable,
                "cwd": str(self.temp),
                "components": [{"name": "bridge-client", "type": "file",
                                "path": str(self.marker)}],
                "tools": [],
            },
        }
        self._write_config()

    def _write_config(self) -> None:
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")

    def _command(self, *extra: str) -> list[str]:
        return [sys.executable, str(SCRIPT), "--config", str(self.config_path),
                "--wait", "0.35", "--poll", "0.03", *extra]

    def _result(self, job_id: str, state: str = "confirmed",
                schema: str = "lenovo-readiness-probe/2") -> dict:
        definitions = {item["name"]: item
                       for item in self.config["lenovo"]["components"]}
        names = list(definitions)
        names.extend(item["name"] for item in self.config["lenovo"]["tools"])
        components = []
        for name in names:
            item = {"name": name, "location": "lenovo", "state": state,
                    "reason": "fixture component result",
                    "nextAction": "none" if state == "confirmed"
                    else f"update {name}"}
            definition = definitions.get(name)
            if schema == "lenovo-readiness-probe/2" \
                    and definition is not None \
                    and definition["type"] == "directory":
                item["observedRevision"] = _revision(definition["path"])
                item["cwdDigest"] = hashlib.sha256(
                    definition["path"].encode("utf-8")).hexdigest()
            components.append(item)
        # Legacy revision pins are ignored by the probe and never expected here.
        body = {"schema": schema, "scenario": self.config["scenario"],
                "components": components}
        return bridge.build_result_payload(
            job_id, "completed", exit_code=0,
            stdout=json.dumps(body, separators=(",", ":")))

    def _wait_job_ids(self, count: int = 1) -> list[str]:
        deadline = time.monotonic() + 5
        jobs = self.queue / "jobs"
        while time.monotonic() < deadline:
            ids = sorted(path.stem for path in jobs.glob("*.json")) if jobs.exists() else []
            if len(ids) >= count:
                return ids
            time.sleep(0.01)
        self.fail("fake worker did not receive the expected jobs")

    def _worker(self, state: str = "confirmed", tamper: bool = False,
                count: int = 1, result_changes: dict | None = None,
                result_removals: tuple[str, ...] = (),
                schema: str = "lenovo-readiness-probe/2") -> threading.Thread:
        def run() -> None:
            for job_id in self._wait_job_ids(count):
                raw = bridge.read(self.queue / "jobs", job_id)
                bridge.open_envelope(raw, self.key, "job", job_id)
                result = self._result(job_id, state, schema=schema)
                result.update(result_changes or {})
                for field in result_removals:
                    result.pop(field, None)
                sealed = bytearray(bridge.seal(result, self.key))
                if tamper:
                    sealed[-3] ^= 1
                bridge.publish(self.queue / "results", job_id, bytes(sealed))

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    def _publish_claim(self, job_id: str) -> None:
        raw = bridge.read(self.queue / "jobs", job_id)
        claim = {
            "schema": "lenovo-job-claim/1", "kind": "claim", "id": job_id,
            "owner": {"instance": "lenovo-worker-1", "pid": 5150},
            "generation": "ab" * 16, "claimed_at": "fixture",
            "resources": ["exclusive:global", "working-directory:c:\\fixture"],
            "job_envelope_sha256": hashlib.sha256(raw).hexdigest(),
            "state": "owned-unknown-until-signed-result",
        }
        bridge.publish(self.queue / "claims", job_id,
                       bridge.seal(claim, self.key))

    def _publish_completed_job(self, timeout_seconds: object) -> str:
        submitted = subprocess.run(
            self._command("--authorize-lenovo-check"), capture_output=True,
            text=True, check=False)
        self.assertEqual(submitted.returncode, 3, submitted.stderr)
        job_id = json.loads(submitted.stdout)["jobId"]
        job_path = self.queue / "jobs" / f"{job_id}.json"
        job = bridge.open_envelope(job_path.read_bytes(), self.key, "job", job_id)
        job_path.unlink()
        job["timeout_seconds"] = timeout_seconds
        bridge.publish(self.queue / "jobs", job_id,
                       bridge.seal(job, self.key))
        bridge.publish(self.queue / "results", job_id,
                       bridge.seal(self._result(job_id), self.key))
        return job_id

    def test_success_is_mac_bound_and_secret_free(self):
        worker = self._worker()
        proc = subprocess.run(self._command("--authorize-lenovo-check"),
                              capture_output=True, text=True, check=False)
        worker.join(2)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["outcome"], "ready")
        self.assertRegex(report["jobId"], r"^[0-9a-f]{32}$")
        self.assertNotIn(str(self.key_path), proc.stdout + proc.stderr)
        self.assertNotIn(self.key.hex(), proc.stdout + proc.stderr)

    def test_stale_component_is_not_success(self):
        worker = self._worker("stale")
        proc = subprocess.run(self._command("--authorize-lenovo-check"),
                              capture_output=True, text=True, check=False)
        worker.join(2)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["outcome"], "not-ready")
        self.assertIn("stale", {item["state"] for item in report["components"]})

    def test_missing_component_is_not_success(self):
        worker = self._worker("missing")
        proc = subprocess.run(self._command("--authorize-lenovo-check"),
                              capture_output=True, text=True, check=False)
        worker.join(2)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["outcome"], "not-ready")
        self.assertIn("missing", {item["state"] for item in report["components"]})

    def test_bad_mac_is_refused(self):
        worker = self._worker(tamper=True)
        proc = subprocess.run(self._command("--authorize-lenovo-check"),
                              capture_output=True, text=True, check=False)
        worker.join(2)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("bad mac", proc.stderr)
        self.assertNotIn(self.key.hex(), proc.stdout + proc.stderr)

    def test_worker_exit_timeout_and_truncation_are_never_ready(self):
        cases = ({"exit_code": 7}, {"timed_out": True},
                 {"output_truncated": True}, {"status": "unknown"})
        for changes in cases:
            with self.subTest(changes=changes):
                shutil.rmtree(self.queue / "jobs", ignore_errors=True)
                shutil.rmtree(self.queue / "results", ignore_errors=True)
                worker = self._worker(result_changes=changes)
                proc = subprocess.run(self._command("--authorize-lenovo-check"),
                                      capture_output=True, text=True, check=False)
                worker.join(2)
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertEqual(json.loads(proc.stdout)["outcome"], "not-ready")

    def test_worker_exit_code_requires_exact_integer(self):
        for exit_code, expected_returncode, expected_outcome in (
                (0.0, 1, "not-ready"),
                (0, 0, "ready")):
            with self.subTest(exit_code=exit_code):
                shutil.rmtree(self.queue / "jobs", ignore_errors=True)
                shutil.rmtree(self.queue / "results", ignore_errors=True)
                worker = self._worker(result_changes={"exit_code": exit_code})
                proc = subprocess.run(self._command("--authorize-lenovo-check"),
                                      capture_output=True, text=True, check=False)
                worker.join(2)
                self.assertEqual(proc.returncode, expected_returncode,
                                 proc.stdout + proc.stderr)
                self.assertEqual(json.loads(proc.stdout)["outcome"],
                                 expected_outcome)

    def test_worker_boolean_fields_are_required_and_exact(self):
        cases = (
            ({}, ("timed_out",)),
            ({}, ("output_truncated",)),
            ({"timed_out": 0}, ()),
            ({"output_truncated": "false"}, ()),
        )
        for changes, removals in cases:
            with self.subTest(changes=changes, removals=removals):
                shutil.rmtree(self.queue / "jobs", ignore_errors=True)
                shutil.rmtree(self.queue / "results", ignore_errors=True)
                worker = self._worker(result_changes=changes,
                                      result_removals=removals)
                proc = subprocess.run(self._command("--authorize-lenovo-check"),
                                      capture_output=True, text=True, check=False)
                worker.join(2)
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertIn("must be a boolean", proc.stderr)

    def test_submission_uses_canonical_cmd_dialect(self):
        proc = subprocess.run(self._command("--authorize-lenovo-check"),
                              capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 3, proc.stderr)
        job_id = json.loads(proc.stdout)["jobId"]
        raw = bridge.read(self.queue / "jobs", job_id)
        job = bridge.open_envelope(raw, self.key, "job", job_id)
        self.assertEqual(job["dialect"], "cmd")

    def test_bounded_wait_embeds_one_meaningful_job_status(self):
        # F1/F5: the timeout report names one job state only, and its status
        # member is the same lan-job-status/1 object, not a second wording.
        first = subprocess.run(self._command("--authorize-lenovo-check"),
                               capture_output=True, text=True, check=False)
        self.assertEqual(first.returncode, 3, first.stderr)
        unknown = json.loads(first.stdout)
        self.assertEqual(unknown["outcome"], "unknown")
        self.assertEqual(unknown["status"]["schema"], "lan-job-status/1")
        self.assertEqual(unknown["outcome"], unknown["status"]["outcome"])
        self.assertEqual(unknown["nextAction"], unknown["status"]["nextAction"])
        job_id = unknown["jobId"]
        self.assertEqual(len(self._wait_job_ids()), 1)
        self._publish_claim(job_id)
        waiting = subprocess.run(self._command("--job-id", job_id),
                                 capture_output=True, text=True, check=False)
        self.assertEqual(waiting.returncode, 3, waiting.stderr)
        report = json.loads(waiting.stdout)
        self.assertEqual(report["outcome"], "waiting")
        self.assertEqual(report["outcome"], report["status"]["outcome"])
        self.assertEqual(report["status"]["schema"], "lan-job-status/1")
        self.assertEqual(report["status"]["jobId"], job_id)
        self.assertTrue(report["status"]["occupied"])
        self.assertEqual(report["nextAction"], report["status"]["nextAction"])
        self.assertIn("never resubmit", report["nextAction"])
        self.assertNotIn("running", json.dumps(report["status"]).lower())
        self.assertNotIn(self.key.hex(), waiting.stdout + waiting.stderr)
        # Same-ID recovery never publishes a second job or releases the lease.
        self.assertEqual(len(self._wait_job_ids()), 1)

    def test_unverifiable_request_is_refused_at_exit_two(self):
        # F4: the refused arm reuses the one job status; a request failing its
        # MAC binding is exit 2, never a waiting or unknown guess.
        pending = subprocess.Popen(self._command("--authorize-lenovo-check"),
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        job_id = self._wait_job_ids()[0]
        path = self.queue / "jobs" / f"{job_id}.json"
        sealed = bytearray(path.read_bytes())
        sealed[-3] ^= 1
        path.write_bytes(bytes(sealed))
        out, err = pending.communicate(timeout=10)
        self.assertEqual(pending.returncode, 2, out + err)
        report = json.loads(out)
        self.assertEqual(report["outcome"], "refused")
        self.assertEqual(report["outcome"], report["status"]["outcome"])
        self.assertEqual(report["nextAction"], report["status"]["nextAction"])
        self.assertEqual(len(self._wait_job_ids()), 1)



if __name__ == "__main__":
    unittest.main()


