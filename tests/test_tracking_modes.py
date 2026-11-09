"""CONFIGURATION and LISTING_ONLY tracking modes (approved 2026-10-08). Synthetic data only."""

from __future__ import annotations

import csv
import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import settings
from scraping_fakes import no_network  # noqa: F401
from test_extraction import fixture_offer
from test_supporting_tables import map_row, review_row, top_100_values
from test_target_master import BASE_ROW, target_row, target_values

from laptop_tracker.coverage import audit_coverage
from laptop_tracker.matching import ExactMatcher, MatchStatus
from laptop_tracker.supporting_tables import (
    LEGACY_MATCH_REVIEW_HEADERS,
    MATCH_REVIEW_HEADERS,
    SOURCE_MODEL_MAP_HEADERS,
    validate_match_review,
    validate_source_model_map,
)
from laptop_tracker.target_master import (
    LEGACY_TARGET_MASTER_HEADERS,
    TARGET_MASTER_HEADERS,
    read_target_master_csv,
    validate_target_master,
)

pytestmark = pytest.mark.usefixtures("no_network")

ROOT = Path(__file__).resolve().parents[1]
CORE = ("model", "screen_size_in", "cpu_series", "ram_capacity_gb", "storage_capacity_gb")
LISTING = {
    "tracking_mode": "LISTING_ONLY",
    "shop_id": "10",
    "item_id": "20",
    "source_model_id": "0042",
    "tracking_mode_reason": "Seller and listing show no product model name or code.",
    "classification_evidence": "Listing title, variation names, and seller specs checked.",
}


def listing_row(**overrides: str) -> list[str]:
    """A LISTING_ONLY target: no model, unavailable specs blank, listing identity filled."""
    blanks = dict.fromkeys((*CORE, "manufacturer_model_code", "brand", "resolution"), "")
    return target_row(**(blanks | {"target_id": "TGT-000101"} | LISTING | overrides))


def codes(result) -> list[tuple[int, str, str]]:
    return [
        (int(row.source_row_number), row.column_name, row.error_code)
        for row in result.errors.itertuples(index=False)
    ]


# TARGET_MASTER


def test_templates_append_tracking_columns_without_moving_existing_columns() -> None:
    assert read_target_master_csv(ROOT / "config" / "target_master_template.csv") == [
        TARGET_MASTER_HEADERS
    ]
    assert TARGET_MASTER_HEADERS[: len(LEGACY_TARGET_MASTER_HEADERS)] == (
        LEGACY_TARGET_MASTER_HEADERS
    )
    assert TARGET_MASTER_HEADERS[len(LEGACY_TARGET_MASTER_HEADERS) :] == [
        "tracking_mode",
        "shop_id",
        "item_id",
        "source_model_id",
        "tracking_mode_reason",
        "classification_evidence",
    ]
    assert MATCH_REVIEW_HEADERS[: len(LEGACY_MATCH_REVIEW_HEADERS)] == LEGACY_MATCH_REVIEW_HEADERS
    assert MATCH_REVIEW_HEADERS[len(LEGACY_MATCH_REVIEW_HEADERS) :] == [
        "tracking_mode",
        "shop_id",
        "item_id",
        "tracking_mode_reason",
        "classification_evidence",
    ]


def test_legacy_snapshot_without_tracking_columns_is_read_as_configuration() -> None:
    legacy = target_values(
        target_row(LEGACY_TARGET_MASTER_HEADERS, target_id="TGT-000007"),
        headers=LEGACY_TARGET_MASTER_HEADERS,
    )
    result = validate_target_master(legacy)
    assert result.is_valid
    row = result.valid_rows.iloc[0]
    assert row["target_id"] == "TGT-000007"
    assert row["model"] == "ExampleBook 14"
    assert row["tracking_mode"] == ""  # Stored text is preserved; blank means CONFIGURATION.
    legacy_core_gap = validate_target_master(
        target_values(
            target_row(LEGACY_TARGET_MASTER_HEADERS, model=""),
            headers=LEGACY_TARGET_MASTER_HEADERS,
        )
    )
    assert codes(legacy_core_gap) == [(2, "model", "MISSING_VALUE")]


def test_listing_only_row_is_valid_without_model_or_unavailable_specs() -> None:
    result = validate_target_master(target_values(target_row(), listing_row()))
    assert result.is_valid
    assert result.valid_rows["source_model_id"].tolist() == ["", "0042"]  # Exact text kept.


@pytest.mark.parametrize(
    "missing",
    ["shop_id", "item_id", "source_model_id", "tracking_mode_reason", "classification_evidence"],
)
def test_listing_only_requires_listing_identity_reason_and_evidence(missing: str) -> None:
    result = validate_target_master(target_values(listing_row(**{missing: ""})))
    assert codes(result) == [(2, missing, "MISSING_VALUE")]


@pytest.mark.parametrize(
    ("column", "value"), [("model", "ExampleBook 14"), ("manufacturer_model_code", "EB14")]
)
def test_listing_only_with_a_model_is_contradictory(column: str, value: str) -> None:
    result = validate_target_master(target_values(listing_row(**{column: value})))
    assert codes(result) == [(2, column, "CONTRADICTORY_TRACKING_MODE")]


@pytest.mark.parametrize("mode", ["", "CONFIGURATION"])
def test_configuration_rows_may_not_carry_listing_identity(mode: str) -> None:
    result = validate_target_master(
        target_values(target_row(tracking_mode=mode, shop_id="10", source_model_id="0042"))
    )
    assert codes(result) == [
        (2, "shop_id", "CONTRADICTORY_TRACKING_MODE"),
        (2, "source_model_id", "CONTRADICTORY_TRACKING_MODE"),
    ]


def test_blank_model_alone_does_not_make_a_listing_only_target() -> None:
    # Unfinished research: still a CONFIGURATION row, so its core fields are required.
    result = validate_target_master(target_values(target_row(model="")))
    assert codes(result) == [(2, "model", "MISSING_VALUE")]


@pytest.mark.parametrize("model", ["Generic", "UNBRANDED", " no  brand", "N/A", "OEM"])
def test_placeholder_models_are_rejected(model: str) -> None:
    result = validate_target_master(target_values(target_row(model=model.strip())))
    assert [entry[2] for entry in codes(result)] == ["PLACEHOLDER_MODEL"]


@pytest.mark.parametrize(
    ("column", "value", "code"),
    [
        ("tracking_mode", "LISTING", "INVALID_VALUE"),
        ("tracking_mode", "listing_only", "INVALID_VALUE"),
        ("shop_id", "10a", "INVALID_FORMAT"),
        ("item_id", " 20", "SURROUNDING_WHITESPACE"),
        ("source_model_id", "4.2E+10", "INVALID_FORMAT"),
    ],
)
def test_unknown_modes_and_malformed_listing_ids_are_rejected(
    column: str, value: str, code: str
) -> None:
    result = validate_target_master(target_values(listing_row(**{column: value})))
    assert (2, column, code) in codes(result)
    assert result.valid_rows.empty


def test_listing_duplicates_use_exact_listing_identity() -> None:
    result = validate_target_master(
        target_values(
            listing_row(),
            listing_row(target_id="TGT-000102"),  # Same listing: duplicate.
            listing_row(target_id="TGT-000103", source_model_id="7"),
            listing_row(target_id="TGT-000104", source_model_id="7", item_id="21"),
            listing_row(target_id="TGT-000105", source_model_id="42"),  # Not "0042".
        )
    )
    assert codes(result) == [
        (2, "listing_identity", "DUPLICATE_LISTING"),
        (3, "listing_identity", "DUPLICATE_LISTING"),
        (4, "listing_identity", "CONFLICTING_LISTING_IDENTITY"),
        (5, "listing_identity", "CONFLICTING_LISTING_IDENTITY"),
    ]
    assert result.valid_rows["target_id"].tolist() == ["TGT-000105"]


def test_listing_only_rows_never_form_configuration_duplicates() -> None:
    # Two listing-only rows with the same available specs, and one sharing a configuration's
    # specs, are separate listings, not duplicate configurations.
    specs = {"screen_size_in": "14", "ram_capacity_gb": "16", "storage_capacity_gb": "1000"}
    result = validate_target_master(
        target_values(
            target_row(),
            listing_row(**specs),
            listing_row(target_id="TGT-000102", source_model_id="43", **specs),
        )
    )
    assert result.is_valid


def test_target_ids_stay_unique_across_tracking_modes() -> None:
    result = validate_target_master(
        target_values(target_row(), listing_row(target_id="TGT-000001"))
    )
    assert [entry[2] for entry in codes(result)] == ["DUPLICATE_TARGET_ID"] * 2


# MATCH_REVIEW

COMPLETE_CORE = {name: BASE_ROW[name] for name in CORE}
REVIEW_LISTING = {name: LISTING[name] for name in LISTING if name != "source_model_id"}


def test_legacy_review_snapshot_is_accepted_as_configuration_candidates() -> None:
    legacy_values = dict(
        zip(MATCH_REVIEW_HEADERS, review_row(status="COMPLETE", **COMPLETE_CORE), strict=True)
    )
    row = [legacy_values[name] for name in LEGACY_MATCH_REVIEW_HEADERS]
    result = validate_match_review([LEGACY_MATCH_REVIEW_HEADERS, row])
    assert result.is_valid
    assert result.valid_rows.loc[0, "tracking_mode"] == ""
    legacy_gap = dict(legacy_values, model="")
    gap = validate_match_review(
        [LEGACY_MATCH_REVIEW_HEADERS, [legacy_gap[name] for name in LEGACY_MATCH_REVIEW_HEADERS]]
    )
    assert codes(gap) == [(2, "model", "MISSING_VALUE")]


def test_listing_only_candidate_can_be_complete_without_a_model() -> None:
    result = validate_match_review(
        [
            MATCH_REVIEW_HEADERS,
            review_row(status="COMPLETE", source_model_id="0042", **REVIEW_LISTING),
        ]
    )
    assert result.is_valid
    assert result.info["complete_listing_only_rows"] == 1


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"classification_evidence": ""}, (2, "classification_evidence", "MISSING_VALUE")),
        ({"shop_id": ""}, (2, "shop_id", "MISSING_VALUE")),
        ({"source_model_id": ""}, (2, "source_model_id", "MISSING_VALUE")),
        ({"model": "ExampleBook 14"}, (2, "model", "CONTRADICTORY_TRACKING_MODE")),
    ],
)
def test_complete_listing_only_candidate_must_meet_listing_requirements(
    overrides: dict[str, str], expected: tuple[int, str, str]
) -> None:
    values = {"source_model_id": "0042", **REVIEW_LISTING} | overrides
    result = validate_match_review([MATCH_REVIEW_HEADERS, review_row(status="COMPLETE", **values)])
    assert codes(result) == [expected]


def test_unresolved_classification_conflicts_may_stay_under_review() -> None:
    conflicted = {"source_model_id": "0042", **REVIEW_LISTING, "model": "Maybe EB14"}
    result = validate_match_review(
        [MATCH_REVIEW_HEADERS, review_row(status="REVIEW_REQUIRED", **conflicted)]
    )
    assert result.is_valid


def test_complete_configuration_candidate_rules() -> None:
    blank_model = validate_match_review(
        [MATCH_REVIEW_HEADERS, review_row(status="COMPLETE", **(COMPLETE_CORE | {"model": ""}))]
    )
    assert codes(blank_model) == [(2, "model", "MISSING_VALUE")]
    placeholder = validate_match_review(
        [
            MATCH_REVIEW_HEADERS,
            review_row(status="COMPLETE", **(COMPLETE_CORE | {"model": "Generic"})),
        ]
    )
    assert codes(placeholder) == [(2, "model", "PLACEHOLDER_MODEL")]
    # A configuration candidate may keep its source listing IDs as trace evidence.
    traced = validate_match_review(
        [
            MATCH_REVIEW_HEADERS,
            review_row(
                status="COMPLETE", tracking_mode="CONFIGURATION", shop_id="10", **COMPLETE_CORE
            ),
        ]
    )
    assert traced.is_valid


def test_unknown_review_tracking_mode_is_rejected() -> None:
    result = validate_match_review([MATCH_REVIEW_HEADERS, review_row(tracking_mode="UNSURE")])
    assert codes(result) == [(2, "tracking_mode", "INVALID_VALUE")]


# SOURCE_MODEL_MAP

MASTER = validate_target_master(target_values(target_row(), listing_row()))


def test_mapping_may_reference_either_target_type() -> None:
    result = validate_source_model_map(
        [
            SOURCE_MODEL_MAP_HEADERS,
            map_row(source_model_id="0042", target_id="TGT-000101"),
            map_row(source_model_id="900", target_id="TGT-000001"),
            map_row(source_model_id="901", target_id="TGT-000001"),
        ],
        MASTER,
    )
    assert result.is_valid
    assert result.info["links_to_listing_only_targets"] == 1
    assert result.info["listing_only_targets_without_mapping"] == 0


@pytest.mark.parametrize("source_id", ["42", "900"])
def test_listing_only_target_accepts_only_its_own_source_listing(source_id: str) -> None:
    result = validate_source_model_map(
        [SOURCE_MODEL_MAP_HEADERS, map_row(source_model_id=source_id, target_id="TGT-000101")],
        MASTER,
    )
    assert codes(result) == [(2, "source_model_id", "LISTING_TARGET_MISMATCH")]
    assert result.info["listing_only_targets_without_mapping"] == 1


def test_listing_source_id_may_not_map_to_another_target() -> None:
    result = validate_source_model_map(
        [SOURCE_MODEL_MAP_HEADERS, map_row(source_model_id="0042", target_id="TGT-000001")],
        MASTER,
    )
    assert codes(result) == [(2, "target_id", "LISTING_SOURCE_MAPPED_ELSEWHERE")]


def test_source_id_still_maps_to_only_one_target() -> None:
    result = validate_source_model_map(
        [
            SOURCE_MODEL_MAP_HEADERS,
            map_row(source_model_id="0042", target_id="TGT-000101"),
            map_row(source_model_id="0042", target_id="TGT-000101"),
        ],
        MASTER,
    )
    assert [entry[2] for entry in codes(result)] == ["DUPLICATE_SOURCE_MODEL_ID"] * 2


# Matching: listing-only rows never take part in cross-retailer configuration matching

ATTRIBUTES = {name: BASE_ROW[name] for name in CORE}


def as_dicts(values: list[list[str]]) -> list[dict[str, str]]:
    return [dict(zip(values[0], row, strict=True)) for row in values[1:]]


def test_listing_only_rows_are_excluded_even_if_they_carry_core_values() -> None:
    # Defense in depth: even a listing-only row with core values (which the validator would
    # reject) never creates a match, near-match candidate, or ambiguity.
    leaked = listing_row(**ATTRIBUTES)
    matcher = ExactMatcher(as_dicts(target_values(target_row(), leaked)))
    assert matcher.excluded_listing_only == 1
    assert matcher.configuration_target_count == 1

    exact = matcher.match(fixture_offer("lf_sale"), ATTRIBUTES)
    assert (exact.status, exact.target_id) == (MatchStatus.EXACT, "TGT-000001")

    only_listing = ExactMatcher(as_dicts(target_values(leaked)))
    result = only_listing.match(fixture_offer("lf_sale"), ATTRIBUTES | {"ram_capacity_gb": "16"})
    assert result.status is MatchStatus.UNMATCHED
    assert result.candidate_target_ids == ()
    near = only_listing.match(fixture_offer("lf_sale"), ATTRIBUTES | {"ram_capacity_gb": "32"})
    assert near.candidate_target_ids == ()


def test_matcher_rejects_unknown_tracking_modes() -> None:
    with pytest.raises(ValueError, match="unknown tracking_mode"):
        ExactMatcher(as_dicts(target_values(target_row(tracking_mode="LISTING"))))


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_csv(path: Path, rows: list[list[str]]) -> Path:
    with path.open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows(rows)
    return path


def observations_csv(path: Path) -> Path:
    from laptop_tracker.offers import OBSERVATION_COLUMNS, OFFER_DETAIL_COLUMNS

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=[*OBSERVATION_COLUMNS, *OFFER_DETAIL_COLUMNS])
        writer.writeheader()
        writer.writerow(fixture_offer("lf_sale").to_full_record())
    return path


def test_cli_reports_excluded_listing_rows_for_csv_input(tmp_path: Path, capsys) -> None:
    targets = write_csv(tmp_path / "tm.csv", target_values(target_row(), listing_row()))
    argv = [
        "--target-master",
        str(targets),
        "--observations",
        str(observations_csv(tmp_path / "o.csv")),
    ]
    assert load_script("match_offers").main([*argv, "--output-dir", str(tmp_path / "out")]) == 0
    out = capsys.readouterr().out
    assert "CONFIGURATION targets indexed: 1" in out
    assert "LISTING_ONLY targets excluded from configuration matching: 1" in out


def test_cli_listing_only_catalog_from_sheets_is_reported_clearly(tmp_path: Path, capsys) -> None:
    cli = load_script("match_offers")
    argv = [
        "--target-master-from-sheets",
        "--observations",
        str(observations_csv(tmp_path / "o.csv")),
    ]
    with (
        patch.object(cli.Settings, "from_env", return_value=settings()),
        patch.object(
            cli,
            "read_target_table_values",
            return_value={"TARGET_MASTER": target_values(listing_row())},
        ),
    ):
        assert cli.main([*argv, "--output-dir", str(tmp_path / "out")]) == 0
    captured = capsys.readouterr()
    assert "CONFIGURATION targets indexed: 0" in captured.out
    assert "excluded from configuration matching: 1" in captured.out
    assert "TARGET_MASTER has no CONFIGURATION rows" in captured.err
    with (tmp_path / "out" / "offer_match_results.csv").open(encoding="utf-8") as handle:
        [row] = list(csv.DictReader(handle))
    assert row["target_id"] == "" and row["candidate_target_ids"] == ""


def test_cli_invalid_listing_row_stops_before_results(tmp_path: Path, capsys) -> None:
    targets = write_csv(tmp_path / "tm.csv", target_values(target_row(), listing_row(model="EB14")))
    argv = [
        "--target-master",
        str(targets),
        "--observations",
        str(observations_csv(tmp_path / "o.csv")),
    ]
    assert load_script("match_offers").main([*argv, "--output-dir", str(tmp_path / "out")]) == 1
    assert "CONTRADICTORY_TRACKING_MODE: 1" in capsys.readouterr().out
    assert not (tmp_path / "out" / "offer_match_results.csv").exists()


# Top 100 coverage


def coverage_tables(master_values, map_rows, review_rows):
    master = validate_target_master(master_values)
    return (
        master,
        validate_source_model_map([SOURCE_MODEL_MAP_HEADERS, *map_rows], master),
        validate_match_review([MATCH_REVIEW_HEADERS, *review_rows]),
    )


def test_coverage_distinguishes_configuration_and_listing_only_mappings() -> None:
    # top_100_values() rows all use shop_id 10 and item_id 20.
    tables = coverage_tables(
        target_values(target_row(), listing_row()),
        [
            map_row(source_model_id="0042", target_id="TGT-000101"),
            map_row(source_model_id="5", target_id="TGT-000001"),
        ],
        [
            review_row(status="COMPLETE", source_model_id="0042", **REVIEW_LISTING),
            review_row(status="COMPLETE", source_model_id="5", **COMPLETE_CORE),
            review_row(status="INCOMPLETE", source_model_id="6"),
        ],
    )
    report = audit_coverage(top_100_values("0042", "5", "6"), *tables)
    assert [
        (r["model_id"], r["coverage_status"], r["target_tracking_mode"]) for r in report.rows
    ] == [
        ("0042", "MAPPED_LISTING_ONLY", "LISTING_ONLY"),
        ("5", "MAPPED_CONFIGURATION", "CONFIGURATION"),
        ("6", "UNMAPPED_IN_REVIEW", ""),
    ]
    assert report.complete_without_mapping == []


def test_coverage_checks_listing_identity_against_the_top_100_row() -> None:
    tables = coverage_tables(
        target_values(listing_row(shop_id="11")),  # Same source ID, different shop.
        [map_row(source_model_id="0042", target_id="TGT-000101")],
        [review_row(status="COMPLETE", source_model_id="0042", **REVIEW_LISTING)],
    )
    report = audit_coverage(top_100_values("0042"), *tables)
    assert report.rows[0]["coverage_status"] == "LISTING_IDENTITY_MISMATCH"
    assert [r["issue"] for r in report.complete_without_mapping] == ["LISTING_IDENTITY_MISMATCH"]


def test_coverage_flags_review_and_target_mode_mismatch() -> None:
    tables = coverage_tables(
        target_values(listing_row()),
        [map_row(source_model_id="0042", target_id="TGT-000101")],
        [review_row(status="COMPLETE", source_model_id="0042", **COMPLETE_CORE)],
    )
    report = audit_coverage(top_100_values("0042"), *tables)
    assert report.rows[0]["coverage_status"] == "MAPPED_LISTING_ONLY"
    assert [r["issue"] for r in report.complete_without_mapping] == ["TRACKING_MODE_MISMATCH"]
