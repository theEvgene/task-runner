"""Continuous agent-facing result waiter for the signed LAN bridge.

Second agent operation: ``lan_bridge.py submit`` returns a job id; this
command observes that SAME id across short windows until a confirmed
terminal result or a concrete blocker. It never publishes, so restarting it
with the same id after any interruption resumes without duplicating the job.
Bounded expiry (``--max-windows`` reached) is ``unknown``, never "running".
Stdlib only; no protocol, MAC, queue or key change.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import lan_bridge as bridge  # noqa: E402
import wait as waitmod  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="get.py",
        description="Observe one job id until a verified result or blocker.")
    parser.add_argument("--queue", required=True)
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--key-format", default="raw",
                        choices=("raw", "base64"))
    parser.add_argument("--id", required=True)
    parser.add_argument("--window", type=float, default=60.0,
                        help="seconds per short observation window")
    parser.add_argument("--poll", type=float, default=30.0,
                        help="seconds between queue probes (owner default)")
    parser.add_argument("--max-windows", type=int, default=0,
                        help="window bound; 0 waits without a window bound")
    parser.add_argument("--receipt-out", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Errors echo only the job id and wait parameters, never key material.
    started = time.monotonic()
    try:
        key = bridge.load_key(args.key_file, args.key_format, args.queue)
        payload, report = waitmod.get_result(
            args.queue, key, args.id,
            window_timeout=float(args.window),
            poll_seconds=float(args.poll),
            max_windows=int(args.max_windows))
    except (bridge.BridgeError, ValueError, bridge.FileQueueError,
            bridge.ProtocolError) as e:
        print(f"get refused: {e}", file=sys.stderr)
        return 2
    elapsed = time.monotonic() - started
    if payload is None:
        print(f"TIMEOUT waiting for {args.id}: "
              f"{report['detail']} {report['action']}", file=sys.stderr)
        return 3
    bridge._report_confirmed_state(payload)
    print(f"elapsed={elapsed:.1f}s windows={report['windows_used']} "
          f"probes={report['probes']} outcome=verified id={args.id}",
          file=sys.stderr)
    text = json.dumps(payload, ensure_ascii=False)
    if args.receipt_out:
        out = Path(args.receipt_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
