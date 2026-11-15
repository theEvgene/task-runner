"""Evidence-bound settlement for durably claimed Lenovo jobs.

A crashed worker leaves a live process *unobserved*: nobody saw its exit
code, signal, or final output. Reconciliation can therefore only prove
the tree is absent and release ownership truthfully. It never derives
success from bytes supplied by a key holder, because a key holder can
supply any bytes. The sealed terminal state is always a non-success
``termination: "unknown"`` with no fabricated exit code.

The proof is a typed, machine-checked document
(``lenovo-job-reconciliation/2`` carrying
``lenovo-job-terminal-evidence/1``) bound to the claim generation, the
exact signed job bytes, the exact resource set, the durable start owner,
and the recorded child pid. Opaque evidence bytes and the previous
schema are refused, not reinterpreted.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import job_ownership
import job_tree
import proc

PROOF_SCHEMA = "lenovo-job-reconciliation/2"
EVIDENCE_SCHEMA = "lenovo-job-terminal-evidence/1"
DERIVATION = "unobserved-crash"


def canonical_digest(document: dict) -> str:
    material = json.dumps(document, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _claimed_tree_alive(child_pid: int) -> bool:
    """True when the claimed tree may still be active (fail closed True)."""

    try:
        if proc.is_alive(int(child_pid)):
            return True
    except Exception:
        return True
    try:
        stragglers = job_tree.descendants(int(child_pid))
    except Exception:
        return True
    if stragglers:
        return True
    if os.name != "nt":
        try:
            if job_tree._posix_pg_members(int(child_pid)):
                return True
        except Exception:
            return True
    return False


def _validate_release(release: dict, prepared: job_ownership.PreparedJob,
                      generation: str, owner: dict, result_digest: str,
                      job_id: str) -> bool:
    return (
        release.get("schema") == "lenovo-job-release/1"
        and release.get("kind") == "release"
        and release.get("id") == job_id
        and release.get("generation") == generation
        and release.get("owner") == owner
        and release.get("job_envelope_sha256")
        == prepared.envelope_sha256
        and release.get("resources") == list(prepared.resources)
        and release.get("result_envelope_sha256") == result_digest)


def publish_release(queue: Path, key: bytes,
                    prepared: job_ownership.PreparedJob,
                    generation: str, bridge: object, now) -> str:
    """Record that this exact claim reached a verified immutable result."""

    try:
        claim = job_ownership.read_claim(queue, prepared, bridge, key)
        if claim.get("generation") != generation:
            raise ValueError("release generation does not own the claim")
        result_raw = bridge.read(str(queue / "results"), prepared.job_id)
        bridge.open_envelope(result_raw, key, "result", prepared.job_id)
        result_digest = hashlib.sha256(result_raw).hexdigest()
        release = {
            "schema": "lenovo-job-release/1", "kind": "release",
            "id": prepared.job_id, "generation": generation,
            "owner": claim["owner"],
            "job_envelope_sha256": prepared.envelope_sha256,
            "resources": list(prepared.resources),
            "result_envelope_sha256": result_digest,
            "released_at": now(),
        }
        bridge.publish(str(queue / "releases"), prepared.job_id,
                       bridge.seal(release, key))
        return "sealed"
    except bridge.JobExistsError:
        try:
            raw = bridge.read(str(queue / "releases"), prepared.job_id)
            release = bridge.open_envelope(
                raw, key, "release", prepared.job_id)
            result_raw = bridge.read(
                str(queue / "results"), prepared.job_id)
            result_digest = hashlib.sha256(result_raw).hexdigest()
            claim = job_ownership.read_claim(queue, prepared, bridge, key)
            if (claim.get("generation") == generation
                    and _validate_release(release, prepared, generation,
                                          claim["owner"], result_digest,
                                          prepared.job_id)):
                return "sealed"
        except Exception:
            pass
        print(f"worker: invalid release for {prepared.job_id}",
              file=sys.stderr)
        return "failed"
    except Exception as error:
        print(f"worker: cannot publish release for {prepared.job_id} "
              f"({error})", file=sys.stderr)
        return "failed"


def _validate_evidence(evidence: object, claim: dict,
                       child_pid: int) -> bool:
    """Typed terminal evidence: only what can be re-derived or re-observed."""

    return (
        isinstance(evidence, dict)
        and evidence.get("schema") == EVIDENCE_SCHEMA
        and evidence.get("owner") == claim["owner"]
        and evidence.get("child_pid") == int(child_pid)
        and evidence.get("tree_absent") is True
        and isinstance(evidence.get("observer"), str)
        and bool(evidence.get("observer"))
        and isinstance(evidence.get("observed_at"), str)
        and bool(evidence.get("observed_at"))
    )


def reconcile(queue: Path, key: bytes, job_id: str, proof_path: Path,
              bridge: object, prepare, seal_result, now,
              worker_schema: str) -> int:
    """Settle retained ownership only from a signed, claim-bound proof.

    The sealed outcome is never success: a crash-killed execution has no
    observed exit code, signal, or output, so the truthful terminal state
    is ``error`` + ``termination: "unknown"``. The proof's only effect is
    to justify releasing ownership once the tree is proven absent.
    """

    prepared = prepare(queue, key, job_id)
    if isinstance(prepared, str):
        print(f"worker: cannot reconcile unverified job {job_id}",
              file=sys.stderr)
        return 2
    try:
        claim = job_ownership.read_claim(queue, prepared, bridge, key)
        proof_raw = proof_path.read_bytes()
        proof = bridge.open_envelope(
            proof_raw, key, "reconciliation", job_id)
    except Exception as error:
        print(f"worker: reconciliation proof refused for {job_id} ({error})",
              file=sys.stderr)
        return 2
    if proof.get("schema") != PROOF_SCHEMA:
        print(f"worker: reconciliation proof schema refused for {job_id}: "
              f"only {PROOF_SCHEMA} typed evidence is accepted",
              file=sys.stderr)
        return 2
    evidence = proof.get("evidence")
    if not (
            proof.get("claim_generation") == claim["generation"]
            and proof.get("job_envelope_sha256")
            == prepared.envelope_sha256
            and proof.get("resources") == list(prepared.resources)):
        print(f"worker: reconciliation proof binding invalid for {job_id}",
              file=sys.stderr)
        return 2
    owner_pid = claim["owner"]["pid"]
    if proc.is_alive(owner_pid):
        # The claiming owner is still running: the side effect may still
        # be active. Stop that worker first (its pid dies with it) and
        # reconcile only the retained claim afterwards.
        print(f"worker: reconciliation refused for {job_id}: "
              f"claim owner pid {owner_pid} still active",
              file=sys.stderr)
        return 2
    try:
        start = job_ownership.read_start(
            queue, prepared, claim["generation"], bridge, key)
    except Exception as error:
        print(f"worker: reconciliation refused for {job_id}: "
              f"no durable start record ({error})",
              file=sys.stderr)
        return 2
    if start.get("owner") != claim["owner"]:
        print(f"worker: reconciliation refused for {job_id}: "
              f"start owner mismatch",
              file=sys.stderr)
        return 2
    child_pid = start["child_pid"]
    if not _validate_evidence(evidence, claim, child_pid):
        print(f"worker: reconciliation refused for {job_id}: "
              f"typed terminal evidence invalid",
              file=sys.stderr)
        return 2
    if _claimed_tree_alive(int(child_pid)):
        print(f"worker: reconciliation refused for {job_id}: "
              f"claimed child tree {child_pid} still active",
              file=sys.stderr)
        return 2
    evidence_sha256 = canonical_digest(evidence)
    # Derivation is fixed, not caller-supplied: nobody observed this
    # execution's exit, signal, or output.
    fields = {
        "status": "error",
        "exit_code": None,
        "termination": "unknown",
        "signal": None,
        "stdout": "",
        "stderr": ("reconciled after worker crash: terminal exit, signal, "
                   "and output were not observed; ownership released by "
                   "proof, outcome is not success"),
        "started_at": str(start.get("started_at") or ""),
        "timed_out": False,
        "output_truncated": False,
        "reconciliation": {
            "claim_generation": claim["generation"],
            "evidence_sha256": evidence_sha256,
            "proof_envelope_sha256": hashlib.sha256(proof_raw).hexdigest(),
            "outcome_derivation": DERIVATION,
        },
    }
    sealed = seal_result(queue, key, job_id, **fields)
    if sealed not in ("sealed", "skipped"):
        return 2
    released = publish_release(
        queue, key, prepared, claim["generation"], bridge, now)
    if released != "sealed":
        return 2
    print(json.dumps({
        "schema": worker_schema, "mode": "reconcile",
        "id": job_id, "outcome": "sealed-non-success"}))
    return 0
