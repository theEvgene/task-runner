"""Bounded result payload and return-file helpers."""

from __future__ import annotations

import base64
import json
from pathlib import Path

RETURN_FILES_MAX_COUNT = 8
RETURN_FILE_MAX_BYTES = 262144
RETURN_FILES_MAX_TOTAL_BYTES = 1048576
PAYLOAD_SAFE_MAX_BYTES = 1400000
MIN_FALLBACK_STDERR = (
    "result too large; outputs dropped to fit the signed envelope")


def _payload_size(payload: dict) -> int:
    try:
        return len(json.dumps(payload, ensure_ascii=False,
                              separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError):
        return PAYLOAD_SAFE_MAX_BYTES + 1


def fit_payload(payload: dict) -> dict:
    """Trim payload in place so its JSON stays under the safe ceiling."""

    if _payload_size(payload) <= PAYLOAD_SAFE_MAX_BYTES:
        return payload
    if payload.get("files"):
        payload["files"] = {}
        payload["output_truncated"] = True
    if _payload_size(payload) <= PAYLOAD_SAFE_MAX_BYTES:
        return payload
    for budget in (65536, 16384, 4096, 0):
        if budget == 0:
            payload["stdout"] = ""
            payload["stderr"] = MIN_FALLBACK_STDERR[:4096]
            payload["output_truncated"] = True
            break
        payload["stdout"] = str(payload.get("stdout") or "")[:budget]
        payload["stderr"] = str(payload.get("stderr") or "")[:budget]
        payload["output_truncated"] = True
        if _payload_size(payload) <= PAYLOAD_SAFE_MAX_BYTES:
            break
    return payload


def collect_return_files(
        declared: list, limit_files: int = RETURN_FILES_MAX_COUNT,
        limit_each: int = RETURN_FILE_MAX_BYTES,
        limit_total: int = RETURN_FILES_MAX_TOTAL_BYTES,
        ) -> tuple[dict, bool]:
    """Collect declared paths with per-file, count, and total byte bounds."""

    files: dict[str, str] = {}
    total = 0
    truncated = False
    for raw in (declared or [])[:limit_files]:
        try:
            path = Path(str(raw))
        except (ValueError, TypeError):
            continue
        try:
            if not path.is_file():
                continue
            with path.open("rb") as handle:
                data = handle.read(limit_each + 1)
        except OSError:
            continue
        if len(data) > limit_each:
            data = data[:limit_each]
            truncated = True
        if total + len(data) > limit_total:
            truncated = True
            break
        total += len(data)
        files[str(path)] = base64.b64encode(data).decode("ascii")
    if isinstance(declared, list) and len(declared) > limit_files:
        truncated = True
    return files, truncated
