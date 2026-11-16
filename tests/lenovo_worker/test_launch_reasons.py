"""Fixture-only tests for the shared launch/readiness reason classifier."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "lenovo_worker"))

import reasons  # noqa: E402


class ClassifyTests(unittest.TestCase):
    READY = {"shareStatus": "MAPPED", "shareVisible": True,
             "queuePresent": True, "workerPresent": True}

    def test_only_supported_observations_conclude_a_reason(self) -> None:
        cases = (
            ("auth refused", {"shareStatus": "AUTH-DENIED", "shareVisible": None},
             "smb-auth-refused", "refused"),
            ("credential file", {"shareStatus": "BAD-CREDENTIAL-FILE"},
             "smb-credential-file-invalid", "refused"),
            ("network unreachable", {"shareStatus": "MAP-FAILED", "shareVisible": False,
                                     "networkReachable": False},
             "smb-network-unreachable", "refused"),
            ("share unavailable", {"shareStatus": "MAP-FAILED", "shareVisible": False,
                                   "networkReachable": True},
             "smb-share-unavailable", "refused"),
            ("generic map failure", {"shareStatus": "MAP-FAILED", "shareVisible": False},
             "smb-share-unconfirmed", "refused"),
            ("mapped but absent", {"shareStatus": "MAPPED", "shareVisible": False},
             reasons.UNKNOWN, "unknown"),
            ("auth while visible", {"shareStatus": "AUTH-DENIED", "shareVisible": True},
             reasons.UNKNOWN, "unknown"),
            ("no observation", {}, reasons.UNKNOWN, "unknown"),
            ("queue missing", {"shareStatus": "MAPPED", "shareVisible": True,
                               "queuePresent": False}, "queue-missing", "refused"),
            ("worker missing", {"shareStatus": "MAPPED", "shareVisible": True,
                                "queuePresent": True, "workerPresent": False},
             "worker-missing", "refused"),
            ("launcher cwd unc", {"cwd": "unc"}, "launch-cwd-remote", "refused"),
            ("worker cwd network drive", {"cwd": "local", "workerCwd": "network-drive"},
             "launch-cwd-remote", "refused"),
            ("relative cwd", {"cwd": "relative"}, "launch-cwd-relative", "refused"),
            ("unprovable cwd", {"cwd": "unverifiable"},
             "launch-cwd-unverifiable", "refused"),
        )
        for label, observations, code, state in cases:
            with self.subTest(label=label):
                verdict = reasons.classify(observations)
                self.assertIsNotNone(verdict, observations)
                self.assertEqual(verdict.reason, code)
                self.assertEqual(reasons.describe(verdict)["state"], state)

    def test_confirmed_observations_are_not_a_refusal(self) -> None:
        self.assertIsNone(reasons.classify(self.READY))
        self.assertIsNone(reasons.classify(dict(self.READY, cwd="local")))

    def test_generic_mapping_failure_is_never_bad_credentials(self) -> None:
        verdict = reasons.classify({"shareStatus": "MAP-FAILED",
                                    "shareVisible": False,
                                    "shareDetail": "The network path was not found"})
        self.assertEqual(verdict.reason, "smb-share-unconfirmed")
        self.assertIn("not proof of bad credentials",
                      reasons.describe(verdict)["nextAction"])

    def test_local_classification_uses_only_local_facts(self) -> None:
        self.assertEqual(reasons.classify_local({"queuePresent": False}).reason,
                         "queue-missing")
        self.assertEqual(reasons.classify_local({"keyPresent": False}).reason,
                         "key-unusable")
        self.assertEqual(reasons.classify_local({"toolPresent": False}).reason,
                         "tool-missing")
        self.assertEqual(reasons.classify_local({"componentPresent": False}).reason,
                         "component-unconfirmed")
        self.assertIsNone(reasons.classify_local({"queuePresent": True,
                                                  "keyPresent": True,
                                                  "toolPresent": True,
                                                  "componentPresent": True}))


class ShareTokenTests(unittest.TestCase):
    def test_probe_tokens_are_normalized(self) -> None:
        cases = (("ALREADY:\\\\HOST\\share", 0, "MAPPED"),
                 ("MAPPED:\\\\HOST\\share", 0, "MAPPED"),
                 ("AUTH-DENIED", 10, "AUTH-DENIED"),
                 ("BAD-CREDENTIAL-FILE", 12, "BAD-CREDENTIAL-FILE"),
                 ("MAP-FAILED:The network path was not found", 11, "MAP-FAILED"),
                 ("something-else", 1, "UNOBSERVED"),
                 ("", None, "UNOBSERVED"))
        for token, exit_code, expected in cases:
            with self.subTest(token=token, exit_code=exit_code):
                self.assertEqual(reasons.share_status(token, exit_code), expected)


class SanitizeTests(unittest.TestCase):
    SECRET = "fixture-secret-pw-9z"

    def test_cwd_safety_reads_real_path_semantics(self) -> None:
        self.assertEqual(reasons.cwd_safety("\\\\HOST\\share\\dir"), "unc")
        self.assertEqual(reasons.cwd_safety("//HOST/share"), "unc")
        self.assertEqual(reasons.cwd_safety("\\\\?\\UNC\\HOST\\share"), "unc")
        self.assertEqual(reasons.cwd_safety("relative\\dir"), "relative")
        self.assertEqual(reasons.cwd_safety(""), "unverifiable")
        self.assertEqual(reasons.cwd_safety(os.getcwd()), "local")

    def test_description_has_no_credential_or_private_path(self) -> None:
        observations = {
            "shareStatus": "MAP-FAILED",
            "shareVisible": False,
            "shareDetail": "MAP-FAILED:Access is denied \\\\HOST\\private",
        }
        detail = "share unavailable at C:\\Users\\owner\\.secrets\\pw.txt"
        described = reasons.describe(
            reasons.Refusal("smb-share-unconfirmed", detail, observations))
        blob = json.dumps(described)
        for needle in ("HOST", "private", "Users", "owner", "C:\\", "\\\\"):
            self.assertNotIn(needle, blob, described)
        self.assertIn("<redacted-path>", blob)
        self.assertEqual(described["schema"], reasons.SCHEMA)
        # A credential that only appears inside a path is redacted with it.
        self.assertNotIn(self.SECRET, reasons.sanitize(
            "credential C:\\secrets\\" + self.SECRET + ".txt"))

    def test_every_reason_is_bounded_and_path_free(self) -> None:
        for code, entry in reasons.REASONS.items():
            with self.subTest(code=code):
                self.assertTrue(entry["nextAction"].strip())
                self.assertLessEqual(len(entry["nextAction"]), 200)
                self.assertLessEqual(len(entry["detail"]), 160)
                self.assertNotIn("C:\\", entry["nextAction"] + entry["detail"])
                self.assertNotIn("\\\\", entry["nextAction"] + entry["detail"])
        self.assertEqual(reasons.REASONS["none"]["state"], "ready")
        self.assertEqual(reasons.REASONS[reasons.UNKNOWN]["state"], "unknown")

    def test_unknown_reason_code_is_not_trusted(self) -> None:
        refusal = reasons.Refusal("fabricated-reason", "x")
        self.assertEqual(refusal.reason, reasons.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
