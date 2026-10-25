"""Compare saved web results with all bounded native query tiers, without matching.

This deliberately gives native search every planned tier, even if the production
search job would stop early. It is a discovery benchmark, not a Top 100 rerun.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from discover_web_leads import native_search, write_reports
from laptop_tracker.product_names import default_registry
from laptop_tracker.retailer_search import SEARCH_METHODS, search_queries
from laptop_tracker.scraping import Fetcher
from laptop_tracker.web_discovery import response_record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args(argv)
    run = copy.deepcopy(json.loads((args.run / "run.json").read_text(encoding="utf-8")))
    if not 1 <= len(run["cases"]) <= 100:
        parser.error("The saved cohort must contain between 1 and 100 cases")
    if args.output.exists() and any(args.output.iterdir()):
        parser.error("Output must be empty")
    args.output.mkdir(parents=True, exist_ok=True)
    plans = {}
    for case in run["cases"]:
        amounts = re.findall(r"(\d+)\s*(GB|TB)", case.get("specs", ""), re.I)
        core = {}
        if len(amounts) == 2:
            core = {key: str(int(value) * (1000 if unit.upper() == "TB" else 1))
                    for key, (value, unit) in zip(
                        ("ram_capacity_gb", "storage_capacity_gb"), amounts, strict=True)}
        planned = search_queries(
            case["case_id"], case["retailer_id"], model=case["model"], part_number="",
            brand=case.get("brand", ""), series=case.get("family", ""), core=core,
            kind=default_registry().kind(case.get("brand", ""), case["model"]),
        )
        plans[case["case_id"]] = list(dict.fromkeys(query.text for query in planned))[:7]
    (args.output / "native_plan.json").write_text(json.dumps(plans, indent=2) + "\n")
    if not args.live:
        print(f"Preview: {sum(map(len, plans.values()))} native query invocations; no requests.")
        return 0
    # Preserve response bytes and observation times for self-contained offline replay.
    for record in run["pages"].values():
        if record.get("body_file"):
            body_file = Path(record["body_file"])
            if body_file.is_absolute() or ".." in body_file.parts:
                raise ValueError("Unsafe saved response path")
            shutil.copyfile(args.run / body_file, args.output / body_file)
    run["native_expanded_started_at"] = datetime.now(UTC).isoformat()
    run["original_evidence"] = str(args.run.resolve())
    fetcher = Fetcher()
    for case in run["cases"]:
        records = []
        for words in plans[case["case_id"]]:
            query = f"site:{SEARCH_METHODS[case['retailer_id']].host} {words}"
            records.append(response_record(native_search(query, case["retailer_id"], fetcher)))
        run["searches"][case["case_id"]]["native"] = records
        (args.output / "run.json").write_text(json.dumps(run, indent=2) + "\n")
        print(f"Completed {case['case_id']}: {len(records)} native tiers", flush=True)
    run["native_expanded_finished_at"] = datetime.now(UTC).isoformat()
    (args.output / "run.json").write_text(json.dumps(run, indent=2) + "\n")
    summary = write_reports(args.output, run)
    print(json.dumps({k: v for k, v in summary.items() if k != "per_case"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
