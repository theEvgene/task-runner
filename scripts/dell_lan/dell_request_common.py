"""Small CLI helpers shared by Dell request routes."""

from __future__ import annotations

import argparse
from pathlib import Path

from dell_lan_common import Stop


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
