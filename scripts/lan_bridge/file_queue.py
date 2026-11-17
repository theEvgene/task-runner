"""Immutable file queue for the signed LAN bridge (stdlib only).

One ``<id>.json`` per envelope: publish never replaces an existing file, a
read never consumes a ``.tmp`` file, and every id and path is validated before
it touches the filesystem. The queue therefore offers at-most-once
publication without depending on the envelope or CLI layers above it.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from bridge_errors import (
    AtomicPublishNotSupportedError, InvalidJobIdError, JobExistsError,
    JobNotFoundError, SizeLimitError, UnsafePathError,
)

PUBLISH_MAX = 2 * 1024 * 1024
DEFAULT_READ_MAX = 2 * 1024 * 1024

JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _validate_job_id(job_id: str) -> None:
    if not isinstance(job_id, str) or JOB_ID_RE.fullmatch(job_id) is None:
        raise InvalidJobIdError("job_id must be exactly 32 lowercase hex")
    if job_id in (".", ".."):
        raise UnsafePathError("unsafe job_id")


def _safe_join(directory: str | os.PathLike, filename: str) -> Path:
    base = Path(directory).resolve()
    target = (base / filename).resolve()
    try:
        target.relative_to(base)
    except ValueError as e:
        raise UnsafePathError("path traversal detected") from e
    return target


def publish(directory: str | os.PathLike, job_id: str, data: bytes) -> Path:
    """Publish ``data`` as immutable ``<id>.json``; never replaces (no dup)."""
    if isinstance(data, bool) or not isinstance(data, (bytes, bytearray)):
        raise TypeError("data must be bytes")
    data_b = bytes(data)
    if len(data_b) > PUBLISH_MAX:
        raise SizeLimitError("data exceeds publish limit")
    _validate_job_id(job_id)
    base = Path(directory)
    base.mkdir(parents=True, exist_ok=True)
    base_resolved = base.resolve()
    final_path = _safe_join(base_resolved, f"{job_id}.json")
    if final_path.exists():
        raise JobExistsError(f"job {job_id} already exists")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{job_id}.",
                                    suffix=".tmp", dir=str(base_resolved))
    tmp_path = Path(tmp_name)
    try:
        os.close(fd)
        with tmp_path.open("wb") as fh:
            fh.write(data_b)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        try:
            if os.name == "nt":
                try:
                    os.rename(str(tmp_path), str(final_path))
                except FileExistsError as e:
                    raise JobExistsError(f"job {job_id} already exists") from e
            else:
                try:
                    os.link(str(tmp_path), str(final_path))
                except FileExistsError as e:
                    raise JobExistsError(f"job {job_id} already exists") from e
                except OSError as e:
                    raise AtomicPublishNotSupportedError(
                        "POSIX atomic no-clobber requires link(2)") from e
                try:
                    os.unlink(str(tmp_path))
                except OSError:
                    pass
        except (JobExistsError, AtomicPublishNotSupportedError):
            raise
        except OSError as e:
            raise AtomicPublishNotSupportedError(
                "atomic no-clobber publish unsupported on this filesystem") from e
        return final_path
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


def read(directory: str | os.PathLike, job_id: str,
         max_bytes: int = DEFAULT_READ_MAX) -> bytes:
    """Read ``<id>.json`` bytes; never consumes ``.tmp`` files."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
        raise ValueError("max_bytes must be non-negative int")
    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative int")
    _validate_job_id(job_id)
    final_path = _safe_join(Path(directory).resolve(), f"{job_id}.json")
    try:
        size = final_path.stat().st_size
    except OSError as e:
        raise JobNotFoundError(f"job {job_id} not found") from e
    if size > max_bytes:
        raise SizeLimitError("job exceeds max_bytes")
    try:
        with final_path.open("rb") as fh:
            data = fh.read(max_bytes + 1)
    except OSError as e:
        raise JobNotFoundError(f"job {job_id} not found") from e
    if len(data) > max_bytes:
        raise SizeLimitError("job exceeds max_bytes")
    return data
