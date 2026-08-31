"""Command line entry point for the fund cost census.

    fundcensus compare -c config/funds.yaml

Needs no network and no market data: every input comes from a factsheet or a
broker's published fee schedule.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import load_config
from .report import render


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fundcensus",
        description="Measure how much of an index fund's return actually "
                    "reaches you, and compare candidates on total cost "
                    "rather than headline fee.")
    # Accepted before or after the subcommand: putting it after is the
    # natural thing to type, and rejecting that is a pointless obstacle.
    parser.add_argument("-c", "--config", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    compare = sub.add_parser("compare",
                             help="compare candidate funds on total cost")
    compare.add_argument("-c", "--config", default=None)

    args = parser.parse_args(argv)
    path = Path(args.config or "config/funds.yaml")
    if not path.exists():
        print(f"config not found: {path}", file=sys.stderr)
        return 2
    try:
        cfg = load_config(path)
    except (ValueError, KeyError) as exc:
        print(f"{path}: {exc}", file=sys.stderr)
        return 2
    print(render(cfg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
