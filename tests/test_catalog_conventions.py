"""Catalog convention proposals (model codes, cpu_series forms). Synthetic data only."""

from __future__ import annotations

import csv
import importlib.util
from pathlib import Path

import pytest
from scraping_fakes import no_network  # noqa: F401
from test_extraction import synthetic_offer
from test_supporting_tables import map_row, review_row
from test_target_master import target_row, target_values

from laptop_tracker.catalog_conventions import (
    NEEDS_CONFIRMATION,
    NO_CHANGE,
    NOT_APPLICABLE,
    READY,
    UNRESOLVED,
    CatalogInputError,
    apply_ready_proposals,
    build_report,
    correction_report_markdown,
)
from laptop_tracker.matching import ExactMatcher, MatchStatus
from laptop_tracker.offers import OBSERVATION_COLUMNS, OFFER_DETAIL_COLUMNS
from laptop_tracker.supporting_tables import (
    MATCH_REVIEW_HEADERS,
    SOURCE_MODEL_MAP_HEADERS,
    validate_match_review,
    validate_source_model_map,
)
from laptop_tracker.target_master import TARGET_MASTER_HEADERS, validate_target_master

pytestmark = pytest.mark.usefixtures("no_network")

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = {
    "cpu_series": "Core 5",
    "cpu_model": "Core 5 120U",
    "series": "",
    "manufacturer_model_code": "",
}


def row(**overrides: str) -> list[str]:
    return target_row(**(DEFAULTS | overrides))


def catalog(*rows: list[str]):
    result = validate_target_master(target_values(*rows))
    assert result.is_valid, result.errors
    return result


def proposal(report, target_id: str = "TGT-000001") -> dict[str, str]:
    return next(item for item in report.proposals if item["target_id"] == target_id)


def complete_review(source_model_id: str, **overrides: str) -> list[str]:
    core = {
        "model": "ExampleBook 14",
        "screen_size_in": "14",
        "cpu_series": "Core 5",
        "ram_capacity_gb": "16",
        "storage_capacity_gb": "1000",
    }
    return review_row(status="COMPLETE", source_model_id=source_model_id, **(core | overrides))


def with_mappings(targets, links: list[tuple[str, str]], reviews: list[list[str]]):
    mapping = validate_source_model_map(
        [SOURCE_MODEL_MAP_HEADERS, *(map_row(source_model_id=s, target_id=t) for s, t in links)],
        targets,
    )
    review = validate_match_review([MATCH_REVIEW_HEADERS, *reviews])
    assert mapping.is_valid and review.is_valid
    return {"source_model_map": mapping, "match_review": review}


# model


def test_verified_code_becomes_the_model_and_the_family_name_moves_to_series() -> None:
    report = build_report(catalog(row(manufacturer_model_code="EXB14-0001")))
    item = proposal(report)

    assert (item["proposed_model"], item["proposed_series"]) == ("EXB14-0001", "ExampleBook 14")
    assert (item["model_action"], item["model_readiness"]) == (
        "SET_MODEL_TO_VERIFIED_CODE",
        READY,
    )
    assert "manufacturer_model_code = 'EXB14-0001'" in item["evidence"]
    assert "model 'ExampleBook 14' (proposed series)" in item["original_wording_to_preserve"]
    assert "model was 'ExampleBook 14'" in item["proposed_notes_append"]
    assert item["unresolved_issues"] == ""


def test_an_existing_series_is_kept_and_the_old_model_goes_to_notes() -> None:
    item = proposal(
        build_report(catalog(row(manufacturer_model_code="EXB14-0001", series="ExampleBook")))
    )

    assert (item["proposed_model"], item["proposed_series"]) == ("EXB14-0001", "ExampleBook")
    assert "(notes; series already set)" in item["original_wording_to_preserve"]


def test_a_model_that_already_is_the_code_needs_no_change() -> None:
    item = proposal(build_report(catalog(row(model="EXB14-0001"))))

    assert (item["model_action"], item["model_readiness"]) == ("KEEP_VERIFIED_CODE", NO_CHANGE)
    assert item["proposed_manufacturer_model_code"] == "EXB14-0001"
    assert "MANUFACTURER_MODEL_CODE_BLANK" in item["unresolved_issues"]


@pytest.mark.parametrize(
    ("code_field", "issue"),
    [
        ("ExampleBook X390", "MANUFACTURER_MODEL_CODE_IS_NOT_A_CODE"),
        ("", "NO_VERIFIED_MODEL_CODE"),
    ],
)
def test_without_a_verified_code_the_model_name_is_kept_and_reported(
    code_field: str, issue: str
) -> None:
    item = proposal(
        build_report(
            catalog(
                row(
                    manufacturer_model_code=code_field,
                    variant_notes="Parent item_name: ExampleBook 14 EXB14-0009 16GB 1TB",
                )
            )
        )
    )

    assert item["proposed_model"] == "ExampleBook 14"
    assert item["model_readiness"] == UNRESOLVED
    assert issue in item["unresolved_issues"]
    # Codes in listing text are hints only, never proposals.
    assert item["unverified_code_hints"] == "UNVERIFIED listing/variant text, not used: EXB14-0009"


def test_a_review_recorded_code_needs_confirmation_when_the_target_has_none() -> None:
    targets = catalog(row())
    tables = with_mappings(
        targets,
        [("0042", "TGT-000001")],
        [complete_review("0042", manufacturer_model_code="EXB14-0001")],
    )
    item = proposal(build_report(targets, **tables))

    assert (item["proposed_model"], item["model_readiness"]) == ("EXB14-0001", NEEDS_CONFIRMATION)
    assert "source_model_id 0042" in item["evidence"]
    assert "CONFIRM_REVIEW_RECORDED_CODE" in item["unresolved_issues"]


def test_mapped_listings_recording_another_code_block_the_proposal() -> None:
    targets = catalog(row(manufacturer_model_code="EXB14-0001"))
    tables = with_mappings(
        targets,
        [("0042", "TGT-000001"), ("0043", "TGT-000001")],
        [
            complete_review("0042", manufacturer_model_code="EXB14-0001"),
            complete_review("0043", manufacturer_model_code="EXB14-0002"),
        ],
    )
    item = proposal(build_report(targets, **tables))

    assert item["proposed_model"] == "ExampleBook 14"
    assert (item["model_action"], item["model_readiness"]) == (
        "REVIEW_CONFLICTING_CODES",
        UNRESOLVED,
    )
    assert (
        "MAPPED_LISTINGS_RECORD_OTHER_CODES: target code EXB14-0001; "
        "EXB14-0002 (mapped source_model_id 0043)"
    ) in item["unresolved_issues"]


def test_listing_only_targets_are_not_applicable() -> None:
    listing = row(
        target_id="TGT-000101",
        model="",
        manufacturer_model_code="",
        brand="",
        resolution="",
        screen_size_in="",
        cpu_series="",
        ram_capacity_gb="",
        storage_capacity_gb="",
        tracking_mode="LISTING_ONLY",
        shop_id="10",
        item_id="20",
        source_model_id="0042",
        tracking_mode_reason="No product model name or code.",
        classification_evidence="Listing title and seller specs checked.",
    )
    item = proposal(build_report(catalog(row(), listing)), "TGT-000101")

    assert (item["model_readiness"], item["cpu_readiness"]) == (NOT_APPLICABLE, NOT_APPLICABLE)


# cpu_series


@pytest.mark.parametrize(
    ("brand", "current", "cpu_model", "expected", "readiness"),
    [
        ("Apple", "M5", "M5", "Apple M5", READY),
        ("Apple", "M4 Max", "M4 Max", "Apple M4 Max", READY),
        ("Apple", "A18 Pro", "A18 Pro", "Apple A18 Pro", READY),
        ("ExampleBrand", "Intel Core i5", "i5-1335U", "Core i5", READY),
        ("ExampleBrand", "Intel N150", "", "Intel N150", NO_CHANGE),
        ("ExampleBrand", "Celeron N4020", "", "Celeron N4020", NO_CHANGE),
        # A bare chip name is not given an Apple prefix unless the brand is Apple.
        ("ExampleBrand", "M5", "M5", "M5", UNRESOLVED),
        ("ExampleBrand", "Example Series 5", "", "Example Series 5", UNRESOLVED),
    ],
)
def test_cpu_series_proposals(
    brand: str, current: str, cpu_model: str, expected: str, readiness: str
) -> None:
    targets = catalog(row(brand=brand, cpu_series=current, cpu_model=cpu_model))
    item = proposal(build_report(targets))

    assert (item["proposed_cpu_series"], item["cpu_readiness"]) == (expected, readiness)
    assert item["cpu_model"] == cpu_model  # never changed


def test_cpu_model_disagreement_is_reported_not_corrected() -> None:
    item = proposal(build_report(catalog(row(cpu_series="Core i5", cpu_model="Intel i7"))))

    assert (item["proposed_cpu_series"], item["cpu_readiness"]) == ("Core i5", NO_CHANGE)
    assert "CPU_MODEL_DISAGREES_WITH_CPU_SERIES" in item["unresolved_issues"]


# duplicates, retailer evidence, diagnostic copy


def test_proposals_that_would_create_a_duplicate_core_key_are_reported() -> None:
    targets = catalog(
        row(model="ExampleBook A", manufacturer_model_code="EXB14-0001"),
        row(target_id="TGT-000002", model="ExampleBook B", manufacturer_model_code="EXB14-0001"),
    )
    report = build_report(targets)

    assert [group["target_ids"] for group in report.duplicates] == ["TGT-000001;TGT-000002"]
    assert proposal(report)["proposed_core_key_shared_with"] == "TGT-000002"
    diagnostic = validate_target_master(apply_ready_proposals(targets.valid_rows, report.proposals))
    assert not diagnostic.is_valid
    assert "## 4. Duplicate core keys" in correction_report_markdown(report, "tm.csv")


def lf_offer(ram: str = "16GB DDR5"):
    return synthetic_offer(
        "laptop_factory",
        (
            ("Model Name", "ExampleBrand ExampleBook 14 EXB14-0001 (Example Blue)"),
            ("Processor", "Intel Core 5 120U"),
            ("RAM", ram),
            ("Storage", "1TB NVMe SSD"),
            ("Display Size", '14" FHD 1920x1080 IPS 60Hz'),
        ),
    )


def test_retailer_evidence_reports_core_differences_without_choosing_a_side() -> None:
    targets = catalog(row(manufacturer_model_code="EXB14-0001"))
    item = proposal(build_report(targets, observations=[lf_offer(ram="8GB DDR5")]))

    assert "code matches; ram_capacity_gb retailer 8 vs catalog 16" in item["retailer_evidence"]
    assert "RETAILER_CORE_VALUE_DIFFERS: ram_capacity_gb is '8'" in item["unresolved_issues"]
    assert "does not show which is correct" in item["unresolved_issues"]


def test_the_diagnostic_copy_applies_only_ready_values_and_then_matches() -> None:
    targets = catalog(
        row(manufacturer_model_code="EXB14-0001"),
        row(target_id="TGT-000002", model="ExampleBook 16", screen_size_in="16"),
    )
    tables = with_mappings(
        targets,
        [("0042", "TGT-000002")],
        [complete_review("0042", model="ExampleBook 16", screen_size_in="16",
                         manufacturer_model_code="EXB16-0001")],
    )
    report = build_report(targets, **tables)
    assert proposal(report, "TGT-000002")["model_readiness"] == NEEDS_CONFIRMATION

    values = apply_ready_proposals(targets.valid_rows, report.proposals)
    rows = {line[0]: dict(zip(TARGET_MASTER_HEADERS, line, strict=True)) for line in values[1:]}
    assert rows["TGT-000001"]["model"] == "EXB14-0001"
    assert rows["TGT-000002"]["model"] == "ExampleBook 16"  # NEEDS_CONFIRMATION not applied

    diagnostic = validate_target_master(values)
    assert diagnostic.is_valid
    current = ExactMatcher(targets.valid_rows[list(TARGET_MASTER_HEADERS)]).match_observation(
        lf_offer()
    )
    proposed = ExactMatcher(
        diagnostic.valid_rows[list(TARGET_MASTER_HEADERS)]
    ).match_observation(lf_offer())
    assert current.status is MatchStatus.UNMATCHED
    assert (proposed.status, proposed.target_id) == (MatchStatus.EXACT, "TGT-000001")


def test_invalid_inputs_are_refused() -> None:
    invalid = validate_target_master(target_values(row(), row()))  # duplicate target
    with pytest.raises(CatalogInputError, match="TARGET_MASTER"):
        build_report(invalid)


# CLI


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_csv(path: Path, rows: list[list[str]]) -> Path:
    with path.open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows(rows)
    return path


def test_cli_writes_local_reports_and_prints_counts_only(tmp_path: Path, capsys) -> None:
    targets = write_csv(
        tmp_path / "tm.csv",
        target_values(row(manufacturer_model_code="EXB14-0001"), row(
            target_id="TGT-000002", brand="Apple", model="ExampleBook Air", cpu_series="M5",
            cpu_model="M5",
        )),
    )
    observations = tmp_path / "obs.csv"
    with observations.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=[*OBSERVATION_COLUMNS, *OFFER_DETAIL_COLUMNS])
        writer.writeheader()
        writer.writerow(lf_offer().to_full_record())
    out = tmp_path / "out"

    code = load_script("propose_catalog_conventions").main(
        ["--target-master", str(targets), "--observations", str(observations),
         "--output-dir", str(out)]
    )

    assert code == 0
    printed = capsys.readouterr().out
    assert "model READY: 1" in printed and "cpu_series READY: 1" in printed
    assert "2/2 rows pass validation" in printed
    assert "EXB14-0001" not in printed  # values stay in the files
    with (out / "catalog_convention_proposals.csv").open(encoding="utf-8") as handle:
        proposals = {item["target_id"]: item for item in csv.DictReader(handle)}
    assert proposals["TGT-000002"]["proposed_cpu_series"] == "Apple M5"
    markdown = (out / "SHEET_CORRECTION_REPORT.md").read_text(encoding="utf-8")
    assert "| TGT-000001 | False | EXB14-0001 | ExampleBook 14 |" in markdown
    # Evidence links: the target's source_url and the retailer page whose code matched.
    assert (
        "catalog source: https://example.com/products/eb14; "
        "retailer: https://shop.example.test/product"
    ) in markdown
    assert "| TGT-000002 | Apple | M5 | Apple M5 | M5 |" in markdown
    assert (out / "DIAGNOSTIC_target_master_ready_proposals_applied.csv").is_file()


def test_cli_refuses_an_invalid_snapshot_and_leaves_no_reports(tmp_path: Path, capsys) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "catalog_convention_proposals.csv").write_text("stale", encoding="utf-8")
    targets = write_csv(tmp_path / "tm.csv", target_values(row(), row()))

    code = load_script("propose_catalog_conventions").main(
        ["--target-master", str(targets), "--output-dir", str(out)]
    )

    assert code == 1
    assert "Convention proposal stopped" in capsys.readouterr().err
    assert not (out / "catalog_convention_proposals.csv").exists()
