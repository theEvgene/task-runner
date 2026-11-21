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

    def test_timeout_then_same_id_retrieval_never_resubmits(self):
        first = subprocess.run(self._command("--authorize-lenovo-check"),
                               capture_output=True, text=True, check=False)
        self.assertEqual(first.returncode, 3, first.stderr)
        unknown = json.loads(first.stdout)
        job_id = unknown["jobId"]
        self.assertEqual(len(self._wait_job_ids()), 1)
        bridge.publish(self.queue / "results", job_id,
                       bridge.seal(self._result(job_id), self.key))
        resumed = subprocess.run(self._command("--job-id", job_id),
                                 capture_output=True, text=True, check=False)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(json.loads(resumed.stdout)["jobId"], job_id)
        self.assertEqual(len(self._wait_job_ids()), 1)

    def test_same_id_recovery_rejects_noncanonical_worker_timeout(self):
        for timeout_seconds in (True, 300.0, 0, 3601):
            with self.subTest(timeout_seconds=timeout_seconds):
                job_id = self._publish_completed_job(timeout_seconds)
                resumed = subprocess.run(self._command("--job-id", job_id),
                                         capture_output=True, text=True,
                                         check=False)
                self.assertEqual(resumed.returncode, 2,
                                 resumed.stdout + resumed.stderr)
                self.assertIn("exact selected readiness checklist",
                              resumed.stderr)

    def test_same_id_recovery_accepts_canonical_worker_timeout_bounds(self):
        for timeout_seconds in (1, 3600):
            with self.subTest(timeout_seconds=timeout_seconds):
                job_id = self._publish_completed_job(timeout_seconds)
                resumed = subprocess.run(self._command("--job-id", job_id),
                                         capture_output=True, text=True,
                                         check=False)
                self.assertEqual(resumed.returncode, 0,
                                 resumed.stdout + resumed.stderr)
                self.assertEqual(json.loads(resumed.stdout)["outcome"],
                                 "ready")

    def test_same_id_recovery_rejects_changed_submitted_checklist(self):
        digest = hashlib.sha256(self.marker.read_bytes()).hexdigest()
        self.config["lenovo"].update({
            "components": [{"name": "bridge-client", "type": "file",
                            "path": str(self.marker), "sha256": digest}],
            "tools": [{"name": "python-tool", "command": sys.executable}],
        })
        self._write_config()
        first = subprocess.run(self._command("--authorize-lenovo-check"),
                               capture_output=True, text=True, check=False)
        self.assertEqual(first.returncode, 3, first.stderr)
        job_id = json.loads(first.stdout)["jobId"]
        original = deepcopy(self.config)
        mutations = (
            lambda cfg: cfg["lenovo"]["components"][0].update(
                path=str(self.temp / "other-component.txt")),
            lambda cfg: cfg["lenovo"]["components"][0].update(sha256="f" * 64),
            lambda cfg: cfg["lenovo"]["tools"][0].update(command="other-python"),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.config = deepcopy(original)
                mutate(self.config)
                self._write_config()
                resumed = subprocess.run(self._command("--job-id", job_id),
                                         capture_output=True, text=True, check=False)
                self.assertEqual(resumed.returncode, 2,
                                 resumed.stdout + resumed.stderr)
                self.assertIn("exact selected readiness checklist", resumed.stderr)
                self.assertEqual(len(self._wait_job_ids()), 1)

    def test_dell_lan_needs_separate_corporate_preflight(self):
        self.config.update({
            "scenario": "dell-lan",
            "local": {"components": [{"name": "dell-driver", "type": "file",
                                      "path": str(self.marker)}], "tools": []},
        })
        self._write_config()
        worker = self._worker()
        proc = subprocess.run(self._command("--authorize-lenovo-check"),
                              capture_output=True, text=True, check=False)
        worker.join(2)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        report = json.loads(proc.stdout)
        corporate = next(item for item in report["components"]
                         if item["name"] == "corporate-access")
        self.assertEqual(report["scenario"], "dell-lan")
        self.assertEqual(report["outcome"], "not-ready")
        self.assertEqual(corporate["state"], "unconfirmed")

    def test_legacy_probe_evidence_keeps_readiness_modes_working(self):
        # /1 evidence predates the revision field: readiness presence stays
        # valid, while the selected check refuses it as unverified evidence.
        worker = self._worker(schema="lenovo-readiness-probe/1")
        proc = subprocess.run(self._command("--authorize-lenovo-check"),
                              capture_output=True, text=True, check=False)
        worker.join(2)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["outcome"], "ready")

    def test_legacy_revision_field_is_ignored_not_blocking(self):
        self.config["lenovo"]["revision"] = {
            "name": "task-runner-revision",
            "repository": str(ROOT),
            "expected": "f" * 40,
        }
        self._write_config()
        worker = self._worker()
        proc = subprocess.run(self._command("--authorize-lenovo-check"),
                              capture_output=True, text=True, check=False)
        worker.join(2)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["outcome"], "ready")

    def test_parallel_checks_have_distinct_immutable_jobs(self):
        worker = self._worker(count=2)
        first = subprocess.Popen(self._command("--authorize-lenovo-check"),
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True)
        second = subprocess.Popen(self._command("--authorize-lenovo-check"),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True)
        out1, err1 = first.communicate(timeout=10)
        out2, err2 = second.communicate(timeout=10)
        worker.join(2)
        self.assertEqual((first.returncode, second.returncode), (0, 0), err1 + err2)
        ids = {json.loads(out1)["jobId"], json.loads(out2)["jobId"]}
        self.assertEqual(len(ids), 2)
        self.assertEqual(set(self._wait_job_ids(2)), ids)
        self.assertEqual({path.stem for path in (self.queue / "results").glob("*.json")}, ids)

    def test_uncertain_preflight_reports_one_reason_and_publishes_nothing(self):
        shutil.rmtree(self.queue)
        proc = subprocess.run(self._command("--authorize-lenovo-check"),
                              capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["outcome"], "not-ready")
        self.assertEqual(report["reason"], "queue-missing")
        self.assertTrue(report["nextAction"])
        self.assertFalse(self.queue.exists())  # no empty queue, no signed job
        self.assertNotIn(str(self.temp), proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
