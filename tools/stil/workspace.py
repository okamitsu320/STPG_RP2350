"""Shared disk-backed workspace handling for host-only STIL workflows."""

from __future__ import annotations

import os
from argparse import ArgumentParser, Namespace
from pathlib import Path


WORK_DIR_ENV = "STPG_WORK_DIR"
DEFAULT_WORK_ROOT_NAME = "stpg-work"


def default_work_dir(scope: str) -> Path:
    """Return the default workspace for one CLI scope.

    ``/tmp`` is intentionally not used: on many Linux installations it is a
    size-limited tmpfs even when the root filesystem has ample capacity.
    ``STPG_WORK_DIR`` is a root override, not the final scope directory.
    """

    configured_root = os.environ.get(WORK_DIR_ENV)
    root = Path(configured_root).expanduser() if configured_root else Path.home() / DEFAULT_WORK_ROOT_NAME
    return root / scope


def add_work_dir_options(parser: ArgumentParser, *, scope: str) -> None:
    """Add the common ``--work-dir`` option and legacy ``--output-dir`` alias."""

    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--work-dir",
        type=Path,
        help=f"disk-backed workspace root (default: {default_work_dir(scope)})",
    )
    group.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        help="legacy alias for --work-dir",
    )
    parser.set_defaults(_work_dir_scope=scope)


def resolve_work_dir(args: Namespace) -> Path:
    """Resolve an explicitly supplied or default workspace path."""

    work_dir = getattr(args, "work_dir", None)
    if work_dir is not None:
        return work_dir
    output_dir = getattr(args, "output_dir", None)
    if output_dir is not None:
        return output_dir
    return default_work_dir(getattr(args, "_work_dir_scope", "workflow"))
