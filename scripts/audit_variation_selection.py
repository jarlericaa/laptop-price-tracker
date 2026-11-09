#!/usr/bin/env python3
"""Selection audit of the Top 100 eligible mapped Shopee variations (offline, read-only)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from laptop_tracker.selection_audit import audit_markdown, build_selection_audit, read_rows
from laptop_tracker.supporting_tables import validate_source_model_map
from laptop_tracker.target_master import TargetMasterSchemaError, validate_target_master
from laptop_tracker.variation_ranking import OUTPUT_FILES
from laptop_tracker.verified_top import VerifiedRankingInputError, load_catalog_tables


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Explain a build_top_shopee_variations.py run: the complete eligible pool and where it "
            "comes from, the qualifying mapped variations, exclusions by precise reason with "
            "representative evidence (MATCH_REVIEW shown beside it, never used to select), the "
            "catalog retrieval record and the catalog changes picked up. Local files only; never "
            "reads raw or Sheets and never writes anything but the audit files."
        )
    )
    parser.add_argument("--variation-dir", type=Path, required=True)
    parser.add_argument("--ranking", type=Path, required=True)
    parser.add_argument("--target-master", type=Path, required=True)
    parser.add_argument("--source-model-map", type=Path, required=True)
    parser.add_argument("--match-review", type=Path, required=True)
    parser.add_argument(
        "--previous-catalog-dir",
        type=Path,
        help="Folder with the earlier *_validated.csv catalog copies, to list what changed.",
    )
    parser.add_argument(
        "--compare-ranking",
        action="append",
        default=[],
        metavar="LABEL=PATH",
        help="An earlier eligible_model_ranking.csv, re-evaluated under the current catalog.",
    )
    parser.add_argument("--expect-spreadsheet-id", help="Fail unless the catalog came from it.")
    parser.add_argument("--expect-new-mappings", type=int)
    parser.add_argument("--expect-new-targets", type=int)
    parser.add_argument("--top-n", type=int, default=100)
    parser.add_argument("--output-dir", type=Path, help="Default: --variation-dir.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    paths = {key: args.variation_dir / name for key, name in OUTPUT_FILES.items()}
    try:
        comparisons = {}
        for item in args.compare_ranking:
            label, _, path = item.partition("=")
            comparisons[label] = Path(path)
        tables, _ = load_catalog_tables(
            {
                "TARGET_MASTER": args.target_master,
                "SOURCE_MODEL_MAP": args.source_model_map,
                "MATCH_REVIEW": args.match_review,
            }
        )
        target_master = validate_target_master(tables["TARGET_MASTER"])
        source_model_map = validate_source_model_map(tables["SOURCE_MODEL_MAP"], target_master)
        current = {
            "TARGET_MASTER": read_rows(args.target_master),
            "SOURCE_MODEL_MAP": read_rows(args.source_model_map),
        }
        expectations = {
            key: value
            for key, value in (
                ("spreadsheet_id", args.expect_spreadsheet_id),
                ("new_mappings", args.expect_new_mappings),
                ("new_targets", args.expect_new_targets),
            )
            if value is not None
        }
        audit = build_selection_audit(
            summary=json.loads(paths["summary"].read_text(encoding="utf-8")),
            ranked=read_rows(paths["ranking"]),
            top=read_rows(paths["top"]),
            spec_conflict_rows=read_rows(paths["spec_conflicts"]),
            review_rows=read_rows(args.match_review),
            ranking_path=args.ranking,
            current_catalog=current,
            target_master=target_master,
            source_model_map=source_model_map,
            previous_catalog_dir=args.previous_catalog_dir,
            comparisons=comparisons,
            top_n=args.top_n,
            expectations=expectations,
        )
    except (FileNotFoundError, KeyError, VerifiedRankingInputError, TargetMasterSchemaError) as exc:
        print(f"Selection audit stopped: {exc}", file=sys.stderr)
        return 1
    out = args.output_dir or args.variation_dir
    out.mkdir(parents=True, exist_ok=True)
    (out / "selection_audit.json").write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
    (out / "selection_audit.md").write_text(audit_markdown(audit), encoding="utf-8")
    sel = audit["selection"]
    print(
        f"Selection audit: eligible pool {sel['eligible_pool']}, qualifying "
        f"{sel['qualifying_mapped_variations']}, selected {sel['selected_variations']} of "
        f"{sel['requested_top_n']} (shortfall {sel['shortfall']}), "
        f"{sel['distinct_target_contexts']} target contexts. Catalog inputs: "
        f"{audit['catalog_inputs'].get('status')}. Written to {out}."
    )
    for check in audit["checks"]:
        print(
            f"  {'PASS' if check['passed'] else 'FAIL'}: {check['check']} (found {check['found']})"
        )
    return 0 if audit["checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
