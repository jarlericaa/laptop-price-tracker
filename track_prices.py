#!/usr/bin/env python3
"""Offline Phase 9: store saved observations, match them, and write weekly comparisons/history."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from laptop_tracker.match_run import (
    ObservationFileError,
    TargetSnapshotError,
    matcher_from_validated,
    read_observations_csv,
)
from laptop_tracker.observation_store import (
    ObservationConflictError,
    ObservationStore,
    ObservationStoreError,
)
from laptop_tracker.offer_terms import (
    BUNDLE_FLAG_POLICY,
    DEFAULT_OFFER_TERMS_POLICY,
    OFFER_TERMS_POLICIES,
)
from laptop_tracker.price_tracking import (
    COMPARISON_COLUMNS,
    HISTORY_COLUMNS,
    MATCHING_RULES_VERSION,
    REPORTING_CURRENCY,
    REPORTING_TIMEZONE,
    RETAILERS,
    REVIEW_COLUMNS,
    SELECTION_COLUMNS,
    SERIES_COLUMNS,
    SOURCE_LEDGER_COLUMNS,
    TIE_BREAK,
    PriceTrackingInputError,
    build_price_tracking,
    build_source_ledger,
    match_stored_observations,
    matching_context,
    parse_as_of,
    parse_week_start,
    utc_text,
    validate_selected_list,
)
from laptop_tracker.supporting_tables import validate_match_review, validate_source_model_map
from laptop_tracker.target_master import (
    TargetMasterSchemaError,
    read_table_snapshot_csv,
    validate_target_master,
)
from laptop_tracker.verified_top import (
    CONFIGURATION_COLUMNS,
    LEDGER_COLUMNS,
    VerifiedRankingInputError,
    read_csv_rows,
    sha256_file,
    write_csv,
)

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "data" / "output"
DEFAULT_STORE = ROOT / "data" / "history" / "price_history.sqlite3"
SALES_RANKING_COLUMNS = (
    "model_id",
    "run_id",
    "extract_date",
    "m_minus_1_gmv_usd",
    "mtd_gmv_usd",
    "ranking_gmv_usd",
    "shopee_current_price_php",
    "price_status",
)
OUTPUT_FILES = {
    "comparison": "weekly_price_comparison.csv",
    "selections": "weekly_price_selections.csv",
    "series": "weekly_price_series.csv",
    "history": "price_history.csv",
    "review": "price_review_queue.csv",
    "ledger": "configuration_source_ledger.csv",
    "summary": "price_tracking_run_summary.json",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Offline Phase 9. Import saved OfferObservation CSVs into the local SQLite history "
            "store (duplicates are skipped; contradictions refuse the batch), match every stored "
            "observation against the validated TARGET_MASTER snapshot, and write a weekly "
            "comparison for the selected configurations, retailer price selections, a weekly "
            "price series, per-observation price history, a review queue, a configuration-to-"
            "source ledger, and a run summary. Dates are explicit. Prints counts only. Makes no "
            "retailer requests, never reads raw or Google Sheets, and never writes Sheets, "
            "targets, or mappings."
        )
    )
    parser.add_argument(
        "--observations",
        type=Path,
        action="append",
        default=[],
        help="Saved observation CSV to import first (repeatable; optional).",
    )
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE, help="SQLite history store.")
    parser.add_argument(
        "--target-master", type=Path, default=OUTPUT / "target_master_validated.csv"
    )
    parser.add_argument(
        "--source-model-map", type=Path, default=OUTPUT / "source_model_map_validated.csv"
    )
    parser.add_argument("--match-review", type=Path, default=OUTPUT / "match_review_validated.csv")
    parser.add_argument(
        "--selected-list",
        type=Path,
        default=OUTPUT / "verified_top_100" / "top_100_verified_configurations.csv",
    )
    parser.add_argument(
        "--contributions",
        type=Path,
        default=OUTPUT / "verified_top_100" / "verified_configuration_contributions.csv",
    )
    parser.add_argument(
        "--sales-ranking",
        type=Path,
        default=OUTPUT / "eligible_model_ranking.csv",
        help="Phase 4 eligible ranking for the Shopee snapshot price per source (optional).",
    )
    parser.add_argument(
        "--no-sales-ranking", action="store_true", help="Do not read the sales ranking."
    )
    parser.add_argument(
        "--week-start", required=True, help="Monday of the report week, YYYY-MM-DD."
    )
    parser.add_argument(
        "--as-of",
        required=True,
        help="Explicit cut-off time with a UTC offset, e.g. 2026-10-12T09:00:00+08:00.",
    )
    parser.add_argument(
        "--history-start",
        help="Monday of the first history week (default: the first week with observations).",
    )
    parser.add_argument(
        "--offer-terms-policy",
        choices=OFFER_TERMS_POLICIES,
        default=DEFAULT_OFFER_TERMS_POLICY,
        help=(
            "Commercial-terms rule for price eligibility (default: the purchase-terms policy of "
            f"2026-10-19; '{BUNDLE_FLAG_POLICY}' keeps every checked bundle comparable)."
        ),
    )
    parser.add_argument(
        "--search-context",
        type=Path,
        action="append",
        default=[],
        help=(
            "A collection match_results.csv (repeatable). Its expected_target_id links each "
            "observation to the configuration it was searched for, so a blank price can name the "
            "unconfirmed candidate. Context only: never an assignment."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Report directory (default: data/output/price_tracking/week_<week-start>).",
    )
    return parser.parse_args(argv)


def read_search_context(paths: list[Path]) -> dict[str, list[str]]:
    """``observation_id -> [expected target IDs]`` from collection match results."""
    context: dict[str, list[str]] = {}
    for path in paths:
        for row in read_csv_rows(path, ("observation_id", "expected_target_id"), "Search context"):
            if row["observation_id"] and row["expected_target_id"]:
                context.setdefault(row["observation_id"], []).append(row["expected_target_id"])
    return context


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run_started = datetime.now(UTC)
    try:
        week = parse_week_start(args.week_start)
        as_of = parse_as_of(args.as_of)
        history_start = parse_week_start(args.history_start).start if args.history_start else None
    except PriceTrackingInputError as exc:
        print(f"Price tracking stopped: {exc}", file=sys.stderr)
        return 1
    output_dir = args.output_dir or OUTPUT / "price_tracking" / f"week_{week.start.isoformat()}"
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs = {key: output_dir / name for key, name in OUTPUT_FILES.items()}
    for path in outputs.values():
        path.unlink(missing_ok=True)  # A failed run never leaves stale reports behind.

    # 1. Validate the catalog, the selected list, and every observation file before any write.
    try:
        target_master = validate_target_master(read_table_snapshot_csv(args.target_master))
        mapping = validate_source_model_map(
            read_table_snapshot_csv(args.source_model_map), target_master
        )
        review = validate_match_review(read_table_snapshot_csv(args.match_review))
        for name, table in (
            ("TARGET_MASTER", target_master),
            ("SOURCE_MODEL_MAP", mapping),
            ("MATCH_REVIEW", review),
        ):
            if not table.is_valid:
                raise PriceTrackingInputError(
                    f"{name} did not pass validation; run check_target_tables.py first."
                )
        target_master_sha256 = sha256_file(args.target_master)
        contributions = read_csv_rows(args.contributions, LEDGER_COLUMNS, "Contribution ledger")
        selected = validate_selected_list(
            read_csv_rows(args.selected_list, CONFIGURATION_COLUMNS, "Selected list"),
            contributions,
            target_master,
            target_master_sha256=target_master_sha256,
            list_sha256=sha256_file(args.selected_list),
        )
        use_sales = not args.no_sales_ranking and args.sales_ranking.is_file()
        sales_rows = (
            read_csv_rows(args.sales_ranking, SALES_RANKING_COLUMNS, "Sales ranking")
            if use_sales
            else None
        )
        ledger = build_source_ledger(selected, contributions, mapping, sales_rows)
        matcher = matcher_from_validated(target_master)
        context = matching_context(
            target_master,
            target_master_sha256=target_master_sha256,
            source_label=str(args.target_master),
        )
        batches = [
            (path, read_observations_csv(path), sha256_file(path)) for path in args.observations
        ]
        search_context = read_search_context(args.search_context)
    except (
        FileNotFoundError,
        UnicodeDecodeError,
        KeyError,
        ValueError,
        TargetMasterSchemaError,
        TargetSnapshotError,
        VerifiedRankingInputError,
        PriceTrackingInputError,
        ObservationFileError,
    ) as exc:
        print(f"Price tracking stopped before any store write: {exc}", file=sys.stderr)
        return 1

    # 2. Import (one transaction per file), match under the current context, then report.
    try:
        with ObservationStore.open(args.store) as store:
            before = store.counts()
            imports = [
                store.import_observations(
                    [item.observation for item in observations],
                    source_label=str(path),
                    imported_at=run_started,
                    source_sha256=digest,
                )
                for path, observations, digest in batches
            ]
            known_contexts = {row["context_id"] for row in store.contexts()}
            matched_now = match_stored_observations(store, context, matcher, now=run_started)
            stored = store.observations()
            matches = store.match_results(context.context_id)
            after = store.counts()
            contexts = store.contexts()
        result = build_price_tracking(
            stored,
            matches,
            selected,
            context,
            week=week,
            as_of=as_of,
            history_start=history_start,
            offer_terms_policy=args.offer_terms_policy,
            search_context=search_context,
        )
    except (
        ObservationConflictError,
        ObservationStoreError,
        PriceTrackingInputError,
        sqlite3.Error,
    ) as exc:
        print(f"Price tracking stopped: {exc}", file=sys.stderr)
        return 1

    write_csv(outputs["comparison"], COMPARISON_COLUMNS, result.comparison)
    write_csv(outputs["selections"], SELECTION_COLUMNS, result.selections)
    write_csv(outputs["series"], SERIES_COLUMNS, result.series)
    write_csv(outputs["history"], HISTORY_COLUMNS, result.history)
    write_csv(outputs["review"], REVIEW_COLUMNS, result.review)
    write_csv(outputs["ledger"], SOURCE_LEDGER_COLUMNS, ledger)

    status = week.status(as_of)
    inputs = {
        str(path): sha256_file(path)
        for path in (
            args.target_master,
            args.source_model_map,
            args.match_review,
            args.selected_list,
            args.contributions,
            *((args.sales_ranking,) if use_sales else ()),
            *args.observations,
            *args.search_context,
        )
    }
    summary = {
        "description": "Offline Phase 9 weekly price comparison and price history.",
        "run_started_at_utc": utc_text(run_started),
        "report_week": {
            "start": week.start.isoformat(),
            "end_exclusive": week.end.isoformat(),
            "timezone": REPORTING_TIMEZONE,
            "start_utc": utc_text(week.start_utc),
            "end_utc_exclusive": utc_text(week.end_utc),
            "status": status,
            "label": (
                "Historical: the week had ended at as_of; prices are the observations "
                "retrieved during that week, not current prices."
                if status == "CLOSED_HISTORICAL"
                else "In progress: provisional prices from observations so far."
            ),
        },
        "as_of_utc": utc_text(as_of),
        "history_start": history_start.isoformat() if history_start else "",
        "policy": {
            "currency": REPORTING_CURRENCY,
            "price_basis": "Trusted displayed price (REGULAR or SALE price type); vouchers and "
            "shipping are not observed and stay unknown.",
            "comparable": "EXACT match to a selected target, IN_STOCK or OUT_OF_STOCK, "
            "acceptable purchase terms, PHP, known price type. Stock status remains visible.",
            "offer_terms_policy": args.offer_terms_policy,
            "bundles": (
                "Ordinary offers with stated inclusions (Office, warranty, backpack, ...) are "
                "comparable with an INCLUDED_ITEMS_OFFER note. Paid packages, prices that "
                "require another purchase, restricted (financing, card, trade-in, member) "
                "prices, and bundle wording without evidence of the terms are excluded with "
                "their reason. No accessory value is estimated or deducted."
                if args.offer_terms_policy != BUNDLE_FLAG_POLICY else
                "Included at the displayed package price; no accessory value is estimated or "
                "deducted. Unknown bundle status remains excluded."
            ),
            "comparison_policy_version": (
                "displayed-price/stock-and-purchase-terms/2026-10-19"
                if args.offer_terms_policy != BUNDLE_FLAG_POLICY
                else "displayed-price/stock-and-bundles/2026-10-15"
            ),
            "unconfirmed_candidates": "A blank price may name an unconfirmed candidate offer "
            "(link, outcome, differing values); its price is never shown or used in minimums.",
            "representative_observation": "Latest observation of each offer within the week.",
            "selection": "Lowest comparable price per target and retailer; no averaging.",
            "tie_break": TIE_BREAK,
            "stale_observations": "Never carried forward; earlier weeks are shown only as stale "
            "references in the selections file.",
            "missing_price": "Blank cell with a status code; never zero.",
            "retention": "Observations, import batches, matching contexts, and results are "
            "append-only and kept indefinitely; reports are regenerated per run.",
        },
        "sales_snapshot": {
            "run_id": selected.sales_run_id,
            "extract_date": selected.sales_extract_date,
            "m_minus_1_month": selected.sales_m_minus_1_month,
            "mtd_month": selected.sales_mtd_month,
            "label": selected.gmv_period_label(),
            "note": "Shopee GMV (USD) and snapshot prices (PHP) are historical values from the "
            "sales extract; they are not current marketplace prices and are never mixed with "
            "retailer prices.",
        },
        "selected_list": {
            "configurations": len(selected.configurations),
            "sha256": selected.sha256,
            "built_on_target_master_sha256": selected.built_on_target_master_sha256,
            "catalog_matches_build": selected.catalog_matches_build,
            "source_ledger_rows": len(ledger),
            "ledger_sources_not_currently_mapped": sum(
                1 for row in ledger if row["currently_mapped_to_target"] != "True"
            ),
        },
        "matching_context": {
            "context_id": context.context_id,
            "target_master_sha256": context.target_master_sha256,
            "configuration_targets": context.configuration_targets,
            "listing_only_targets": context.listing_only_targets,
            "matching_rules_version": MATCHING_RULES_VERSION,
            "matching_rules_sha256": context.matching_rules_sha256,
            "new_in_this_run": context.context_id not in known_contexts,
        },
        "store": {
            "path": str(args.store),
            "before": before,
            "after": after,
            "imports": [
                {
                    "source": str(path),
                    "batch_id": report.batch_id,
                    "received": report.received,
                    "new": report.new,
                    "duplicates_in_store": report.duplicates_in_store,
                    "duplicates_in_batch": report.duplicates_in_batch,
                }
                for (path, _, _), report in zip(batches, imports, strict=True)
            ],
            "match_results_added": matched_now,
            "matching_contexts": contexts,
        },
        "input_sha256": inputs,
        "counts": result.counts,
        # Price eligibility of this week's exact matches under each offer-terms policy, with the
        # same matches: the commercial-terms effect, separate from identity matching.
        "offer_terms_effect": result.terms_effect,
        "outputs": {key: str(path) for key, path in outputs.items()},
    }
    outputs["summary"].write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    # Counts only: offer URLs, prices, and IDs stay in the local reports.
    counts = result.counts
    print(
        f"Report week {week.start.isoformat()} to {week.end.isoformat()} (exclusive, "
        f"{REPORTING_TIMEZONE}): {status}; as of {utc_text(as_of)}."
    )
    print(
        f"Shopee sales snapshot {selected.sales_run_id}: {selected.gmv_period_label()}. Not "
        "current marketplace data."
    )
    for report, (path, _, _) in zip(imports, batches, strict=True):
        print(
            f"Imported {path.name}: received {report.received}; new {report.new}; already "
            f"stored {report.duplicates_in_store}; repeated in file {report.duplicates_in_batch}."
        )
    added = after["observations"] - before["observations"]
    print(
        f"Store: {after['observations']} observations ({added} new), {after['offers']} offers, "
        f"{after['import_batches']} import batches, {after['matching_contexts']} matching "
        f"contexts; {matched_now} match results added."
    )
    print(
        f"Selected configurations: {counts['selected_configurations']}; with a comparable "
        f"price: {counts['selected_targets_with_comparable_price']}; without: "
        f"{counts['selected_targets_without_comparable_price']}."
    )
    print(
        f"Observations in week: {counts['observations_in_week']} "
        f"(EXACT {counts['week_match_EXACT']}, UNMATCHED {counts['week_match_UNMATCHED']}, "
        f"AMBIGUOUS {counts['week_match_AMBIGUOUS']}, REVIEW_REQUIRED "
        f"{counts['week_match_REVIEW_REQUIRED']}); EXACT outside the selected list: "
        f"{counts['week_exact_out_of_selected_list']}."
    )
    print(
        "Retailer prices selected: "
        + ", ".join(f"{code} {counts[f'prices_{code}']}" for code, _ in RETAILERS)
        + f"; review rows: {counts['review_rows']}; history rows: {len(result.history)}."
    )
    if not selected.catalog_matches_build:
        print(
            "Note: TARGET_MASTER differs from the catalog the selected list was built on; every "
            "selected identity was rechecked and is unchanged."
        )
    print(f"Reports written to {output_dir}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
