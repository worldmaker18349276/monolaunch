#!/usr/bin/env python3
"""Sync logs of a launch run from remote machines back to the local log dir."""

import argparse
from pathlib import Path
import re
import sys

import monolaunch.monoresource as monoresource
import rospkg

def get_log_dir() -> str:
    return rospkg.get_log_dir() # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType, reportUnknownMemberType]

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

def parse_run(value: str) -> "str | int":
    """Return a run selector: 'latest', a uuid, or a non-negative int (0 = newest run)."""
    if value == "latest" or UUID_RE.match(value):
        return value
    try:
        nth = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected 'latest', a uuid or a non-negative integer, got {value!r}")
    if nth < 0:
        raise argparse.ArgumentTypeError(f"index must be >= 0, got {nth}")
    return nth


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "run",
        nargs="?",
        default="latest",
        type=parse_run,
        help="'latest' (default), a run uuid, or N to pick the N-th last run (0 = newest)",
    )
    parser.add_argument("--log-dir", type=str, help="path to the log dir")
    parser.add_argument("--machine", type=str, help="machine://..., for determining log dir")
    parser.add_argument("--launch", type=str, help="path to the .launch file, for determining log dir")
    args = parser.parse_args()

    if args.log_dir:
        logs_dir = Path(args.log_dir)
    elif args.machine:
        logs_dir = Path(monoresource.remote_expandvars(args.machine, "${ROS_LOG_DIR}"))
    elif args.launch:
        logs_dir = monoresource.get_log_dir_from_launch(args.launch)
    else:
        logs_dir = Path(get_log_dir())

    run = args.run
    try:
        if isinstance(run, int):
            local_log_dir = monoresource.find_log_dir(logs_dir, run)
        elif isinstance(run, str):
            local_log_dir = logs_dir / run
        else:
            local_log_dir = (logs_dir / "latest").resolve()
        monoresource.sync_logs(local_log_dir)
    except (ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
