#!/usr/bin/env python3
"""One signed-queue batch transfer of closed npm tarballs (Dell side).

Builds the needs manifest, submits the single Lenovo fill command through
the existing signed LAN queue, fetches the MAC-bound result, and re-verifies
every byte against the lockfile. Transport only: trust comes from the
lockfile integrity check in ``dell_deps_lib.verify_export_files``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import dell_deps_lib as lib
from dell_lan_common import (
    Stop, default_bridge_dir, sha256_bytes,
)

SCHEMA_NEEDS = "dell-deps-needs/1"
SCHEMA_EXPORT = "dell-deps-export/1"


def run_batch(args, *, lock_sha: str, manifest_sha: str,
              wanted: list[dict]) -> tuple[dict, dict | None, dict | None]:
    """Submit one batch and verify it. Returns (transfer, verified, failure).

    ``failure`` is a ``done()`` payload (verdict + fields) when the batch
    must stop the run, else None. ``verified`` maps ``name@version`` to bytes.
    Raises Stop/FailClosed only for programmer errors handled by the caller.
    """

    from dell_lan_common import fetch_verified, load_bridge, read_key
    bridge = load_bridge(default_bridge_dir())
    queue = Path(args.queue)
    shared = Path(args.shared)
    key = read_key(bridge, queue, Path(args.key_file), "raw",
                   repos=[Path(args.project), Path(args.scratch_root)],
                   shared=shared)
    token = args.token or os.urandom(16).hex()
    needs_name = f"deps-needs-{token}.json"
    export_name = f"deps-export-{token}.json"
    needs = {"schema": SCHEMA_NEEDS, "token": token, "lockSha256": lock_sha,
             "manifestSha256": manifest_sha,
             "publicRegistry": args.public_registry,
             "packages": [{k: n[k] for k in ("name", "version", "resolved",
                                             "integrity")} for n in wanted]}
    needs_text = json.dumps(needs, ensure_ascii=False,
                            separators=(",", ":")) + "\n"
    needs_path = shared / needs_name
    if needs_path.exists():
        raise FileExistsError(f"refusing to replace {needs_name}")
    needs_path.write_bytes(needs_text.encode("utf-8"))
    needs_digest = sha256_bytes(needs_text.encode("utf-8"))
    driver = Path(args.lenovo_driver_dir)
    command = (f'"{args.lenovo_python}" "{driver / "lenovo_deps_fill.py"}"'
               f' --request "{needs_path}" --request-sha256 {needs_digest}'
               f' --source-dir "{Path(args.lenovo_source_dir)}"'
               f" --out-name {export_name}")
    job_id = bridge.submit_job(str(queue), key, command, str(driver),
                               300, 65536, [str(shared / export_name)])
    print(f"JOBID={job_id}", flush=True)
    transfer: dict = {"attempted": True, "jobId": job_id, "needs": needs_name,
                      "export": export_name}
    try:
        payload = fetch_verified(bridge, queue, key, job_id,
                                 args.fetch_timeout, args.fetch_poll)
    except Stop as stop:
        if stop.verdict == "unknown":
            return transfer, None, {"verdict": "unknown", "code": 3,
                                    "reason": "the signed result did not arrive "
                                              "in time; nothing was installed",
                                    "nextAction": "reconcile the job before any retry",
                                    "transfer": transfer}
        raise
    from dell_lan_common import result_failure as carried_failure
    failure = carried_failure(payload)
    if failure is not None:
        return transfer, None, {"verdict": "transfer-refused", "code": 1,
                                "reason": "Lenovo refused the batch; nothing was installed",
                                "nextAction": "fix the corporate source (missing or tampered "
                                              "tarballs) and re-run with a fresh token",
                                "transfer": transfer, "carried": failure}
    try:
        export = json.loads(str(payload.get("stdout") or ""))
    except ValueError:
        export = None
    bound = (isinstance(export, dict) and export.get("schema") == SCHEMA_EXPORT
             and export.get("token") == token
             and export.get("lockSha256") == lock_sha)
    verified = lib.verify_export_files(shared, export, wanted) if bound else None
    if verified is None:
        verdict = "integrity-mismatch" if bound else "transfer-refused"
        return transfer, None, {"verdict": verdict, "code": 1,
                                "reason": "a transferred tarball fails the lock integrity "
                                          "check, is unbound, or the batch is incomplete",
                                "nextAction": "fix the corporate source and re-run with a fresh "
                                              "token; nothing was installed",
                                "transfer": transfer}
    transfer["files"] = [
        {"name": i.get("name"), "version": i.get("version"),
         "sha256": i.get("sha256"), "bytes": i.get("bytes")}
        for i in export.get("files") or []]
    return transfer, verified, None
