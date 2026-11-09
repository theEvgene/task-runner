"""Offline fixtures for the pipeline observer (synthetic only).

No test reaches Lenovo, GitLab, or the network. GitLab answers are
fakes keyed by API path, traces are fakes keyed by job id, and the
signed queue is a temporary directory sealed with a temporary key
through a fixture-only bridge double that mirrors the documented
signed-queue wire format (HMAC-SHA256 envelope, kind/id binding,
immutable publish). Production uses the existing signed bridge client;
this double is test-only and never live evidence.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "pipeline_observer"
sys.path.insert(0, str(SCRIPTS))

import gate  # noqa: E402
import probe as probe_mod  # noqa: E402
import readback  # noqa: E402
import request as request_mod  # noqa: E402

URL = "https://gitlab.example/team/app/-/merge_requests/7"
SHA1, SHA2 = "a" * 40, "b" * 40
BASE = "/projects/team%2Fapp/merge_requests/7"
PROJECT = "team%2Fapp"

SINGLE_LOG = ("FAIL src/cart.test.ts > totals > applies discount\n"
              "Expected: 90; Received: 100\n")


def pipe(pid: int, sha: str, state: str) -> dict:
    return {"id": pid, "sha": sha, "status": state,
            "web_url": f"https://gitlab.example/team/app/-/pipelines/{pid}"}


def job(name: str, state: str, job_id: int) -> dict:
    return {"id": job_id, "name": name, "status": state, "stage": "test",
            "web_url": f"https://gitlab.example/team/app/-/jobs/{job_id}"}


def bridge_entry(name: str, state: str, child_id: int | None) -> dict:
    downstream = None
    if child_id is not None:
        downstream = {"id": child_id, "project_id": 42, "status": state,
                      "web_url": "https://gitlab.example/team/app/"
                                 f"-/pipelines/{child_id}"}
    return {"name": name, "status": state,
            "downstream_pipeline": downstream}


def prof(jobs=(), children=()) -> dict:
    return {"schema": "pipeline-observer-checks/1",
            "required_jobs": list(jobs),
            "required_child_pipelines": [dict(c) for c in children]}


class FixtureUnavailable(RuntimeError):
    pass


class FakeGitlab:
    def __init__(self, heads, pipelines, details=None, jobs=None,
                 bridges=None, child_details=None, child_jobs=None,
                 traces=None, unavailable=None):
        self.heads = list(heads)
        self.pipelines = pipelines
        self.details = details or {}
        self.jobs = jobs or {}
        self.bridges = bridges or {}
        self.child_details = child_details or {}
        self.child_jobs = child_jobs or {}
        self.traces = traces or {}
        self.unavailable = unavailable

    def _head(self):
        if len(self.heads) > 1:
            return self.heads.pop(0)
        return self.heads[0]

    def get(self, host: str, path: str):
        bare = path.split("?")[0]
        if self.unavailable and self.unavailable in path:
            raise FixtureUnavailable("unavailable in fixture")
        if bare == BASE:
            return {"sha": self._head(), "web_url": URL}
        if bare == BASE + "/pipelines":
            return self.pipelines
        if bare.endswith("/jobs"):
            project = path.split("/projects/")[1].split("/pipelines/")[0]
            pid = int(bare.split("/pipelines/")[1].split("/")[0])
            pool = self.jobs if project == PROJECT else self.child_jobs
            return list(pool.get(pid, []))
        if bare.endswith("/bridges"):
            pid = int(bare.split("/pipelines/")[1].split("/")[0])
            return list(self.bridges.get(pid, []))
        project = path.split("/projects/")[1].split("/pipelines/")[0]
        pid = int(bare.split("/pipelines/")[1])
        pool = self.details if project == PROJECT else self.child_details
        if pid not in pool:
            raise AssertionError(f"unknown pipeline {pid}")
        return pool[pid]

    def read_log(self, host, project, job_id):
        return self.traces.get(job_id)


# --- Fixture-only signed-queue double (test-only, not the product route) ---

class FixtureProtocolError(Exception):
    pass


class FixtureTimeout(Exception):
    pass


class FixtureBridge:
    """Minimal HMAC envelope + immutable file queue for offline tests."""

    FetchTimeout = FixtureTimeout
    ProtocolError = FixtureProtocolError
    BridgeError = Exception

    @staticmethod
    def load_key(key_file, key_format, queue):
        raw = Path(key_file).read_bytes()
        if key_format == "base64":
            import base64 as _b64
            raw = _b64.b64decode(raw.decode("ascii").strip())
        if len(raw) != 32:
            raise FixtureProtocolError("invalid key")
        q = Path(queue).resolve()
        key_path = Path(key_file).resolve()
        try:
            key_path.relative_to(q)
        except ValueError:
            pass
        else:
            raise FixtureProtocolError("key must live outside the queue")
        return bytes(raw)

    @staticmethod
    def build_result_payload(job_id, status, exit_code=None, stdout="",
                             stderr="", timed_out=False,
                             output_truncated=False):
        return {"kind": "result", "id": job_id, "status": status,
                "exit_code": exit_code, "stdout": stdout, "stderr": stderr,
                "timed_out": bool(timed_out),
                "output_truncated": bool(output_truncated)}

    @staticmethod
    def seal(payload: dict, key: bytes) -> bytes:
        payload_bytes = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")).encode()
        mac = hmac.new(key, payload_bytes, hashlib.sha256).hexdigest()
        env = {"v": 1, "kind": payload["kind"], "id": payload["id"],
               "payload_b64": base64.b64encode(payload_bytes).decode(),
               "mac": mac}
        return json.dumps(env, ensure_ascii=False,
                          separators=(",", ":")).encode()

    @staticmethod
    def open(payload_bytes: bytes, key: bytes, expected_id: str) -> dict:
        try:
            env = json.loads(bytes(payload_bytes).decode())
        except Exception as error:
            raise FixtureProtocolError("malformed envelope") from error
        if env.get("v") != 1 or env.get("kind") != "result":
            raise FixtureProtocolError("bad kind")
        if env.get("id") != expected_id:
            raise FixtureProtocolError("bad id")
        try:
            raw = base64.b64decode(env["payload_b64"], validate=True)
        except Exception as error:
            raise FixtureProtocolError("malformed base64") from error
        calc = hmac.new(key, raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, str(env.get("mac", "")).lower()):
            raise FixtureProtocolError("bad mac")
        payload = json.loads(raw.decode())
        if payload.get("kind") != "result" or payload.get("id") != expected_id:
            raise FixtureProtocolError("bad payload")
        return payload

    def publish(self, directory, job_id, data: bytes):
        base = Path(directory)
        base.mkdir(parents=True, exist_ok=True)
        final = base / f"{job_id}.json"
        if final.exists():
            raise FixtureProtocolError(f"job {job_id} already exists")
        tmp = base / f".{job_id}.tmp"
        tmp.write_bytes(bytes(data))
        try:
            os.link(str(tmp), str(final))
        except FileExistsError as error:
            raise FixtureProtocolError(
                f"job {job_id} already exists") from error
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
        return final

    def fetch_result(self, queue, key, job_id, timeout_seconds=5,
                     poll_seconds=0.05):
        deadline = time.monotonic() + timeout_seconds
        path = Path(queue) / "results" / f"{job_id}.json"
        while True:
            if path.is_file():
                return self.open(path.read_bytes(), key, job_id)
            if time.monotonic() >= deadline:
                raise FixtureTimeout(f"no sealed result for {job_id}")
            time.sleep(poll_seconds)


def run_probe(fake: FakeGitlab, expected: str, profile: dict) -> dict:
    def get(host: str, path: str):
        try:
            return fake.get(host, path)
        except FixtureUnavailable as error:
            raise gate.GitlabUnavailable(str(error)) from error
    return probe_mod.probe(URL, expected, profile, get=get,
                           read_log=fake.read_log)


def passed_probe_receipt(mr_url: str = URL, sha: str = SHA1) -> dict:
    return {"schema": "pipeline-observer-probe/1", "read_only": True,
            "mr_url": mr_url, "expected_sha": sha, "observed_sha": sha,
            "status_outcome": "passed", "verdict": "passed",
            "reason": "gate_passed", "pipeline_id": 1,
            "pipeline_url": "https://gitlab.example/team/app/-/pipelines/1"}


# --- Fixture live-client dir (answers probe main() without GitLab) ---
#
# probe.py main() loads ``gitlab_api.api_get`` and
# ``mr_pipeline_recover_support.live_read_log`` from --client-dir. This
# builder writes fixture-only stand-ins of those two modules answering
# from one JSON file named by PIPELINE_OBSERVER_FIXTURE_JSON. Production
# uses the existing gitlab-mr-flow scripts directory instead.

FIXTURE_API_MODULE = '''
"""Fixture-only gitlab_api stand-in (offline tests, never live)."""
import json as _json
import os as _os


class GitlabUnavailable(Exception):
    pass


def _fixture():
    path = _os.environ.get("PIPELINE_OBSERVER_FIXTURE_JSON", "")
    with open(path, encoding="utf-8") as _handle:
        return _json.load(_handle)


def api_get(host, path):
    fx = _fixture()
    bare = path.split("?")[0]
    if "/merge_requests/" in bare and bare.endswith("/pipelines"):
        return list(fx.get("pipelines", []))
    if "/merge_requests/" in bare and "/pipelines" not in bare:
        return {"sha": fx["mr_sha"], "web_url": fx["mr_url"]}
    if bare.endswith("/jobs"):
        pid = bare.split("/pipelines/")[1].split("/")[0]
        jobs = fx.get("jobs") or {}
        return list(jobs.get(str(pid), []))
    if bare.endswith("/bridges"):
        pid = bare.split("/pipelines/")[1].split("/")[0]
        bridges = fx.get("bridges") or {}
        return list(bridges.get(str(pid), []))
    if "/pipelines/" in bare:
        pid = bare.rsplit("/", 1)[1]
        details = fx.get("details") or {}
        child = fx.get("child_details") or {}
        if str(pid) in details:
            return dict(details[str(pid)])
        if str(pid) in child:
            return dict(child[str(pid)])
        raise GitlabUnavailable("unknown pipeline in fixture: %s" % pid)
    raise GitlabUnavailable("unknown fixture endpoint: %s" % path)
'''

FIXTURE_SUPPORT_MODULE = '''
"""Fixture-only recover-support stand-in (offline tests, never live)."""
import json as _json
import os as _os


def live_read_log(host, project, job_id):
    path = _os.environ.get("PIPELINE_OBSERVER_FIXTURE_JSON", "")
    with open(path, encoding="utf-8") as _handle:
        fx = _json.load(_handle)
    return (fx.get("traces") or {}).get(str(job_id))
'''


def write_fixture_client(directory, fixture: dict) -> Path:
    """Write fixture.json + two stand-in client modules; return the dir."""
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    (target / "gitlab_api.py").write_text(
        FIXTURE_API_MODULE + "\n", encoding="utf-8")
    (target / "mr_pipeline_recover_support.py").write_text(
        FIXTURE_SUPPORT_MODULE + "\n", encoding="utf-8")
    fixture_path = target / "fixture.json"
    fixture_path.write_text(json.dumps(fixture, ensure_ascii=False),
                            encoding="utf-8")
    return target


def passed_client_fixture(mr_url: str = URL, sha: str = SHA1) -> dict:
    """One green parent pipeline fixture for the stand-in client."""
    pid = 1
    return {
        "mr_url": mr_url, "mr_sha": sha,
        "pipelines": [{"id": pid, "sha": sha, "status": "success",
                       "web_url": mr_url.replace(
                           "/-/merge_requests/7",
                           "/-/pipelines/%d" % pid)}],
        "details": {str(pid): {"id": pid, "sha": sha, "status": "success",
                               "web_url": "https://gitlab.example/x/%d"
                                           % pid}},
        "jobs": {str(pid): [{"id": 11, "name": "unit", "status": "success",
                             "web_url": "https://gitlab.example/x/11"}]},
        "bridges": {str(pid): []},
        "child_details": {}, "child_jobs": {}, "traces": {},
    }


def profile_file(directory, jobs=("unit",), children=()) -> Path:
    path = Path(directory) / "required-checks.json"
    path.write_text(json.dumps(
        {"schema": "pipeline-observer-checks/1",
         "required_jobs": list(jobs),
         "required_child_pipelines": [dict(c) for c in children]},
        ensure_ascii=False), encoding="utf-8")
    return path


def find_real_bridge_dir() -> Path | None:
    """Locate the existing signed bridge client (read-only check)."""
    candidates = [os.environ.get("PIPELINE_OBSERVER_REAL_BRIDGE_DIR")]
    home = Path.home()
    candidates += [
        str(home / ".agents" / "skills" / "lan-task-bridge" / "scripts"),
    ]
    for candidate in candidates:
        if candidate and (Path(candidate) / "lan_bridge.py").is_file():
            return Path(candidate)
    return None


def powershell_exe() -> str | None:
    import shutil
    return shutil.which("powershell") or shutil.which("pwsh")
