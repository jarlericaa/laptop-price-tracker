#!/usr/bin/env python3
"""Join the Top 100 Shopee variations to their contexts' reviewed price report (offline)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from laptop_tracker.variation_ranking import (
    CONTEXT_COLUMNS,
    VARIATION_COLUMNS,
)
from laptop_tracker.variation_reports import (
    VariationReportError,
    build_variation_reports,
    load_context_report,
    sha256_file,
    validate_variation_reports,
    write_reports,
)
from laptop_tracker.verified_top import VerifiedRankingInputError, read_csv_rows


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build the variation-level price reports (schema shopee-variation-sheets-1) from the "
            "output of build_top_shopee_variations.py and the track_prices.py report of its "
            "context list. One row per selected Shopee variation with its own Shopee snapshot "
            "price; retailer cells are the row's context's reviewed cells; row-to-observation "
            "associations are separate records; row, context and unique-observation coverage are "
            "reported apart. Offline: no retailer request, no rematching, no store access, no "
            "Sheets access."
        )
    )
    parser.add_argument(
        "--variation-dir",
        type=Path,
        required=True,
        help="build_top_shopee_variations.py output folder.",
    )
    parser.add_argument(
        "--context-report-dir",
        type=Path,
        required=True,
        help="track_prices.py report folder built from the context list.",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        top = read_csv_rows(
            args.variation_dir / "top_100_shopee_variations.csv",
            VARIATION_COLUMNS,
            "Selected variations",
        )
        contexts = read_csv_rows(
            args.variation_dir / "variation_contexts.csv", CONTEXT_COLUMNS, "Variation contexts"
        )
        report = load_context_report(args.context_report_dir)
        reports = build_variation_reports(
            top, contexts, sha256_file(args.variation_dir / "context_selected_list.csv"), report
        )
        issues = validate_variation_reports(reports)
    except (FileNotFoundError, VerifiedRankingInputError, VariationReportError, KeyError) as exc:
        print(f"Variation reports stopped: {exc}", file=sys.stderr)
        return 1
    if issues:
        print(
            "Variation reports failed their own validation; nothing was written:", file=sys.stderr
        )
        for issue in issues[:20]:
            print(f"  - {issue}", file=sys.stderr)
        return 1
    write_reports(args.output_dir, reports)
    c = reports.coverage["counts"]
    week = reports.summary["report_week"]
    print(
        f"Reporting week {week['start']} to {week['end_exclusive']} ({week['status']}); cutoff "
        f"{reports.summary['as_of_utc']}."
    )
    print(
        f"Variation rows: {c['selected_variations']} (distinct contexts "
        f"{c['distinct_configuration_contexts']}; mapped {c['mapped_selected_variations']}; "
        f"unmapped {c['unmapped_selected_variations']}; without trusted context "
        f"{c['rows_without_trusted_context']}); own Shopee price {c['rows_with_own_shopee_price']}."
    )
    print(
        f"Retailer coverage: rows with a matching price {c['matching_priced_variation_rows']}, "
        f"rows showing an alternative {c['alternative_priced_variation_rows']} (alternative-only "
        f"{c['rows_by_comparison_status'].get('ALTERNATIVE_PRICES_ONLY', 0)}), any price "
        f"{c['variation_rows_with_any_retailer_price']}; contexts with a matching price "
        f"{c['contexts_with_matching_price']}; unique matching observations "
        f"{c['unique_matching_observations']} behind {c['row_associations_matching']} row "
        f"associations."
    )
    print(f"Reports written to {args.output_dir}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
