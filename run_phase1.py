#!/usr/bin/env python3
"""Run the safe Phase 1 target-generation pipeline."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from laptop_tracker.config import Settings
from laptop_tracker.pipeline import PipelineSafetyError, run_phase1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate Phase 1 laptop targets from read-only raw data."
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--dry-run", action="store_true", help="Read raw and write local files only (default)."
    )
    actions.add_argument(
        "--write-sheets",
        action="store_true",
        help="Explicitly replace approved P1 output tabs after local output generation.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = run_phase1(Settings.from_env(), write_sheets=args.write_sheets)
    except (FileNotFoundError, PipelineSafetyError, RuntimeError, ValueError) as exc:
        print(f"Phase 1 failed safely: {exc}", file=sys.stderr)
        return 1
    print(
        "Phase 1 complete: "
        f"{result.summary['top_100_count']} targets; local outputs in {result.local_output_dir}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
