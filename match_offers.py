#!/usr/bin/env python3
"""Match saved retailer offer observations to a validated TARGET_MASTER snapshot (offline)."""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from laptop_tracker.match_run import (
    MATCH_EVIDENCE_COLUMNS,
    MATCH_RESULT_COLUMNS,
    ObservationFileError,
    TargetSnapshotError,
    evidence_records,
    is_review_item,
    match_all,
    matcher_from_validated,
    read_observations_csv,
    result_record,
    write_records,
)
from laptop_tracker.target_master import (
    TargetMasterSchemaError,
    read_target_master_csv,
    validate_target_master,
)

RESULTS_FILENAME = "offer_match_results.csv"
REVIEW_FILENAME = "offer_match_review_queue.csv"
EVIDENCE_FILENAME = "offer_match_evidence.csv"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Match saved offer observations (OfferObservation.to_full_record() CSV columns) "
            "against a local TARGET_MASTER CSV snapshot using the five-field exact rule. Writes "
            "local result, review-queue, and evidence reports and prints counts only. Makes no "
            "network or Google Sheets calls and never creates targets or mappings."
        )
    )
    parser.add_argument("--target-master", type=Path, required=True, help="TARGET_MASTER CSV.")
    parser.add_argument(
        "--observations", type=Path, required=True, help="Offer observation CSV."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "data" / "output",
        help="Directory for local reports (default: data/output, ignored by Git).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    outputs = [
        args.output_dir / name for name in (RESULTS_FILENAME, REVIEW_FILENAME, EVIDENCE_FILENAME)
    ]
    # Remove previous reports so a failed run never leaves stale results behind.
    for path in outputs:
        path.unlink(missing_ok=True)

    try:
        targets = validate_target_master(read_target_master_csv(args.target_master))
        matcher = matcher_from_validated(targets)
        observations = read_observations_csv(args.observations)
    except TargetSnapshotError as exc:
        codes = targets.errors["error_code"].value_counts().sort_index()
        for code, count in codes.items():
            print(f"  TARGET_MASTER {code}: {count}")
        print(f"Offer matching stopped: {exc}", file=sys.stderr)
        return 1
    except (
        FileNotFoundError,
        UnicodeDecodeError,
        TargetMasterSchemaError,
        ObservationFileError,
    ) as exc:
        print(f"Offer matching stopped: {exc}", file=sys.stderr)
        return 1

    matched = match_all(matcher, observations)
    results = [result_record(item, result) for item, result in matched]
    write_records(outputs[0], MATCH_RESULT_COLUMNS, results)
    review = (
        record
        for record, (_, result) in zip(results, matched, strict=True)
        if is_review_item(result)
    )
    write_records(outputs[1], MATCH_RESULT_COLUMNS, review)
    write_records(
        outputs[2],
        MATCH_EVIDENCE_COLUMNS,
        (row for item, result in matched for row in evidence_records(item, result)),
    )

    # Counts and codes only: offer text and target values stay in the local reports.
    statuses = Counter(result.status.value for _, result in matched)
    reasons = Counter(code for _, result in matched for code in result.reason_codes)
    print(f"Observations matched: {len(matched)}; targets indexed: {len(targets.valid_rows)}.")
    for status, count in sorted(statuses.items()):
        print(f"  {status}: {count}")
    for code, count in sorted(reasons.items()):
        print(f"  reason {code}: {count}")
    print(f"Reports written to {args.output_dir}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
