"""Lenovo share cleaner for the single-shortcut launch (stdlib only).

Moves stale ``*.tmp`` files from the TOP LEVEL of the shared LAN folder into
a dedicated trash directory. It never deletes, never touches the configured
queue (wherever it lives), never touches state or trash folders placed inside
the share, never touches ``*.json`` requests or manifests, and never recurses.
Trash is only created after placement checks prove it sits outside the queue
and the share; the queue and the share never are. Trash target names are
unique per move, so one pass never overwrites another file.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

CLEANER_SCHEMA = "lenovo-cleaner/1"


def _fail(message: str) -> int:
    print(f"cleaner refused: {message}", file=sys.stderr)
    return 2


def _norm(path: Path) -> str:
    """Lexical absolute form (no existence required, for placement checks)."""

    import os

    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _within_or_equal(child: Path, parent: Path) -> bool:
    import os

    child_n, parent_n = _norm(child), _norm(parent)
    return child_n == parent_n or child_n.startswith(parent_n + os.sep)


def _is_inside(child: Path, parent: Path) -> bool:
    try:
        Path(child).resolve().relative_to(Path(parent).resolve())
        return True
    except (ValueError, OSError, RuntimeError):
        return False


def _is_protected(entry: Path, queue: Path, trash: Path,
                  state: Path | None) -> bool:
    """True when a top-level share entry belongs to the queue/trash/state."""

    if _within_or_equal(entry, queue) or _within_or_equal(entry, trash):
        return True
    return state is not None and _within_or_equal(entry, state)


def run_once(shared: Path, trash: Path, max_age_s: float,
             queue: Path | None = None, state: Path | None = None) -> dict:
    moved: list[str] = []
    skipped = 0
    now = time.time()
    queue = Path(queue) if queue is not None else shared / "queue"
    if _within_or_equal(trash, queue) or _within_or_equal(trash, shared):
        error = f"trash {trash} must be outside the queue and the shared folder"
        print(f"cleaner: {error}", file=sys.stderr)
        return {"moved": moved, "skipped": skipped, "error": error}
    try:
        entries = sorted(shared.iterdir())
    except OSError as error:
        print(f"cleaner: cannot list shared folder ({error})", file=sys.stderr)
        return {"moved": moved, "skipped": skipped, "error": str(error)[:200]}
    try:
        trash.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        print(f"cleaner: cannot create trash ({error})", file=sys.stderr)
        return {"moved": moved, "skipped": skipped, "error": str(error)[:200]}
    for entry in entries:
        if _is_protected(entry, queue, trash, state):
            skipped += 1
            continue  # the signed queue (any dirname), trash, or state
        if not entry.is_file() or entry.is_symlink():
            skipped += 1
            continue
        if not (entry.name.endswith(".tmp") or entry.name.startswith(".")):
            continue  # only temp files; requests/manifests/bundles stay
        try:
            if now - entry.stat().st_mtime < max_age_s:
                skipped += 1
                continue
        except OSError:
            skipped += 1
            continue
        target = trash / f"{entry.name}.{os.getpid()}.{time.time_ns()}"
        try:
            os.replace(str(entry), str(target))
            moved.append(entry.name)
        except OSError as error:
            print(f"cleaner: cannot move {entry.name} ({error})", file=sys.stderr)
            skipped += 1
    return {"moved": moved, "skipped": skipped}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cleaner.py", description="Lenovo share cleaner")
    parser.add_argument("--shared", required=True)
    parser.add_argument("--queue-dir", default=None,
                        help="configured signed queue (default: <shared>/queue)")
    parser.add_argument("--trash-dir", required=True)
    parser.add_argument("--state-dir", default=None,
                        help="launcher state dir, excluded when inside the share")
    parser.add_argument("--max-age", type=float, default=3600.0)
    parser.add_argument("--once", action="store_true", help="single pass, then exit 0")
    parser.add_argument("--interval", type=float, default=300.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    shared, trash = Path(args.shared), Path(args.trash_dir)
    queue = Path(args.queue_dir) if args.queue_dir else shared / "queue"
    state = Path(args.state_dir) if args.state_dir else None
    if not shared.is_dir():
        return _fail(f"shared folder unavailable at {args.shared}: check path/mount")
    if _within_or_equal(trash, queue) or _within_or_equal(trash, shared):
        return _fail("trash must be outside the queue and the shared folder")
    if state is not None and (_within_or_equal(state, queue) or _within_or_equal(state, shared)):
        return _fail("state must be outside the queue and the shared folder")
    if not 0 <= float(args.max_age) <= 30 * 24 * 3600:
        return _fail("max-age out of range")
    import json

    if args.once:
        result = run_once(shared, trash, float(args.max_age), queue, state)
        print(json.dumps({"schema": CLEANER_SCHEMA, "mode": "once", **result}))
        return 0 if "error" not in result else 2
    if not 1 <= float(args.interval) <= 3600:
        return _fail("interval must be within [1, 3600] seconds")
    while True:
        result = run_once(shared, trash, float(args.max_age), queue, state)
        print(json.dumps({"schema": CLEANER_SCHEMA, "mode": "loop",
                          "moved": len(result["moved"]),
                          "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}),
              flush=True)
        time.sleep(float(args.interval))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
