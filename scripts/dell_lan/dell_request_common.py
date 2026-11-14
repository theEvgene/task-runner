"""Small CLI helpers shared by Dell request routes."""

from __future__ import annotations

import argparse
from pathlib import Path

from dell_lan_common import Stop
from shell_command import build_for_transport

RECONCILE_DRIVERS = {
    "lenovo_publish.py",
    "lenovo_publish_existing.py",
    "lenovo_transfer.py",
}


class SingleDialect(argparse.Action):
    """Accept exactly one explicit dialect declaration."""

    def __call__(self, parser, namespace, value, option_string=None) -> None:
        if getattr(namespace, self.dest, None) is not None:
            parser.error(f"{option_string} must be declared exactly once")
        setattr(namespace, self.dest, value)


def require_absolute_lenovo_paths(*pairs: tuple[str, str]) -> None:
    for label, value in pairs:
        if not value or not Path(value).is_absolute():
            raise Stop("usage", code=2, detail=(
                f"{label} must be the absolute path on the Lenovo machine"))


def flow_args(args) -> list[str]:
    """Extra Lenovo command arguments for a publication driver."""

    result: list[str] = []
    flow_dir = getattr(args, "lenovo_flow_dir", None)
    if flow_dir:
        require_absolute_lenovo_paths(("--lenovo-flow-dir", flow_dir))
        result += ["--flow-dir", str(Path(flow_dir))]
    if getattr(args, "reconcile", False):
        result.append("--reconcile")
    return result


def add_reconcile_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--reconcile", action="store_true",
                        help="render the consent-gated read-only recovery command")


def render_reconcile(command_parts: list[str], driver: str, dialect: str,
                     enabled: bool) -> str | None:
    if enabled or driver not in RECONCILE_DRIVERS:
        return None
    return build_for_transport(command_parts + ["--reconcile"], dialect)
