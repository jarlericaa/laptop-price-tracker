"""Real matching/report selection through the existing publisher's offline Sheets API."""

import hashlib
import json
from dataclasses import replace
from decimal import Decimal

from test_color_storage_variants import page
from test_observation_store import T0
from test_price_tracking import AS_OF, TM_SHA, WEEK, ledger, list_rows
from test_sheet_publication import FakeSpreadsheet, build_reports, row_for, write_dicts
from test_target_master import target_values

from laptop_tracker.match_run import matcher_from_validated
from laptop_tracker.observation_store import ObservationStore
from laptop_tracker.price_tracking import (
    COMPARISON_COLUMNS,
    HISTORY_COLUMNS,
    REVIEW_COLUMNS,
    SELECTION_COLUMNS,
    build_price_tracking,
    match_stored_observations,
    matching_context,
    validate_selected_list,
)
from laptop_tracker.sheet_publication import (
    COMPARISON_TAB,
    build_plan,
    load_pilot_report,
    publish,
)
from laptop_tracker.target_master import TARGET_MASTER_HEADERS, validate_target_master
from laptop_tracker.verified_top import CONFIGURATION_COLUMNS


def test_bound_variants_produce_correct_separate_sheet_prices_and_verified_readback(tmp_path):
    targets = []
    for i, capacity in enumerate(("256", "512"), 1):
        values = dict.fromkeys(TARGET_MASTER_HEADERS, "") | {
            "target_id": f"TGT-00000{i}", "status": "ACTIVE", "brand": "Apple",
            "model": "MacBook Neo", "screen_size_in": "13", "cpu_series": "Apple A18 Pro",
            "ram_capacity_gb": "8", "storage_capacity_gb": capacity,
        }
        targets.append([values[key] for key in TARGET_MASTER_HEADERS])
    observations = [replace(page(i), price=Decimal(price), raw_price_text=price, retrieved_at=T0)
                    for i, price in ((0, "25000.00"), (4, "30000.00"))]
    world = build_reports(tmp_path / "publication")
    selected = list_rows(targets=targets)
    write_dicts(world["selected"], CONFIGURATION_COLUMNS, selected)
    write_dicts(world["scope"], CONFIGURATION_COLUMNS, selected)
    digest = hashlib.sha256(world["selected"].read_bytes()).hexdigest()
    tm = validate_target_master(target_values(*targets))
    checked = validate_selected_list(selected, ledger(), tm, target_master_sha256=TM_SHA,
                                     list_sha256=digest)
    context = matching_context(tm, target_master_sha256=TM_SHA, source_label="variant_flow")
    with ObservationStore.open(tmp_path / "actual.sqlite3") as store:
        store.import_observations(observations, source_label="variant_flow", imported_at=AS_OF)
        match_stored_observations(store, context, matcher_from_validated(tm), now=AS_OF)
        result = build_price_tracking(store.observations(), store.match_results(context.context_id),
                                      checked, context, week=WEEK, as_of=AS_OF)
    assert [row["PCC_Price"] for row in result.comparison] == ["25000.00", "30000.00"]
    assert [row["Storage_Capacity"] for row in result.comparison] == ["256", "512"]
    for filename, columns, rows in (
        ("weekly_price_comparison.csv", COMPARISON_COLUMNS, result.comparison),
        ("weekly_price_selections.csv", SELECTION_COLUMNS, result.selections),
        ("price_history.csv", HISTORY_COLUMNS, result.history),
        ("price_review_queue.csv", REVIEW_COLUMNS, result.review),
    ):
        write_dicts(world["report"] / filename, columns, rows)
    summary_path = world["report"] / "price_tracking_run_summary.json"
    summary = json.loads(summary_path.read_text())
    summary["selected_list"].update(configurations=2, sha256=digest)
    summary["input_sha256"] = {str(world["selected"]): digest}
    summary_path.write_text(json.dumps(summary))
    report = load_pilot_report(world["report"], world["scope"], selected_list=world["selected"])
    fake = FakeSpreadsheet({"raw": [["Protected input"]]})
    outcome = publish(fake, build_plan(report), published_at=AS_OF,
                      backup_dir=tmp_path / "backups")
    assert outcome.status == "SUCCESS"
    assert Decimal(row_for(fake, COMPARISON_TAB, "TGT-000001")["PC Central price (PHP)"]) == 25000
    assert Decimal(row_for(fake, COMPARISON_TAB, "TGT-000002")["PC Central price (PHP)"]) == 30000
    assert fake.sheets["raw"]["grid"][(0, 0)]["userEnteredValue"] == {
        "stringValue": "Protected input"}
