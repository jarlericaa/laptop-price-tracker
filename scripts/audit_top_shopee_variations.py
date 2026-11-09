#!/usr/bin/env python3
"""Audit the Top 100 Shopee variations and report the before/after grain change (offline)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from laptop_tracker.supporting_tables import validate_source_model_map
from laptop_tracker.target_master import TargetMasterSchemaError, validate_target_master
from laptop_tracker.variation_ranking import (
    CONTEXT_COLUMNS,
    EXCLUSION_COLUMNS,
    LIST_NAME,
    OUTPUT_FILES,
    SELECTION_POLICY,
    VARIATION_COLUMNS,
    audit_variation_ranking,
    build_variation_ranking,
    read_variation_snapshot,
)
from laptop_tracker.verified_top import (
    CONFIGURATION_COLUMNS,
    LEDGER_COLUMNS,
    VerifiedRankingInputError,
    load_catalog_tables,
    read_csv_rows,
    read_register,
    sha256_file,
)

ROOT = Path(__file__).resolve().parents[1]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Independently audit a build_top_shopee_variations.py run: the written files must "
            "equal a fresh rebuild from the same inputs, every row must match its own sales row, "
            "and the context list must fit the existing retailer layer. With --legacy-dir, also "
            "write a before/after grain audit against the configuration-level verified list."
        )
    )
    parser.add_argument("--variation-dir", type=Path, required=True)
    parser.add_argument("--ranking", type=Path, required=True)
    parser.add_argument("--target-master", type=Path, required=True)
    parser.add_argument("--source-model-map", type=Path, required=True)
    parser.add_argument("--match-review", type=Path, required=True)
    parser.add_argument(
        "--identity-register", type=Path, default=ROOT / "config" / "identity_review_register.csv"
    )
    parser.add_argument(
        "--legacy-dir",
        type=Path,
        help="verified_top_100 folder of the same run (the configuration grain).",
    )
    parser.add_argument("--top-n", type=int, default=100)
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Where to write grain_audit.json / grain_audit.md (default: --variation-dir).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    paths = {key: args.variation_dir / name for key, name in OUTPUT_FILES.items()}
    try:
        snapshot = read_variation_snapshot(args.ranking)
        tables, fingerprints = load_catalog_tables(
            {
                "TARGET_MASTER": args.target_master,
                "SOURCE_MODEL_MAP": args.source_model_map,
                "MATCH_REVIEW": args.match_review,
            }
        )
        target_master = validate_target_master(tables["TARGET_MASTER"])
        source_model_map = validate_source_model_map(tables["SOURCE_MODEL_MAP"], target_master)
        written = {
            "ranked": read_csv_rows(paths["ranking"], VARIATION_COLUMNS, "Variation ranking"),
            "top": read_csv_rows(paths["top"], VARIATION_COLUMNS, "Selected variations"),
            "exclusions": read_csv_rows(
                paths["exclusions"], EXCLUSION_COLUMNS, "Mapping exclusions"
            ),
            "contexts": read_csv_rows(paths["contexts"], CONTEXT_COLUMNS, "Contexts"),
            "list": read_csv_rows(paths["context_list"], CONFIGURATION_COLUMNS, "Context list"),
            "ledger": read_csv_rows(paths["context_ledger"], LEDGER_COLUMNS, "Context ledger"),
        }
        summary = json.loads(paths["summary"].read_text(encoding="utf-8"))
        original_ids = set()
        top_path = args.ranking.parent / "top_100_targets.csv"
        if top_path.is_file():
            original_ids = {r["model_id"] for r in read_csv_rows(top_path, ("model_id",), "Top")}
        shop_names = {}
        names_path = args.ranking.parent / "source_shop_names.csv"
        if names_path.is_file():
            shop_names = {
                r["source_key"]: r["shop_name"]
                for r in read_csv_rows(names_path, ("source_key", "shop_name"), "Shop names")
            }
        register = read_register(args.identity_register)
        rebuilt = build_variation_ranking(
            snapshot,
            target_master,
            source_model_map,
            source_top_100_ids=original_ids,
            register=register,
            top_n=args.top_n,
            target_master_sha256=fingerprints["TARGET_MASTER"][1],
            shop_names=shop_names,
        )
    except (
        FileNotFoundError,
        KeyError,
        VerifiedRankingInputError,
        TargetMasterSchemaError,
        ValueError,
    ) as exc:
        print(f"Audit stopped: {exc}", file=sys.stderr)
        return 1

    issues = audit_variation_ranking(
        written["ranked"],
        written["top"],
        written["list"],
        written["ledger"],
        snapshot,
        target_master,
        source_model_map,
        top_n=args.top_n,
        register=register,
    )
    for name, rows, fresh in (
        ("ranking", written["ranked"], rebuilt.ranked),
        ("selected list", written["top"], rebuilt.top),
        ("mapping exclusions", written["exclusions"], rebuilt.exclusions),
        ("contexts", written["contexts"], rebuilt.contexts),
        ("context list", written["list"], rebuilt.context_list),
        ("context ledger", written["ledger"], rebuilt.context_ledger),
    ):
        if [dict(r) for r in rows] != [dict(r) for r in fresh]:
            issues.append(f"The written {name} differs from a fresh rebuild of the same inputs.")
    if summary.get("schema_version") != "shopee-variation-sheets-1":
        issues.append("The run summary is not schema shopee-variation-sheets-1.")
    if summary.get("selection_policy") != SELECTION_POLICY:
        issues.append(f"The run summary was not built under the {SELECTION_POLICY} policy.")
    if summary.get("input_sha256", {}).get(str(args.ranking)) not in (
        None,
        sha256_file(args.ranking),
    ):
        issues.append("The sales ranking changed since the run was built.")

    counts = rebuilt.counts
    legacy = _legacy_audit(args.legacy_dir, snapshot, counts) if args.legacy_dir else None
    report = {
        "schema_version": "shopee-variation-sheets-1",
        "audit_passed": not issues,
        "issues": issues,
        "after": {
            "ranking_grain": "SHOPEE_VARIATION",
            "list_name": LIST_NAME,
            "eligible_variations": counts["eligible_variations"],
            "eligible_variations_with_verified_mapping": counts[
                "eligible_variations_with_verified_mapping"
            ],
            "excluded_from_top_consideration": counts["excluded_from_top_consideration"],
            "excluded_by_mapping_failure": counts["excluded_by_mapping_failure"],
            "selected_with_lower_eligible_rank_than_top_n": counts[
                "selected_with_lower_eligible_rank_than_top_n"
            ],
            "shortfall_reason": counts["shortfall_reason"],
            "selected_variations": counts["selected_variations"],
            "distinct_configuration_contexts": counts["distinct_configuration_contexts"],
            "mapped_selected_variations": counts["mapped_selected_variations"],
            "unmapped_selected_variations": counts["unmapped_selected_variations"],
            "selected_without_trusted_context": counts["selected_without_context"],
            "selected_with_own_price": counts["selected_with_own_price"],
            "selected_by_mapping_status": counts["selected_by_mapping_status"],
            "selected_by_comparison_status": counts["selected_by_comparison_status"],
            "contexts_with_several_variations": counts["contexts_with_several_variations"],
            "shortfall": counts["shortfall"],
        },
        "before": legacy,
    }
    out = args.output_dir or args.variation_dir
    out.mkdir(parents=True, exist_ok=True)
    (out / "grain_audit.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (out / "grain_audit.md").write_text(_markdown(report), encoding="utf-8")
    print(
        f"Variation audit {'PASSED' if not issues else 'FAILED'}; "
        f"{counts['selected_variations']} variations, "
        f"{counts['distinct_configuration_contexts']} contexts. Written to {out}."
    )
    for issue in issues[:20]:
        print(f"  - {issue}", file=sys.stderr)
    return 0 if not issues else 1


def _legacy_audit(legacy_dir: Path, snapshot, counts: dict) -> dict:
    top = read_csv_rows(
        legacy_dir / "top_100_verified_configurations.csv", CONFIGURATION_COLUMNS, "Legacy top"
    )
    ledger = read_csv_rows(
        legacy_dir / "verified_configuration_contributions.csv", LEDGER_COLUMNS, "Legacy ledger"
    )
    contributing = [r for r in ledger if r["contribution_status"] == "CONTRIBUTING"]
    eligible = sum(1 for r in snapshot.rows if r.values["eligibility_status"] == "ELIGIBLE")
    return {
        "ranking_grain": "TARGET_CONFIGURATION",
        "rows": len(top),
        "distinct_target_ids": len({r["target_id"] for r in top}),
        "contributing_source_variations": len(contributing),
        "eligible_source_variations": eligible,
        "eligible_variations_not_represented": eligible - len(contributing),
        "unmapped_or_untrusted_eligible_variations": sum(
            1
            for r in ledger
            if r["contribution_status"] == "EXCLUDED"
            and r["reason_code"] not in {"SOURCE_NOT_ELIGIBLE"}
        ),
        "shortfall_below_100": max(0, 100 - len(top)),
        "rows_with_several_sources": sum(1 for r in top if int(r["contributing_source_count"]) > 1),
        "note": "A configuration row summed all its variations' GMV and carried one configuration "
        "level Shopee minimum; unmapped variations could not be ranked at all.",
        "variation_rows_per_legacy_row_selected": round(
            counts["selected_variations"] / max(len(top), 1), 2
        ),
    }


def _markdown(report: dict) -> str:
    after, before = report["after"], report["before"]
    lines = [
        "# Before / after grain audit",
        "",
        f"Audit {'passed' if report['audit_passed'] else 'FAILED'}.",
        "",
    ]
    if before:
        lines += ["## Before: configuration grain (TARGET_CONFIGURATION)", ""]
        lines += [f"- {k}: {v}" for k, v in before.items()]
        lines.append("")
    lines += ["## After: Shopee variation grain (SHOPEE_VARIATION)", ""]
    lines += [f"- {k}: {v}" for k, v in after.items()]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
