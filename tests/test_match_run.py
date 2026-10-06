"""End-to-end offline matching: adapter fixtures -> observation CSV -> match_offers.py reports."""

from __future__ import annotations

import csv
import importlib.util
from pathlib import Path

import pytest
from scraping_fakes import no_network  # noqa: F401
from test_extraction import fixture_offer
from test_target_master import target_row, target_values

from laptop_tracker.match_run import MATCH_EVIDENCE_COLUMNS, MATCH_RESULT_COLUMNS
from laptop_tracker.offers import (
    OBSERVATION_COLUMNS,
    OFFER_DETAIL_COLUMNS,
    ObservationValidationError,
    observation_from_record,
)

pytestmark = pytest.mark.usefixtures("no_network")

ROOT = Path(__file__).resolve().parents[1]
FULL_COLUMNS = [*OBSERVATION_COLUMNS, *OFFER_DETAIL_COLUMNS]

# Synthetic targets for synthetic fixtures. TGT-000002 states a refresh rate that conflicts
# with the PC Central fixture's 165Hz; TGT-000003 is retired.
LF_TARGET = {
    "target_id": "TGT-000001",
    "brand": "",
    "model": "Acer Aspire AI 14 A14-11M-X3BP (Steam Blue)",
    "screen_size_in": "14",
    "cpu_series": "Snapdragon X",
    "ram_capacity_gb": "16",
    "storage_capacity_gb": "512",
    "resolution": "1920x1200",
    "refresh_rate_hz": "",
    "gpu_model": "",
    "os": "",
}
TARGET_ROWS = [
    target_row(**LF_TARGET),
    target_row(
        target_id="TGT-000002",
        model="Example Nitro 15 EN15-0002",
        screen_size_in="15.6",
        cpu_series="Core 5",
        ram_capacity_gb="16",
        storage_capacity_gb="512",
        resolution="",
        refresh_rate_hz="144",
    ),
    target_row(
        target_id="TGT-000003",
        status="INACTIVE",
        model="Example Notebook 15 EN15-01",
        screen_size_in="15.6",
        cpu_series="Core 5",
        ram_capacity_gb="8",
        storage_capacity_gb="256",
    ),
]


def load_cli():
    spec = importlib.util.spec_from_file_location("match_offers", ROOT / "match_offers.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_rows(path: Path, rows: list[list[str]]) -> Path:
    with path.open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows(rows)
    return path


def write_observations(path: Path, *names: str) -> Path:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FULL_COLUMNS)
        writer.writeheader()
        for name in names:
            writer.writerow(fixture_offer(name).to_full_record())
    return path


def read_dicts(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


@pytest.mark.parametrize(
    "name", ["lf_sale", "dbz_bundle", "oct_variants", "pcc_single", "pcx_regular"]
)
def test_observation_records_round_trip(name: str) -> None:
    offer = fixture_offer(name)
    assert observation_from_record(offer.to_full_record()) == offer


def test_observation_without_details_round_trips_to_none() -> None:
    offer = fixture_offer("lf_sale")
    record = offer.to_record()
    rebuilt = observation_from_record(record)
    assert rebuilt.details is None
    assert rebuilt.to_record() == record


@pytest.mark.parametrize(
    ("column", "value", "field"),
    [
        ("price", "abc", "price"),
        ("retrieved_at", "yesterday", "retrieved_at"),
        ("availability", "MAYBE", "availability"),
        ("raw_specifications", "{}", "raw_specifications"),
        ("bundle_flag", "yes", "bundle_flag"),
        ("retailer_id", "Bad Retailer", "retailer_id"),
    ],
)
def test_malformed_observation_records_are_rejected(column: str, value: str, field: str) -> None:
    record = fixture_offer("lf_sale").to_full_record() | {column: value}
    with pytest.raises(ObservationValidationError) as raised:
        observation_from_record(record)
    assert raised.value.field_name == field
    assert value not in str(raised.value)


@pytest.mark.parametrize(
    ("template", "columns"),
    [
        ("offer_match_results_template.csv", MATCH_RESULT_COLUMNS),
        ("offer_match_evidence_template.csv", MATCH_EVIDENCE_COLUMNS),
    ],
)
def test_report_templates_match_code(template: str, columns: tuple[str, ...]) -> None:
    with (ROOT / "config" / template).open(newline="", encoding="utf-8") as handle:
        assert list(csv.reader(handle)) == [list(columns)]


def test_cli_matches_observations_and_writes_traceable_reports(tmp_path: Path, capsys) -> None:
    targets = write_rows(tmp_path / "tm.csv", target_values(*TARGET_ROWS))
    observations = write_observations(
        tmp_path / "obs.csv", "lf_sale", "pcc_single", "lf_regular", "dbz_sale", "pcc_variants"
    )
    out = tmp_path / "out"

    assert load_cli().main(
        ["--target-master", str(targets), "--observations", str(observations),
         "--output-dir", str(out)]
    ) == 0

    results = read_dicts(out / "offer_match_results.csv")
    assert list(results[0]) == list(MATCH_RESULT_COLUMNS)
    by_row = {row["source_row_number"]: row for row in results}
    assert [row["match_status"] for row in results] == [
        "EXACT",
        "REVIEW_REQUIRED",
        "REVIEW_REQUIRED",
        "REVIEW_REQUIRED",
        "REVIEW_REQUIRED",
    ]
    exact = by_row["2"]
    assert exact["target_id"] == "TGT-000001"
    assert exact["retailer_id"] == "laptop_factory"
    assert exact["offer_url"].startswith("https://laptopfactory.com.ph/product/")
    assert exact["retrieved_at"].endswith("Z")
    assert exact["offer_cpu_series"] == "Snapdragon X"

    conflict = by_row["3"]
    assert conflict["target_id"] == ""
    assert conflict["candidate_target_ids"] == "TGT-000002"
    assert conflict["reason_codes"] == "SECONDARY_CONFLICT;SECONDARY_CONFLICT_REFRESH_RATE_HZ"

    # The LF regular fixture's CPU row is not a recognized series, so it never reaches the
    # inactive TGT-000003 or any other target.
    assert by_row["4"]["reason_codes"] == "MISSING_CORE_FIELDS;UNPARSEABLE_CPU_SERIES"
    assert by_row["5"]["reason_codes"] == "MISSING_CORE_FIELDS;MISSING_MODEL"
    assert "MULTI_VARIANT_SHARED_DESCRIPTION" in by_row["6"]["reason_codes"]

    queue = read_dicts(out / "offer_match_review_queue.csv")
    assert [row["source_row_number"] for row in queue] == ["3", "4", "5", "6"]
    assert all(row["target_id"] == "" for row in queue)

    evidence = read_dicts(out / "offer_match_evidence.csv")
    assert list(evidence[0]) == list(MATCH_EVIDENCE_COLUMNS)
    refresh = [
        row for row in evidence
        if row["source_row_number"] == "3" and row["field"] == "refresh_rate_hz"
    ]
    assert [(row["stage"], row["status"]) for row in refresh] == [
        ("EXTRACTION", "EXTRACTED"),
        ("MATCH", "CONFLICT"),
    ]
    assert refresh[0]["source_label"] == "Display"
    assert refresh[1]["target_id"] == "TGT-000002"

    printed = capsys.readouterr().out
    assert "EXACT: 1" in printed and "REVIEW_REQUIRED: 4" in printed
    assert "Acer" not in printed and "Nitro" not in printed


def test_cli_inactive_target_is_reported_unmatched(tmp_path: Path) -> None:
    inactive = target_row(
        target_id="TGT-000009",
        status="INACTIVE",
        model="Example Nitro 15 EN15-0002",
        screen_size_in="15.6",
        cpu_series="Core 5",
        ram_capacity_gb="16",
        storage_capacity_gb="512",
        refresh_rate_hz="",
    )
    targets = write_rows(tmp_path / "tm.csv", target_values(inactive))
    observations = write_observations(tmp_path / "obs.csv", "pcc_single")
    out = tmp_path / "out"
    argv = ["--target-master", str(targets), "--observations", str(observations)]
    assert load_cli().main([*argv, "--output-dir", str(out)]) == 0
    [row] = read_dicts(out / "offer_match_results.csv")
    assert (row["match_status"], row["target_id"], row["reason_codes"]) == (
        "UNMATCHED",
        "",
        "INACTIVE_TARGET",
    )
    assert len(read_dicts(out / "offer_match_review_queue.csv")) == 1


def test_cli_refuses_an_invalid_target_snapshot_and_removes_stale_reports(
    tmp_path: Path, capsys
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "offer_match_results.csv").write_text("stale\n", encoding="utf-8")
    duplicate = target_row(**(LF_TARGET | {"target_id": "TGT-000002"}))
    targets = write_rows(tmp_path / "tm.csv", target_values(TARGET_ROWS[0], duplicate))
    observations = write_observations(tmp_path / "obs.csv", "lf_sale")
    argv = ["--target-master", str(targets), "--observations", str(observations)]

    assert load_cli().main([*argv, "--output-dir", str(out)]) == 1

    captured = capsys.readouterr()
    assert "DUPLICATE_CONFIGURATION: 2" in captured.out
    assert "did not pass validation" in captured.err
    assert not (out / "offer_match_results.csv").exists()


def test_cli_fails_closed_on_a_malformed_observation_row(tmp_path: Path, capsys) -> None:
    targets = write_rows(tmp_path / "tm.csv", target_values(*TARGET_ROWS))
    path = write_observations(tmp_path / "obs.csv", "lf_sale", "pcc_single")
    rows = read_dicts(path)
    rows[1]["price"] = "SECRET-PRICE"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FULL_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    out = tmp_path / "out"
    argv = ["--target-master", str(targets), "--observations", str(path)]

    assert load_cli().main([*argv, "--output-dir", str(out)]) == 1

    captured = capsys.readouterr()
    assert "Observation row 3 is invalid (price)" in captured.err
    assert "SECRET-PRICE" not in captured.out + captured.err
    assert not (out / "offer_match_results.csv").exists()


def test_cli_requires_observation_columns(tmp_path: Path, capsys) -> None:
    targets = write_rows(tmp_path / "tm.csv", target_values(*TARGET_ROWS))
    observations = write_rows(tmp_path / "obs.csv", [["retailer_id", "offer_url"]])
    argv = ["--target-master", str(targets), "--observations", str(observations)]
    assert load_cli().main([*argv, "--output-dir", str(tmp_path / "out")]) == 1
    assert "missing columns" in capsys.readouterr().err
