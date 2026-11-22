"""Producer timeout contract: every job producer stays within 1..600 (#168).

Regression for the review blocker: the MR Ready default (1800) failed at
submit time, and the release / pipeline-observer producers accepted values
above 600 only to fail later. Managed submit defaulted to 900. This suite
proves, without any ten-minute wait, that each producer default submits
through the client contract and that 601+ is refused at build time while
smaller timeouts pass through unchanged.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "lan_bridge"))
sys.path.insert(0, str(ROOT / "scripts" / "dell_lan"))

import job_payload as payload  # noqa: E402


def _load(name: str, relative: str):
    path = ROOT / "scripts" / relative
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mr_request = _load("mr_ready_request", "mr_ready/request.py")
release_request = _load("release_request", "release/request.py")
observer_request = _load("observer_request", "pipeline_observer/request.py")
managed_submit = _load("dell_managed_submit", "dell_lan/dell_managed_submit.py")

MR_URL = "https://gitlab.example/team/app/-/merge_requests/7"
SHA = "1" * 40
PY = "C:\\Python\\python.exe"
CLIENT_DIR = "C:\\flow\\gitlab-mr-flow\\scripts"
CHECKOUT = "C:\\repo"
NODE = "C:/lenovo/node.exe"
ENTRY = "C:/lenovo/release-tag.mjs"
JOB_ID = "c" * 32


def _submit(timeout_seconds: int) -> dict:
    return payload.build_job_payload(
        job_id=JOB_ID, command="echo hi",
        working_directory="C:\\work", timeout_seconds=timeout_seconds)


class MrReadyTimeoutTests(unittest.TestCase):
    def _build(self, **overrides):
        params = {
            "mr_url": MR_URL, "python": PY, "client_dir": CLIENT_DIR,
            "checkout": CHECKOUT, "change_type": "feat", "key": "ABC-1",
            "subject": "Short subject", "scopes": ["src/**"],
        }
        params.update(overrides)
        return mr_request.build_request(**params)

    def test_default_submits_through_client_contract(self) -> None:
        built, _ = self._build()
        self.assertLessEqual(built["job"]["timeout_seconds"], 600)
        sealed = _submit(built["job"]["timeout_seconds"])
        self.assertEqual(sealed["timeout_seconds"],
                         built["job"]["timeout_seconds"])

    def test_boundaries_599_600_601(self) -> None:
        for good in (1, 45, 599, 600):
            built, _ = self._build(timeout_seconds=good)
            self.assertEqual(built["job"]["timeout_seconds"], good)
        for bad in (0, 601, 1800, 3600):
            with self.assertRaises(Exception, msg=repr(bad)):
                self._build(timeout_seconds=bad)


class ReleaseTimeoutTests(unittest.TestCase):
    def _build(self, **overrides):
        params = {"node": NODE, "entrypoint": ENTRY, "checkout": CHECKOUT}
        params.update(overrides)
        return release_request.build_request(**params)

    def test_default_submits_through_client_contract(self) -> None:
        req, _ = self._build()
        self.assertLessEqual(req["job"]["timeout_seconds"], 600)
        sealed = _submit(req["job"]["timeout_seconds"])
        self.assertEqual(sealed["timeout_seconds"],
                         req["job"]["timeout_seconds"])

    def test_boundaries_599_600_601(self) -> None:
        for good in (1, 45, 599, 600):
            req, _ = self._build(timeout_seconds=good)
            self.assertEqual(req["job"]["timeout_seconds"], good)
        for bad in (0, 601, 3600):
            with self.assertRaises(Exception, msg=repr(bad)):
                self._build(timeout_seconds=bad)


class PipelineObserverTimeoutTests(unittest.TestCase):
    def _build(self, **overrides):
        params = {"mr_url": MR_URL, "expected_sha": SHA,
                  "profile": "C:\\prof.json", "python": PY,
                  "flow_dir": "C:\\flow", "checkout": CHECKOUT}
        params.update(overrides)
        return observer_request.build_request(**params)

    def test_default_submits_through_client_contract(self) -> None:
        req, _ = self._build()
        self.assertLessEqual(req["job"]["timeout_seconds"], 600)
        sealed = _submit(req["job"]["timeout_seconds"])
        self.assertEqual(sealed["timeout_seconds"],
                         req["job"]["timeout_seconds"])

    def test_boundaries_599_600_601(self) -> None:
        for good in (1, 45, 599, 600):
            req, _ = self._build(timeout_seconds=good)
            self.assertEqual(req["job"]["timeout_seconds"], good)
        for bad in (0, 601, 3600):
            with self.assertRaises(Exception, msg=repr(bad)):
                self._build(timeout_seconds=bad)


class ManagedSubmitTimeoutTests(unittest.TestCase):
    def test_defaults_submit_through_client_contract(self) -> None:
        self.assertEqual(
            managed_submit.submit_managed.__kwdefaults__["timeout_seconds"],
            600)
        parser = managed_submit.build_parser()
        self.assertEqual(parser.get_default("timeout"), 600)
        sealed = _submit(600)
        self.assertEqual(sealed["timeout_seconds"], 600)

    def test_legacy_default_above_cap_is_not_submittable(self) -> None:
        with self.assertRaises(ValueError):
            _submit(900)


if __name__ == "__main__":
    unittest.main()
