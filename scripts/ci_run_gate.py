#!/usr/bin/env python3
"""Publication readiness gate and job summary for one weekly run folder.

Reads the run's own ``status.json``, ``stages.jsonl``, discovery summary and publication preview
report (never console output). Exit 0: the run may be published unattended. Exit 1: blocked, with
the reasons printed and written to the job summary. No Sheets access, no retailer requests.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from laptop_tracker import retailer_failure_policy as rfp
from laptop_tracker import run_gate
from laptop_tracker import state_store as ss


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, help="Write the gate result JSON here.")
    parser.add_argument("--title", default="Publication readiness")
    parser.add_argument(
        "--accept-retailer-failures",
        action="store_true",
        help="A person reviewed the retailer request failures and accepts a run the automated "
        "policy blocks. Never overrides any other check.",
    )
    parser.add_argument("--max-age-hours", type=float, default=run_gate.DEFAULT_MAX_AGE_HOURS)
    parser.add_argument(
        "--max-matching-drop-pct",
        type=float,
        default=None,
        help="Optional: block if matching rows fell by more than this percentage "
        "against the last successful publication (off by default).",
    )
    parser.add_argument("--no-summary", action="store_true")
    return parser


def previous_counts(store: ss.rs.ReleaseStore, ring: ss.sc.KeyRing) -> dict | None:
    found = ss.latest_successful_publication(store, ring)
    return found.get("counts") if found else None


def main(
    argv: list[str] | None = None,
    *,
    store: ss.rs.ReleaseStore | None = None,
    ring: ss.sc.KeyRing | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        policy = rfp.FailurePolicy.from_environment()
    except rfp.PolicyConfigError as exc:
        # A malformed threshold never falls back to a default: the gate refuses.
        print(f"BLOCKED: invalid retailer-failure policy configuration: {exc}", file=sys.stderr)
        if not args.no_summary:
            ss.write_step_summary(
                f"## {args.title}\n\n**Gate:** BLOCKED  \n- Invalid policy: {exc}"
            )
        return 1
    previous = None
    if args.max_matching_drop_pct is not None:
        try:
            if store is None or ring is None:
                store, ring = ss.store_and_keys()
            previous = previous_counts(store, ring)
        except ss.StateError as exc:
            print(f"State error: {exc}", file=sys.stderr)
            return 1
    result = run_gate.evaluate(
        args.run_dir,
        accept_retailer_failures=args.accept_retailer_failures,
        max_age_hours=args.max_age_hours,
        previous_counts=previous,
        max_matching_drop_pct=args.max_matching_drop_pct,
        policy=policy,
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result.as_dict(), indent=2, default=str) + "\n")
    if not args.no_summary:
        ss.write_step_summary(run_gate.render_summary(args.title, result))
    run_gate.report_to_console(result)
    print("Publication gate:", result.decision)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
