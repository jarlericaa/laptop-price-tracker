#!/usr/bin/env python3
"""Build the Top 100 Shopee variations ranking, its configuration contexts and audit outputs."""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from laptop_tracker.catalog_provenance import (
    DIRECT_READ,
    MANIFEST_NAME,
    describe_catalog_inputs,
    read_retrieval_manifest,
)
from laptop_tracker.sheets_source import SheetsSourceError
from laptop_tracker.supporting_tables import validate_match_review, validate_source_model_map
from laptop_tracker.target_master import TargetMasterSchemaError, validate_target_master
from laptop_tracker.variation_ranking import (
    LIST_NAME,
    OUTPUT_FILES,
    RANKING_GRAIN,
    SCHEMA_VERSION,
    SELECTION_POLICY,
    audit_variation_ranking,
    build_variation_ranking,
    rank_metric_scope,
    read_variation_snapshot,
    write_variation_outputs,
)
from laptop_tracker.verified_top import (
    DEFAULT_TOP_N,
    VerifiedRankingInputError,
    check_source_top_100,
    load_catalog_tables,
    read_csv_rows,
    read_register,
    sha256_file,
)

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "output"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Select the Top 100 eligible mapped Shopee variations (owner decision 2026-11-03). "
            "Every eligible variation is resolved against TARGET_MASTER / SOURCE_MODEL_MAP and "
            "only MAPPED_VERIFIED ones (one active, complete, non-LISTING_ONLY configuration "
            "target, no open conflict) qualify. That filter runs before the limit: the qualifying "
            "pool is ranked by each variation's own ranking_gmv_usd (the Phase 1 metric: M-1 plus "
            "MTD, deterministic tie-break) and the first 100 are selected, so lower-ranked "
            "qualifying variations fill places left by unmapped ones; nothing is padded. The full "
            "eligible ranking and the exclusions by mapping failure stay in the outputs for "
            "audit. Distinct targets of the selected rows become comparison contexts, written in "
            "the formats discover_products.py and track_prices.py already read, so a context is "
            "searched and observed once however many variations share it. Reads local CSVs or, "
            "with --from-sheets, the catalog tabs read-only. Never reads raw, never writes "
            "Sheets, never maps or promotes rows."
        )
    )
    parser.add_argument("--ranking", type=Path, default=OUTPUT / "eligible_model_ranking.csv")
    parser.add_argument("--source-top-100", type=Path, default=OUTPUT / "top_100_targets.csv")
    parser.add_argument(
        "--target-master", type=Path, default=OUTPUT / "target_master_validated.csv"
    )
    parser.add_argument(
        "--source-model-map", type=Path, default=OUTPUT / "source_model_map_validated.csv"
    )
    parser.add_argument("--match-review", type=Path, default=OUTPUT / "match_review_validated.csv")
    parser.add_argument("--from-sheets", action="store_true")
    parser.add_argument(
        "--catalog-manifest",
        type=Path,
        help=f"{MANIFEST_NAME} written by check_target_tables.py (default: beside "
        "--target-master). Records when and from where the catalog was retrieved.",
    )
    parser.add_argument(
        "--identity-register", type=Path, default=ROOT / "config" / "identity_review_register.csv"
    )
    parser.add_argument(
        "--shop-names",
        type=Path,
        help="source_shop_names.csv written by the sales refresh (default: beside --ranking).",
    )
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT / "shopee_variations")
    return parser.parse_args(argv)


def read_shop_names(path: Path | None) -> dict[str, str]:
    if path is None or not path.is_file():
        return {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return {row["source_key"]: row["shop_name"] for row in csv.DictReader(handle)}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name in OUTPUT_FILES.values():
        (args.output_dir / name).unlink(missing_ok=True)  # A failed run leaves no stale results.

    shop_names_path = args.shop_names or args.ranking.parent / "source_shop_names.csv"
    try:
        snapshot = read_variation_snapshot(args.ranking)
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
        catalog_read_at = datetime.now(UTC)
        tables, catalog_fingerprints = load_catalog_tables(
            {
                "TARGET_MASTER": args.target_master,
                "SOURCE_MODEL_MAP": args.source_model_map,
                "MATCH_REVIEW": args.match_review,
            },
            from_sheets=args.from_sheets,
        )
        target_master = validate_target_master(tables["TARGET_MASTER"])
        source_model_map = validate_source_model_map(tables["SOURCE_MODEL_MAP"], target_master)
        validate_match_review(tables["MATCH_REVIEW"])  # Read for provenance only; never a gate.
        if args.from_sheets:
            catalog_inputs = {
                "status": DIRECT_READ,
                "detail": "The three tabs were read from Google Sheets (read-only) by this build.",
                "retrieved_at_utc": catalog_read_at.isoformat(timespec="seconds"),
                "tab_values_sha256": {n: v[1] for n, v in catalog_fingerprints.items()},
            }
        else:
            catalog_inputs = describe_catalog_inputs(
                {
                    "TARGET_MASTER": args.target_master,
                    "SOURCE_MODEL_MAP": args.source_model_map,
                    "MATCH_REVIEW": args.match_review,
                },
                read_retrieval_manifest(
                    args.catalog_manifest or args.target_master.parent / MANIFEST_NAME
                ),
            )
        register = read_register(args.identity_register)
        result = build_variation_ranking(
            snapshot,
            target_master,
            source_model_map,
            source_top_100_ids=original_ids,
            register=register,
            top_n=args.top_n,
            target_master_sha256=catalog_fingerprints["TARGET_MASTER"][1],
            shop_names=read_shop_names(shop_names_path),
        )
        issues = audit_variation_ranking(
            result.ranked,
            result.top,
            result.context_list,
            result.context_ledger,
            snapshot,
            target_master,
            source_model_map,
            top_n=args.top_n,
            register=register,
        )
    except (
        FileNotFoundError,
        UnicodeDecodeError,
        KeyError,
        SheetsSourceError,
        TargetMasterSchemaError,
        VerifiedRankingInputError,
    ) as exc:
        print(f"Shopee variation ranking stopped: {exc}", file=sys.stderr)
        return 1
    if issues:
        print(
            "Shopee variation ranking failed its own audit; nothing was written:", file=sys.stderr
        )
        for issue in issues[:20]:
            print(f"  - {issue}", file=sys.stderr)
        return 1

    fingerprints = {
        str(args.ranking): sha256_file(args.ranking),
        **({str(args.source_top_100): sha256_file(args.source_top_100)} if original_ids else {}),
        **dict(catalog_fingerprints.values()),
        **(
            {str(args.identity_register): sha256_file(args.identity_register)}
            if args.identity_register.is_file()
            else {}
        ),
        **(
            {str(shop_names_path): sha256_file(shop_names_path)}
            if shop_names_path.is_file()
            else {}
        ),
    }
    summary = {
        "list_name": LIST_NAME,
        "schema_version": SCHEMA_VERSION,
        "ranking_grain": RANKING_GRAIN,
        "selection_policy": SELECTION_POLICY,
        "policy": (
            "Owner decisions 2026-10-27 and 2026-11-03: one row per eligible Shopee seller "
            "variation (source_key), ranked by its own ranking_gmv_usd (Phase 1 metric: M-1 + "
            "MTD). Only MAPPED_VERIFIED variations qualify, and that filter runs before the "
            "100-row limit; MATCH_REVIEW completion and retailer coverage do not gate selection. "
            "Configuration contexts are built after the selection."
        ),
        "rank_metric": "ranking_gmv_usd",
        "rank_metric_scope": rank_metric_scope(snapshot),
        "built_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "sales_snapshot": {
            "run_id": snapshot.run_id,
            "extract_date": snapshot.extract_date,
            "m_minus_1_month": snapshot.m_minus_1_month,
            "mtd_month": snapshot.mtd_month,
            "labels": snapshot.period_labels(),
        },
        "catalog_inputs": catalog_inputs,
        "catalog_source": "Google Sheets (read-only)"
        if args.from_sheets
        else "local CSV snapshots",
        "input_sha256": fingerprints,
        "counts": result.counts,
        "outputs": {key: str(args.output_dir / name) for key, name in OUTPUT_FILES.items()},
    }
    write_variation_outputs(args.output_dir, result, summary)

    c = result.counts
    print(
        f"Catalog inputs: {catalog_inputs['status']}"
        + (
            f" (retrieved {catalog_inputs['retrieved_at_utc']})"
            if catalog_inputs.get("retrieved_at_utc")
            else ""
        )
        + f". {catalog_inputs.get('detail', '')}"
    )
    print(f"Sales snapshot {snapshot.run_id}: {rank_metric_scope(snapshot)}.")
    print(
        f"{LIST_NAME}: {c['eligible_variations']} eligible variations, "
        f"{c['eligible_variations_with_verified_mapping']} with a verified mapping, "
        f"{c['excluded_from_top_consideration']} excluded from consideration."
    )
    for reason, count in c["excluded_by_mapping_failure"].items():
        print(f"  excluded {reason}: {count}")
    print(
        f"Selected: {c['selected_variations']} of {c['requested_top_n']} (shortfall "
        f"{c['shortfall']}); {c['selected_with_lower_eligible_rank_than_top_n']} selected from "
        f"eligible rank beyond {c['requested_top_n']}; distinct configuration contexts: "
        f"{c['distinct_configuration_contexts']}."
    )
    if c["shortfall"]:
        print(f"Shortfall: {c['shortfall_reason']}.")
    print(
        f"Own Shopee snapshot price: {c['selected_with_own_price']} of {c['selected_variations']} "
        f"selected rows. Audit passed. Outputs in {args.output_dir}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
