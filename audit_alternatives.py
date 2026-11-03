#!/usr/bin/env python3
"""Offline audit of every close alternative in a weekly report against its field-level evidence.

For each row of ``weekly_price_alternatives.csv`` the saved observation is loaded from the
history store (read-only), re-extracted, and compared with the report independently of the
alternative evaluator: the retailer's stated values, the target's catalog values, the count and
set of differing dimensions (at most two), the price, stock and observation identity, the
observation time (inside the report week and no later than the cutoff), and the source of each
value (the page's specification or its title). Prints counts only; writes one audit row per
alternative. Exit status 1 when any row fails.

    python audit_alternatives.py --report-dir <reports> --store <history.sqlite3> \\
        --target-master <target_master_validated.csv> --output-dir <folder>

Run it on a disposable copy of the history store: nothing is fetched, imported, matched or
written to Sheets.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from laptop_tracker.alternatives import MAX_DIFFERENCES, alternative_tag  # noqa: E402
from laptop_tracker.conventions import (  # noqa: E402
    canonical_gpu_models,
    is_model_code,
    processor_models,
)
from laptop_tracker.extraction import ExtractionStatus, extract_offer_attributes  # noqa: E402
from laptop_tracker.normalization import text_key  # noqa: E402
from laptop_tracker.observation_store import ObservationStore  # noqa: E402

AUDIT_COLUMNS = (
    "target_id", "retailer_code", "observation_id", "alternative_tag", "difference_count",
    "differing_dimensions", "audit_result", "audit_findings", "title_supplied_dimensions",
    "specification_dimensions", "offer_url", "price", "availability", "retrieved_at_utc",
)
FIELDS = {
    "model": ("model", "target_model", "offer_model"),
    "screen": ("screen_size_in", "target_screen_in", "offer_screen_in"),
    "cpu": ("cpu_series", "target_cpu_series", "offer_cpu_series"),
    "ram": ("ram_capacity_gb", "target_ram_gb", "offer_ram_gb"),
    "storage": ("storage_capacity_gb", "target_storage_gb", "offer_storage_gb"),
    "gpu": ("gpu_model", "target_gpu", "offer_gpu"),
}


def gpu_key(value: str) -> str:
    forms = canonical_gpu_models(value) if value else set()
    return forms.pop().casefold() if len(forms) == 1 else (text_key(value) or "")


def independent_differences(target: dict[str, str], offer: dict[str, str]) -> tuple[set, set]:
    """``(differing, unknown)`` dimensions recomputed from stated values only."""
    differing: set[str] = set()
    unknown: set[str] = set()
    if not offer.get("model") or not target.get("model"):
        unknown.add("model")
    elif text_key(offer["model"]) != text_key(target["model"]):
        differing.add("model")
    for dimension, name in (("screen", "screen_size_in"), ("ram", "ram_capacity_gb"),
                            ("storage", "storage_capacity_gb")):
        if not offer.get(name) or not target.get(name):
            unknown.add(dimension)
        elif Decimal(offer[name]) != Decimal(target[name]):
            differing.add(dimension)
    if not offer.get("cpu_series") or not target.get("cpu_series"):
        unknown.add("cpu")
    elif text_key(offer["cpu_series"]) != text_key(target["cpu_series"]):
        differing.add("cpu")
    else:
        mine = {m.key for m in processor_models(offer.get("cpu_model", ""), bare_apple=True)}
        theirs = {m.key for m in processor_models(target.get("cpu_model", ""), bare_apple=True)}
        if len(mine) == 1 and len(theirs) == 1 and mine != theirs:
            differing.add("cpu")
    if not offer.get("gpu_model") or not target.get("gpu_model"):
        unknown.add("gpu")
    elif gpu_key(offer["gpu_model"]) != gpu_key(target["gpu_model"]):
        differing.add("gpu")
    return differing, unknown


def audit(report_dir: Path, store_path: Path, target_master: Path) -> list[dict[str, str]]:
    alternatives = list(csv.DictReader((report_dir / "weekly_price_alternatives.csv").open(
        newline="", encoding="utf-8")))
    summary = json.loads((report_dir / "price_tracking_run_summary.json").read_text("utf-8"))
    week_start, week_end = summary["report_week"]["start_utc"], summary["report_week"][
        "end_utc_exclusive"]
    as_of = summary["as_of_utc"]
    targets = {row["target_id"]: row for row in csv.DictReader(
        target_master.open(newline="", encoding="utf-8"))}
    with ObservationStore.open(store_path) as store:
        stored = {s.observation_id: s for s in store.observations()}
    rows = []
    for row in alternatives:
        findings: list[str] = []
        observation = stored.get(row["observation_id"])
        target = targets.get(row["target_id"])
        title_dims: list[str] = []
        spec_dims: list[str] = []
        if observation is None:
            findings.append("observation not in the store")
        if target is None:
            findings.append("target not in the catalog")
        if observation is not None and target is not None:
            extracted = extract_offer_attributes(observation.observation())
            values = dict(extracted.values)
            if (observation.offer_url, observation.price_text, observation.availability) != (
                    row["offer_url"], row["price"], row["availability"]):
                findings.append("offer url, price or stock differs from the stored observation")
            if not (week_start <= row["retrieved_at_utc"] < week_end
                    and row["retrieved_at_utc"] <= as_of):
                findings.append("observation time outside the report week or after the cutoff")
            codes = set(extracted.reason_codes)
            offer = {}
            for dimension, (name, target_col, offer_col) in FIELDS.items():
                if row[offer_col] != values.get(name, ""):
                    findings.append(f"{dimension}: report states {row[offer_col]!r}, the page "
                                    f"extracts {values.get(name, '')!r}")
                target_value = target.get(name, "")
                if dimension != "model" and row[target_col] != target_value:
                    findings.append(f"{dimension}: report target {row[target_col]!r} differs "
                                    f"from the catalog {target_value!r}")
                if values.get(name):
                    supplied = f"TITLE_SUPPLIED_{name.upper()}" in codes
                    (title_dims if supplied else spec_dims).append(dimension)
                offer[name] = values.get(name, "")
            offer["cpu_model"] = values.get("cpu_model", "")
            target_values = {name: target.get(name, "") for name in (
                "model", "screen_size_in", "cpu_series", "cpu_model", "ram_capacity_gb",
                "storage_capacity_gb", "gpu_model")}
            # The target's code: its model when that is one manufacturer code, else the code the
            # catalog records for it. A broad family name states none: compared at family level.
            code = next((v for v in (target.get("model", ""),
                                     target.get("manufacturer_model_code", ""))
                         if v and is_model_code(v)), "")
            from laptop_tracker.product_names import FAMILY_NAME, default_registry
            if not code and default_registry().kind(
                    target.get("brand", ""), target.get("model", "")) == FAMILY_NAME:
                target_values["model"] = offer.get("model", "")
            elif code:
                target_values["model"] = code
                if not offer.get("model") or not is_model_code(offer["model"]):
                    offer["model"] = ""
            elif not offer.get("model"):
                offer["model"] = ""
            differing, unknown = independent_differences(target_values, offer)
            reported = {d for d in row["differing_dimensions"].split(";") if d}
            if differing != reported:
                findings.append(f"differing dimensions: independent {sorted(differing)} vs "
                                f"report {sorted(reported)}")
            if not 1 <= len(differing) <= MAX_DIFFERENCES:
                findings.append(f"{len(differing)} differences are outside the policy")
            if row["difference_count"] != str(len(reported)):
                findings.append("difference count disagrees with the differing dimensions")
            if row["alternative_tag"] != alternative_tag(len(reported), reported):
                findings.append("tag disagrees with the differing dimensions")
            for dimension in unknown & {"screen", "cpu", "ram", "storage"}:
                findings.append(f"required dimension {dimension} is not established")
            if extracted.evidence and any(
                    item.status is ExtractionStatus.AMBIGUOUS and item.field in {
                        v[0] for v in FIELDS.values()} for item in extracted.evidence):
                findings.append("the page's own evidence is contradictory for a compared field")
        rows.append({
            "target_id": row["target_id"], "retailer_code": row["retailer_code"],
            "observation_id": row["observation_id"], "alternative_tag": row["alternative_tag"],
            "difference_count": row["difference_count"],
            "differing_dimensions": row["differing_dimensions"],
            "audit_result": "PASS" if not findings else "FAIL",
            "audit_findings": " | ".join(findings),
            "title_supplied_dimensions": ";".join(sorted(set(title_dims))),
            "specification_dimensions": ";".join(sorted(set(spec_dims))),
            "offer_url": row["offer_url"], "price": row["price"],
            "availability": row["availability"], "retrieved_at_utc": row["retrieved_at_utc"],
        })
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--target-master", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    rows = audit(args.report_dir, args.store, args.target_master)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with (args.output_dir / "alternative_audit.csv").open("w", newline="", encoding="utf-8") as h:
        writer = csv.DictWriter(h, AUDIT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    results = Counter(row["audit_result"] for row in rows)
    print(f"Audited {len(rows)} alternative association(s): PASS {results['PASS']}, "
          f"FAIL {results['FAIL']}. Tags: " + ", ".join(
              f"{tag} {count}" for tag, count in sorted(
                  Counter(row["alternative_tag"] for row in rows).items())))
    for row in rows:
        if row["audit_result"] == "FAIL":
            print(f"  FAIL {row['target_id']} {row['retailer_code']} "
                  f"{row['observation_id'][:12]}: {row['audit_findings']}")
    return 0 if not results["FAIL"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
