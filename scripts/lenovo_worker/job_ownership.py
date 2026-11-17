"""Authenticated durable claims and the bounded worker supervisor."""

from __future__ import annotations

import importlib.util
import os
import sys
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path

import job_lifecycle

CLAIM_SCHEMA = "lenovo-job-claim/1"
START_SCHEMA = "lenovo-job-start/1"
ID_SUFFIX = ".json"

_CLAIM_SHAPE = None


def _claim_shape() -> object:
    """The shared claim predicate from the canonical bridge client.

    Loaded by path (like the bridge itself) so this module never depends on
    import order or on a copy of the claim fields here. One predicate means
    the worker and an observer cannot drift into two validators.
    """
    global _CLAIM_SHAPE
    if _CLAIM_SHAPE is None:
        path = Path(__file__).resolve().parent.parent / "lan_bridge" / "job_claim.py"
        spec = importlib.util.spec_from_file_location(
            "lenovo_worker_job_claim", str(path))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _CLAIM_SHAPE = module
    return _CLAIM_SHAPE


@dataclass(frozen=True)
class PreparedJob:
    job_id: str
    command: str
    workdir: str
    timeout_seconds: int
    output_limit: int
    dialect: str | None
    return_files: list
    return_files_invalid: bool
    resources: tuple[str, ...]
    envelope_sha256: str


def pending(queue: Path) -> list[str]:
    try:
        names = sorted(path.name for path in (queue / "jobs").iterdir()
                       if path.is_file() and path.name.endswith(ID_SUFFIX))
    except OSError as error:
        print(f"worker: cannot list jobs ({error})", file=sys.stderr)
        return []
    result = []
    for name in names:
        job_id = name[:-len(ID_SUFFIX)]
        if (len(job_id) == 32
                and all(c in "0123456789abcdef" for c in job_id)
                and not (queue / "results" / name).exists()):
            result.append(job_id)
    return result


def batch_exit(counts: dict) -> int:
    if counts.get("refused") or counts.get("failed"):
        return 2
    return 3 if counts.get("owned") or counts.get("unknown") else 0


def validate_claim(payload: dict, prepared: PreparedJob) -> bool:
    return _claim_shape().claim_matches(
        payload, job_id=prepared.job_id,
        job_envelope_sha256=prepared.envelope_sha256,
        resources=prepared.resources)


def read_claim(queue: Path, prepared: PreparedJob, bridge: object,
               key: bytes) -> dict:
    raw = bridge.read(str(queue / "claims"), prepared.job_id)
    payload = bridge.open_envelope(raw, key, "claim", prepared.job_id)
    if not validate_claim(payload, prepared):
        raise ValueError("claim does not match authenticated job/resource identity")
    return payload


def claim_job(queue: Path, prepared: PreparedJob, owner_id: str,
              bridge: object, key: bytes, now,
              generation: str | None = None) -> str:
    """Publish a MAC-bound claim; never trust mere filename existence."""

    if (queue / "results" / f"{prepared.job_id}.json").exists():
        return "skipped"
    claim = {
        "schema": CLAIM_SCHEMA, "kind": "claim", "id": prepared.job_id,
        "owner": {"instance": owner_id, "pid": os.getpid()},
        "generation": generation or uuid.uuid4().hex,
        "claimed_at": now(), "resources": list(prepared.resources),
        "job_envelope_sha256": prepared.envelope_sha256,
        "state": "owned-unknown-until-signed-result",
    }
    try:
        bridge.publish(str(queue / "claims"), prepared.job_id,
                       bridge.seal(claim, key))
        return "claimed"
    except bridge.JobExistsError:
        try:
            read_claim(queue, prepared, bridge, key)
        except Exception:
            print(f"worker: refusing invalid claim for {prepared.job_id}",
                  file=sys.stderr)
            return "refused"
        return "owned"
    except Exception as error:
        print(f"worker: cannot claim job {prepared.job_id} ({error})",
              file=sys.stderr)
        return "failed"


def validate_start(payload: dict, prepared: PreparedJob,
                     generation: str) -> bool:
    owner = payload.get("owner")
    child_pid = payload.get("child_pid")
    return (
        payload.get("schema") == START_SCHEMA
        and payload.get("kind") == "start"
        and payload.get("id") == prepared.job_id
        and payload.get("generation") == generation
        and isinstance(owner, dict)
        and isinstance(owner.get("instance"), str) and owner["instance"]
        and isinstance(owner.get("pid"), int)
        and not isinstance(owner["pid"], bool)
        and isinstance(child_pid, int) and not isinstance(child_pid, bool)
        and child_pid > 0
        and payload.get("resources") == list(prepared.resources)
        and payload.get("job_envelope_sha256") == prepared.envelope_sha256)


def publish_start(queue: Path, prepared: PreparedJob, owner: dict,
                  generation: str, child_pid: int, bridge: object,
                  key: bytes, now) -> str:
    """Durably bind the spawned child pid to this exact claim attempt."""

    record = {
        "schema": START_SCHEMA, "kind": "start", "id": prepared.job_id,
        "owner": {"instance": owner["instance"], "pid": owner["pid"]},
        "generation": generation,
        "child_pid": int(child_pid),
        "started_at": now(), "resources": list(prepared.resources),
        "job_envelope_sha256": prepared.envelope_sha256,
    }
    try:
        bridge.publish(str(queue / "starts"), prepared.job_id,
                       bridge.seal(record, key))
        return "published"
    except bridge.JobExistsError:
        try:
            raw = bridge.read(str(queue / "starts"), prepared.job_id)
            existing = bridge.open_envelope(
                raw, key, "start", prepared.job_id)
            if (validate_start(existing, prepared, generation)
                    and existing.get("owner") == record["owner"]
                    and existing.get("child_pid") == int(child_pid)):
                return "published"
        except Exception:
            pass
        print(f"worker: refusing conflicting start for {prepared.job_id}",
              file=sys.stderr)
        raise ValueError("conflicting start record")


def read_start(queue: Path, prepared: PreparedJob, generation: str,
               bridge: object, key: bytes) -> dict:
    raw = bridge.read(str(queue / "starts"), prepared.job_id)
    payload = bridge.open_envelope(raw, key, "start", prepared.job_id)
    if not validate_start(payload, prepared, generation):
        raise ValueError("start record does not match claim attempt")
    return payload


def run_bounded(waiting: list[PreparedJob], max_active: int,
                claim, execute) -> dict[str, int]:
    counts: dict[str, int] = {}
    active: dict[Future, PreparedJob] = {}
    with ThreadPoolExecutor(max_workers=max_active,
                            thread_name_prefix="lenovo-job") as pool:
        while waiting or active:
            admitted = True
            while waiting and len(active) < max_active and admitted:
                admitted = False
                resources = [job.resources for job in active.values()]
                for index, prepared in enumerate(waiting):
                    if any(job_lifecycle.resources_overlap(
                            prepared.resources, resource)
                           for resource in resources):
                        continue
                    waiting.pop(index)
                    outcome = claim(prepared)
                    if outcome == "claimed":
                        active[pool.submit(execute, prepared)] = prepared
                    else:
                        counts[outcome] = counts.get(outcome, 0) + 1
                    admitted = True
                    break
            if active:
                completed, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in completed:
                    prepared = active.pop(future)
                    try:
                        outcome = future.result()
                    except Exception as error:
                        print(f"worker: job {prepared.job_id} ended unknown "
                              f"({error})", file=sys.stderr)
                        outcome = "unknown"
                    counts[outcome] = counts.get(outcome, 0) + 1
                    if outcome == "unknown":
                        # An unproven descendant may still own any resource.
                        # Finish already-active neighbors, but admit no more.
                        waiting.clear()
            elif waiting:
                raise RuntimeError("worker admission stalled")
    return counts
