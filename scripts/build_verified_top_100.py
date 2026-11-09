#!/usr/bin/env python3
"""Build the Top 100 verified configurations working list (local, read-only inputs)."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from laptop_tracker.sheets_source import SheetsSourceError
from laptop_tracker.supporting_tables import validate_match_review, validate_source_model_map
from laptop_tracker.target_master import TargetMasterSchemaError, validate_target_master
from laptop_tracker.verified_top import (
    CONFIGURATION_COLUMNS,
    DEFAULT_TOP_N,
    EXCLUSION_COLUMNS,
    LEDGER_COLUMNS,
    OUTPUT_FILES,
    REVIEW_PRIORITY_COLUMNS,
    VerifiedRankingInputError,
    build_verified_ranking,
    check_source_top_100,
    load_catalog_tables,
    read_csv_rows,
    read_register,
    read_sales_snapshot,
    sha256_file,
    write_csv,
)

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "output"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Rank unique ACTIVE CONFIGURATION targets with complete five-field identities by the "
            "combined GMV of their eligible, approved mapped sources, and write the Top 100 "
            "verified configurations plus a contribution ledger, exclusions, a review-priority "
            "report, and a run summary. This is a target-level working list, not the overall "
            "sales Top 100, which stays unchanged. Reads local CSVs, or with --from-sheets the "
            "three catalog tabs using the read-only scope. Never reads raw, never rebuilds Phase "
            "4, never writes to Sheets, and never promotes or maps rows."
        )
    )
    parser.add_argument(
        "--ranking",
        type=Path,
        default=OUTPUT / "eligible_model_ranking.csv",
        help="Full Phase 4 eligible source ranking (default: in data/output/).",
    )
    parser.add_argument(
        "--source-top-100",
        type=Path,
        default=OUTPUT / "top_100_targets.csv",
        help="Accepted source-level Top 100, kept as a reference (default: in data/output/).",
    )
    parser.add_argument(
        "--target-master", type=Path, default=OUTPUT / "target_master_validated.csv"
    )
    parser.add_argument(
        "--source-model-map", type=Path, default=OUTPUT / "source_model_map_validated.csv"
    )
    parser.add_argument("--match-review", type=Path, default=OUTPUT / "match_review_validated.csv")
    parser.add_argument(
        "--from-sheets",
        action="store_true",
        help="Read TARGET_MASTER, SOURCE_MODEL_MAP, and MATCH_REVIEW from Sheets (read-only).",
    )
    parser.add_argument(
        "--identity-register",
        type=Path,
        default=ROOT / "config" / "identity_review_register.csv",
        help="Optional local register of known unresolved identity conflicts (ignored by Git).",
    )
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT / "verified_top_100")
    return parser.parse_args(argv)


def load_catalog(
    args: argparse.Namespace,
) -> tuple[dict[str, list[list[str]]], dict[str, tuple[str, str]]]:
    """The three catalog tables plus, per table, (source label, SHA-256)."""
    return load_catalog_tables(
        {
            "TARGET_MASTER": args.target_master,
            "SOURCE_MODEL_MAP": args.source_model_map,
            "MATCH_REVIEW": args.match_review,
        },
        from_sheets=args.from_sheets,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    outputs = {key: args.output_dir / name for key, name in OUTPUT_FILES.items()}
    for path in outputs.values():
        path.unlink(missing_ok=True)  # A failed run never leaves stale results behind.

    try:
        snapshot = read_sales_snapshot(args.ranking)
        original_ids: set[str] = set()
        if args.source_top_100 and args.source_top_100.is_file():
            original_ids = check_source_top_100(
                snapshot,
                read_csv_rows(
                    args.source_top_100,
                    (
                        "model_id",
                        "run_id",
                        "extract_date",
                        "m_minus_1_month",
                        "mtd_month",
                        "m_minus_1_gmv_usd",
                        "mtd_gmv_usd",
                        "ranking_gmv_usd",
                    ),
                    "Source Top 100",
                ),
            )
        tables, catalog_fingerprints = load_catalog(args)
        target_master = validate_target_master(tables["TARGET_MASTER"])
        source_model_map = validate_source_model_map(tables["SOURCE_MODEL_MAP"], target_master)
        match_review = validate_match_review(tables["MATCH_REVIEW"])
        register = read_register(args.identity_register)
        tm_fingerprint = catalog_fingerprints["TARGET_MASTER"][1]
        result = build_verified_ranking(
            snapshot,
            target_master,
            source_model_map,
            match_review,
            source_top_100_ids=original_ids,
            register=register,
            top_n=args.top_n,
            target_master_sha256=tm_fingerprint,
        )
    except (
        FileNotFoundError,
        UnicodeDecodeError,
        KeyError,
        SheetsSourceError,
        TargetMasterSchemaError,
        VerifiedRankingInputError,
    ) as exc:
        print(f"Verified configuration ranking stopped: {exc}", file=sys.stderr)
        return 1

    write_csv(outputs["ranking"], CONFIGURATION_COLUMNS, result.configurations)
    write_csv(outputs["top"], CONFIGURATION_COLUMNS, result.top)
    write_csv(outputs["ledger"], LEDGER_COLUMNS, result.ledger)
    write_csv(outputs["exclusions"], EXCLUSION_COLUMNS, result.exclusions)
    write_csv(outputs["review_priority"], REVIEW_PRIORITY_COLUMNS, result.review_priority)
    fingerprints = {
        str(args.ranking): sha256_file(args.ranking),
        **({str(args.source_top_100): sha256_file(args.source_top_100)} if original_ids else {}),
        **dict(catalog_fingerprints.values()),
        **(
            {str(args.identity_register): sha256_file(args.identity_register)}
            if args.identity_register.is_file()
            else {}
        ),
    }
    summary = {
        "list_name": "Top 100 verified configurations",
        "policy": "Owner decision 2026-10-12: unique ACTIVE CONFIGURATION targets with complete "
        "five-field identities, ranked by the combined GMV of eligible, approved mapped sources. "
        "Not the overall sales Top 100.",
        "built_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "sales_snapshot": {
            "run_id": snapshot.run_id,
            "extract_date": snapshot.extract_date,
            "m_minus_1_month": snapshot.m_minus_1_month,
            "mtd_month": snapshot.mtd_month,
            "labels": snapshot.period_labels(),
        },
        "catalog_source": "Google Sheets (read-only)"
        if args.from_sheets
        else "local CSV snapshots",
        "input_sha256": fingerprints,
        "counts": result.counts,
        "outputs": {key: str(path) for key, path in outputs.items()},
    }
    outputs["summary"].write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    # Counts only: IDs and names stay in the local files.
    counts = result.counts
    labels = snapshot.period_labels()
    print(
        f"Sales snapshot {snapshot.run_id}: M-1 {labels['m_minus_1_gmv_usd']}; MTD "
        f"{labels['mtd_gmv_usd']}. Historical data; not refreshed by this run."
    )
    print(
        f"Eligible sources: {counts['eligible_sources']}; contributing: "
        f"{counts['contributing_sources']}; excluded: {counts['excluded_sources']}."
    )
    for key, value in counts.items():
        if key.startswith("excluded_") and key not in {"excluded_sources", "excluded_targets"}:
            print(f"  {key}: {value}")
    print(
        f"Qualifying unique configurations: {counts['qualifying_configurations']} "
        f"({counts['configurations_with_multiple_sources']} with several sources); target-level "
        f"exclusions: {counts['excluded_targets']}."
    )
    print(
        f"Selected: {counts['selected_configurations']} of {counts['requested_top_n']}; "
        f"shortfall: {counts['shortfall']}."
    )
    print(f"Review-priority rows: {counts['review_priority_rows']}. Outputs in {args.output_dir}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
