"""Top 100 verified configurations (owner decision 2026-10-12). Synthetic data only."""

from __future__ import annotations

import csv
import importlib.util
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import settings
from scraping_fakes import no_network  # noqa: F401
from test_supporting_tables import map_row, review_row
from test_target_master import target_row, target_values

from laptop_tracker.supporting_tables import (
    MATCH_REVIEW_HEADERS,
    SOURCE_MODEL_MAP_HEADERS,
    validate_match_review,
    validate_source_model_map,
)
from laptop_tracker.target_master import validate_target_master
from laptop_tracker.verified_top import (
    CONFIGURATION_COLUMNS,
    LEDGER_COLUMNS,
    OUTPUT_FILES,
    RANKING_REQUIRED_COLUMNS,
    REGISTER_COLUMNS,
    RegisterEntry,
    VerifiedRankingInputError,
    audit_verified_ranking,
    build_sales_snapshot,
    build_verified_ranking,
    check_source_top_100,
    read_register,
)

pytestmark = pytest.mark.usefixtures("no_network")

ROOT = Path(__file__).resolve().parents[1]
CORE = {
    "model": "ExampleBook 14",
    "screen_size_in": "14",
    "cpu_series": "Core 5",
    "ram_capacity_gb": "16",
    "storage_capacity_gb": "1000",
}


def source(model_id: str, m1: str, mtd: str, **overrides: str) -> dict[str, str]:
    ranking = str(Decimal(m1) + Decimal(mtd))
    return {
        "rank": "1",
        "run_id": "20260928T112328+0800",
        "extract_date": "2026-09-28",
        "m_minus_1_month": "2026-08",
        "mtd_month": "2026-09",
        "shop_id": "10",
        "item_id": "20",
        "model_id": model_id,
        "item_name": f"Synthetic listing {model_id}",
        "shopee_model_name": "Synthetic variation",
        "m_minus_1_gmv_usd": m1,
        "mtd_gmv_usd": mtd,
        "ranking_gmv_usd": ranking,
        "eligibility_status": "ELIGIBLE",
        "present_in_mtd": "True",
    } | overrides


def target(target_id: str, **overrides: str) -> list[str]:
    defaults = {"cpu_model": "", "manufacturer_model_code": "", "series": ""}
    return target_row(**(defaults | CORE | {"target_id": target_id} | overrides))


def review(source_model_id: str, **overrides: str) -> list[str]:
    return review_row(status="COMPLETE", source_model_id=source_model_id, **(CORE | overrides))


def build(
    sources: list[dict[str, str]],
    targets: list[list[str]],
    links: list[tuple[str, str]],
    reviews: list[list[str]] | None = None,
    **kwargs,
):
    target_master = validate_target_master(target_values(*targets))
    mapping = validate_source_model_map(
        [SOURCE_MODEL_MAP_HEADERS, *(map_row(source_model_id=s, target_id=t) for s, t in links)],
        target_master,
    )
    if reviews is None:
        reviews = [review(s, **_core_of(targets, t)) for s, t in links]
    match_review = validate_match_review([MATCH_REVIEW_HEADERS, *reviews])
    assert target_master.is_valid and mapping.is_valid and match_review.is_valid
    snapshot = build_sales_snapshot(sources)
    result = build_verified_ranking(snapshot, target_master, mapping, match_review, **kwargs)
    return result, snapshot, target_master, mapping


def _core_of(targets: list[list[str]], target_id: str) -> dict[str, str]:
    values = target_values(*targets)
    for row in values[1:]:
        record = dict(zip(values[0], row, strict=True))
        if record["target_id"] == target_id:
            return {name: record[name] for name in CORE} | {
                "manufacturer_model_code": record["manufacturer_model_code"]
            }
    return {}


def ledger_reason(result, model_id: str) -> str:
    return next(row for row in result.ledger if row["model_id"] == model_id)["reason_code"]


# Grouping, totals, ordering


def test_sources_are_grouped_by_target_and_gmv_summed_per_period() -> None:
    result, *_ = build(
        [source("101", "100.5", "20.25"), source("102", "300", "0.75"), source("103", "50", "50")],
        [target("TGT-000001"), target("TGT-000002", model="ExampleBook 16")],
        [("101", "TGT-000001"), ("102", "TGT-000001"), ("103", "TGT-000002")],
    )

    assert [c["target_id"] for c in result.configurations] == ["TGT-000001", "TGT-000002"]
    first = result.configurations[0]
    assert (first["m_minus_1_gmv_usd"], first["mtd_gmv_usd"], first["ranking_gmv_usd"]) == (
        "400.5",
        "21.00",
        "421.50",
    )
    assert first["contributing_source_count"] == "2"
    # Contributors listed by their own GMV, highest first.
    assert first["contributing_source_model_ids"] == "102;101"
    assert first["model"] == "ExampleBook 14" and first["rank"] == "1"
    assert result.counts["configurations_with_multiple_sources"] == 1


def test_money_is_summed_exactly_from_the_source_text() -> None:
    result, *_ = build(
        [source("101", "0.1", "0.2"), source("102", "0.2", "0.1")],
        [target("TGT-000001")],
        [("101", "TGT-000001"), ("102", "TGT-000001")],
    )

    assert result.configurations[0]["ranking_gmv_usd"] == "0.6"  # not 0.6000000000000001


def test_ties_break_on_mtd_then_target_id() -> None:
    result, *_ = build(
        [
            source("101", "60", "40"),  # TGT-000003: 100, MTD 40
            source("102", "40", "60"),  # TGT-000002: 100, MTD 60
            source("103", "40", "60"),  # TGT-000001: 100, MTD 60
        ],
        [
            target("TGT-000001"),
            target("TGT-000002", model="ExampleBook 15"),
            target("TGT-000003", model="ExampleBook 16"),
        ],
        [("101", "TGT-000003"), ("102", "TGT-000002"), ("103", "TGT-000001")],
    )

    assert [c["target_id"] for c in result.configurations] == [
        "TGT-000001",
        "TGT-000002",
        "TGT-000003",
    ]


def test_the_whole_pool_is_grouped_before_the_top_n_is_selected() -> None:
    # TGT-000002 has no single large source, but its combined GMV beats TGT-000001's one source.
    result, *_ = build(
        [source("101", "90", "0"), source("102", "50", "0"), source("103", "50", "0")],
        [target("TGT-000001"), target("TGT-000002", model="ExampleBook 15")],
        [("101", "TGT-000001"), ("102", "TGT-000002"), ("103", "TGT-000002")],
        top_n=1,
    )

    assert [c["target_id"] for c in result.top] == ["TGT-000002"]
    assert result.top[0]["ranking_gmv_usd"] == "100"


def test_fewer_qualifying_configurations_than_requested_is_reported_not_padded() -> None:
    result, *_ = build([source("101", "1", "1")], [target("TGT-000001")], [("101", "TGT-000001")])

    assert len(result.top) == len(result.configurations) == 1
    assert result.counts["shortfall"] == 99
    assert result.counts["selected_configurations"] == 1


def test_each_source_counts_once_and_ids_are_exact_text() -> None:
    # "42" is not "0042": the mapping is for 0042 only.
    result, *_ = build(
        [source("42", "10", "10"), source("0042", "5", "5")],
        [target("TGT-000001")],
        [("0042", "TGT-000001")],
    )

    assert result.configurations[0]["contributing_source_model_ids"] == "0042"
    assert result.configurations[0]["ranking_gmv_usd"] == "10"
    assert ledger_reason(result, "42") == "UNMAPPED"
    assert len(result.ledger) == 2


# Exclusions


def test_target_level_exclusions() -> None:
    listing = target(
        "TGT-000101",
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
        source_model_id="104",
        tracking_mode_reason="No product model name or code.",
        classification_evidence="Listing title and seller specs checked.",
    )
    result, *_ = build(
        [
            source("101", "10", "0"),
            source("102", "10", "0"),
            source("103", "10", "0"),
            source("104", "10", "0"),
        ],
        [
            target("TGT-000001"),
            target("TGT-000002", model="ExampleBook 15", status="INACTIVE"),
            target(
                "TGT-000003", model="ExampleBook 16", cpu_series="Core i5", cpu_model="Intel i7"
            ),
            listing,
        ],
        [
            ("101", "TGT-000001"),
            ("102", "TGT-000002"),
            ("103", "TGT-000003"),
            ("104", "TGT-000101"),
        ],
        reviews=[
            review("101"),
            review("102", model="ExampleBook 15"),
            review("103", model="ExampleBook 16", cpu_series="Core i5"),
            review_row(
                status="COMPLETE",
                source_model_id="104",
                tracking_mode="LISTING_ONLY",
                shop_id="10",
                item_id="20",
                tracking_mode_reason="No product model name or code.",
                classification_evidence="Listing title and seller specs checked.",
            ),
        ],
    )

    assert [c["target_id"] for c in result.configurations] == ["TGT-000001"]
    assert ledger_reason(result, "102") == "TARGET_INACTIVE"
    assert ledger_reason(result, "103") == "TARGET_CPU_MODEL_CONFLICT"
    assert ledger_reason(result, "104") == "TARGET_LISTING_ONLY"
    target_rows = {
        (e["target_id"], e["reason_code"]) for e in result.exclusions if e["scope"] == "TARGET"
    }
    assert target_rows == {
        ("TGT-000002", "TARGET_INACTIVE"),
        ("TGT-000003", "TARGET_CPU_MODEL_CONFLICT"),
        ("TGT-000101", "TARGET_LISTING_ONLY"),
    }


@pytest.mark.parametrize(
    "reviews",
    [
        [],
        [review("101"), review_row(status="REVIEW_REQUIRED", source_model_id="101")],
        [review("101", ram_capacity_gb="8")],
        [review("101", manufacturer_model_code="EXB14-0002")],
    ],
    ids=["no-review-rows", "open-review-row", "core-differs", "code-differs"],
)
def test_match_review_does_not_gate_mapped_sources(reviews: list[list[str]]) -> None:
    # Owner decision 2026-10-26: TARGET_MASTER and SOURCE_MODEL_MAP decide the ranking.
    result, *_ = build(
        [source("101", "10", "0")],
        [target("TGT-000001", manufacturer_model_code="EXB14-0001")],
        [("101", "TGT-000001")],
        reviews=reviews,
    )

    assert [c["target_id"] for c in result.configurations] == ["TGT-000001"]
    assert ledger_reason(result, "101") == ""
    assert result.counts["shortfall"] == 99


def test_source_eligibility_requirements_are_preserved() -> None:
    result, *_ = build(
        [source("101", "10", "0", present_in_mtd="False"), source("102", "1", "0")],
        [target("TGT-000001")],
        [("101", "TGT-000001"), ("102", "TGT-000001")],
    )

    assert ledger_reason(result, "101") == "SOURCE_NOT_ELIGIBLE"
    assert result.configurations[0]["ranking_gmv_usd"] == "1"


def register_entry(**overrides: str) -> RegisterEntry:
    values = dict.fromkeys(REGISTER_COLUMNS, "") | {
        "field": "ram_capacity_gb",
        "recorded_value": "16",
        "stated_value": "32",
        "status": "OPEN",
        "recorded_at": "2026-10-12",
    }
    return RegisterEntry(**(values | overrides))


def test_an_open_register_entry_overrides_a_complete_review_status() -> None:
    sources = [source("101", "10", "0"), source("102", "5", "0")]
    targets = [target("TGT-000001")]
    links = [("101", "TGT-000001"), ("102", "TGT-000001")]

    source_conflict, *_ = build(
        sources, targets, links, register=[register_entry(source_model_id="101")]
    )
    assert ledger_reason(source_conflict, "101") == "KNOWN_UNRESOLVED_CONFLICT"
    assert source_conflict.configurations[0]["ranking_gmv_usd"] == "5"

    target_conflict, *_ = build(
        sources, targets, links, register=[register_entry(target_id="TGT-000001")]
    )
    assert target_conflict.configurations == []

    resolved, *_ = build(
        sources, targets, links, register=[register_entry(source_model_id="101", status="RESOLVED")]
    )
    assert resolved.configurations[0]["ranking_gmv_usd"] == "15"


# Review priority


def test_review_priority_orders_by_source_gmv_and_infers_nothing() -> None:
    result, *_ = build(
        [source("101", "10", "0"), source("201", "5", "0"), source("202", "50", "0")],
        [target("TGT-000001")],
        [("101", "TGT-000001")],
        reviews=[
            review("101"),
            review_row(status="INCOMPLETE", source_model_id="201", ram_capacity_gb="8"),
        ],
        register=[
            register_entry(source_model_id="201", recorded_value="8", stated_value="16"),
            register_entry(source_model_id="999", target_id="TGT-000001"),
        ],
    )

    rows = result.review_priority
    assert [r["model_id"] for r in rows] == ["202", "201", "999"]
    assert rows[0]["suggested_action"].startswith("No MATCH_REVIEW row")
    assert rows[1]["missing_core_fields"] == "model;screen_size_in;cpu_series;storage_capacity_gb"
    assert "recorded '8', stated '16'" in rows[1]["known_register_notes"]
    assert rows[2]["scope"] == "KNOWN_REGISTER_ENTRY_NOT_IN_RANKING"
    assert rows[2]["ranking_gmv_usd"] == ""


# Input validation


def bad(records):
    with pytest.raises(VerifiedRankingInputError):
        build_sales_snapshot(records)


@pytest.mark.parametrize(
    "records",
    [
        [],
        [source("101", "1", "1"), source("101", "2", "2")],
        [source("101", "1", "1"), source("102", "1", "1", run_id="other")],
        [source("101", "1", "1"), source("102", "1", "1", mtd_month="2026-10")],
        [source("101", "1", "1", m_minus_1_month="2026-07")],
        [source("101", "1", "1", extract_date="2026-10-01")],
        [source("abc", "1", "1")],
        [source("", "1", "1")],
        [source("101", "1", "1", m_minus_1_gmv_usd="1,000")],
        [source("101", "1", "1", mtd_gmv_usd="-1", ranking_gmv_usd="0")],
        [source("101", "1", "1", mtd_gmv_usd="NaN")],
        [source("101", "1", "1", mtd_gmv_usd="")],
        [source("101", "1", "1", mtd_gmv_usd=" 1")],
        [source("101", "1", "1", ranking_gmv_usd="3")],
    ],
    ids=[
        "empty",
        "duplicate-source",
        "mixed-run",
        "mixed-period",
        "non-adjacent-months",
        "extract-outside-mtd",
        "non-digit-id",
        "blank-id",
        "grouped-number",
        "negative",
        "nan",
        "blank-gmv",
        "padded-gmv",
        "total-mismatch",
    ],
)
def test_malformed_rankings_are_refused(records) -> None:
    bad(records)


def test_float_artifacts_in_the_stored_total_are_tolerated() -> None:
    snapshot = build_sales_snapshot(
        [source("101", "450134.6174", "450567.4579", ranking_gmv_usd="900702.0752999999")]
    )
    assert snapshot.rows[0].ranking_gmv == Decimal("900702.0752999999")


def test_the_source_top_100_must_match_the_ranking_run() -> None:
    snapshot = build_sales_snapshot([source("101", "1", "1"), source("102", "1", "1")])
    assert check_source_top_100(snapshot, [source("101", "1", "1")]) == {"101"}
    for records in (
        [source("103", "1", "1")],
        [source("101", "1", "2", ranking_gmv_usd="3")],
        [source("101", "1", "1", run_id="other")],
        [source("101", "1", "1"), source("101", "1", "1")],
    ):
        with pytest.raises(VerifiedRankingInputError):
            check_source_top_100(snapshot, records)


def test_invalid_catalog_tables_are_refused() -> None:
    target_master = validate_target_master(
        target_values(target("TGT-000001"), target("TGT-000001"))
    )
    mapping = validate_source_model_map([SOURCE_MODEL_MAP_HEADERS], target_master)
    match_review = validate_match_review([MATCH_REVIEW_HEADERS])
    with pytest.raises(VerifiedRankingInputError, match="TARGET_MASTER"):
        build_verified_ranking(
            build_sales_snapshot([source("101", "1", "1")]), target_master, mapping, match_review
        )


def write(path: Path, columns, rows) -> Path:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns))
        writer.writeheader()
        writer.writerows(rows)
    return path


@pytest.mark.parametrize(
    "row",
    [
        {"status": "MAYBE"},
        {"source_model_id": "", "target_id": ""},
        {"source_model_id": "12a"},
        {"target_id": "tgt-1"},
    ],
)
def test_malformed_register_rows_are_refused(tmp_path: Path, row: dict[str, str]) -> None:
    values = dict.fromkeys(REGISTER_COLUMNS, "") | {"source_model_id": "101", "status": "OPEN"}
    path = write(tmp_path / "register.csv", REGISTER_COLUMNS, [values | row])
    with pytest.raises(VerifiedRankingInputError):
        read_register(path)


def test_a_missing_register_file_means_no_entries(tmp_path: Path) -> None:
    assert read_register(tmp_path / "absent.csv") == []


# Audit


def audit(result, snapshot, target_master, mapping, **changes):
    return audit_verified_ranking(
        changes.get("configurations", result.configurations),
        changes.get("top", result.top),
        changes.get("ledger", result.ledger),
        snapshot,
        target_master,
        mapping,
        top_n=changes.get("top_n", 100),
    )


def audited_example():
    return build(
        [source("101", "100", "1"), source("102", "50", "1"), source("103", "20", "1")],
        [target("TGT-000001"), target("TGT-000002", model="ExampleBook 15")],
        [("101", "TGT-000001"), ("102", "TGT-000002"), ("103", "TGT-000002")],
    )


def test_the_audit_passes_a_built_ranking() -> None:
    assert audit(*audited_example()) == []


def test_the_audit_detects_tampering() -> None:
    result, snapshot, target_master, mapping = audited_example()
    first, second = (dict(row) for row in result.configurations)

    wrong_total = [first | {"ranking_gmv_usd": "999"}, second]
    assert any(
        "GMV totals" in i
        for i in audit(
            result, snapshot, target_master, mapping, configurations=wrong_total, top=wrong_total
        )
    )

    swapped = [second | {"rank": "1"}, first | {"rank": "2"}]
    assert any(
        "deterministic order" in i
        for i in audit(
            result, snapshot, target_master, mapping, configurations=swapped, top=swapped
        )
    )

    repeated = [first, first | {"rank": "2"}]
    assert any(
        "repeats a target_id" in i
        for i in audit(
            result, snapshot, target_master, mapping, configurations=repeated, top=repeated
        )
    )

    moved = [dict(row) for row in result.ledger]
    moved[2]["target_id"] = "TGT-000001"
    assert any(
        "is not mapped" in i for i in audit(result, snapshot, target_master, mapping, ledger=moved)
    )

    assert any(
        "first 1 rows" in i
        for i in audit(result, snapshot, target_master, mapping, top=result.configurations, top_n=1)
    )


# CLI


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_rows(path: Path, rows: list[list[str]]) -> Path:
    with path.open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows(rows)
    return path


def cli_inputs(tmp_path: Path) -> list[str]:
    ranking = write(
        tmp_path / "ranking.csv",
        RANKING_REQUIRED_COLUMNS,
        [source("101", "100", "1"), source("102", "50", "1"), source("201", "5", "0")],
    )
    tm = write_rows(
        tmp_path / "tm.csv",
        target_values(target("TGT-000001"), target("TGT-000002", model="ExampleBook 15")),
    )
    smm = write_rows(
        tmp_path / "smm.csv",
        [
            SOURCE_MODEL_MAP_HEADERS,
            map_row(source_model_id="101", target_id="TGT-000001"),
            map_row(source_model_id="102", target_id="TGT-000002"),
        ],
    )
    mr = write_rows(
        tmp_path / "mr.csv",
        [MATCH_REVIEW_HEADERS, review("101"), review("102", model="ExampleBook 15")],
    )
    return [
        "--ranking",
        str(ranking),
        "--source-top-100",
        str(tmp_path / "none.csv"),
        "--target-master",
        str(tm),
        "--source-model-map",
        str(smm),
        "--match-review",
        str(mr),
        "--identity-register",
        str(tmp_path / "no_register.csv"),
    ]


def test_cli_builds_all_outputs_and_the_audit_passes(tmp_path: Path, capsys) -> None:
    inputs = cli_inputs(tmp_path)
    out = tmp_path / "out"

    assert load_script("build_verified_top_100").main([*inputs, "--output-dir", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "2026-08 (full calendar month)" in printed
    assert "2026-09 month-to-date, as extracted on 2026-09-28" in printed
    assert "Qualifying unique configurations: 2" in printed
    assert "Selected: 2 of 100; shortfall: 98." in printed
    assert "TGT-000001" not in printed  # IDs stay in the files
    for name in OUTPUT_FILES.values():
        assert (out / name).is_file()
    with (out / OUTPUT_FILES["top"]).open(encoding="utf-8") as handle:
        top = list(csv.DictReader(handle))
    assert list(top[0]) == list(CONFIGURATION_COLUMNS)
    with (out / OUTPUT_FILES["ledger"]).open(encoding="utf-8") as handle:
        assert list(csv.DictReader(handle).fieldnames or []) == list(LEDGER_COLUMNS)

    audit_args = [
        a
        for a in inputs
        if a
        not in {
            "--source-top-100",
            str(tmp_path / "none.csv"),
            "--identity-register",
            str(tmp_path / "no_register.csv"),
        }
    ]
    assert load_script("audit_verified_top_100").main([*audit_args, "--run-dir", str(out)]) == 0
    assert "Verified configuration audit passed." in capsys.readouterr().out


def test_cli_audit_rejects_a_different_catalog(tmp_path: Path, capsys) -> None:
    inputs = cli_inputs(tmp_path)
    out = tmp_path / "out"
    assert load_script("build_verified_top_100").main([*inputs, "--output-dir", str(out)]) == 0
    tm = Path(inputs[inputs.index("--target-master") + 1])
    tm.write_text(tm.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    audit_args = [
        a
        for a in inputs
        if a
        not in {
            "--source-top-100",
            str(tmp_path / "none.csv"),
            "--identity-register",
            str(tmp_path / "no_register.csv"),
        }
    ]
    assert load_script("audit_verified_top_100").main([*audit_args, "--run-dir", str(out)]) == 1
    assert "differs from the catalog this run was built from" in capsys.readouterr().out


def test_cli_failure_leaves_no_outputs(tmp_path: Path, capsys) -> None:
    inputs = cli_inputs(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    (out / OUTPUT_FILES["top"]).write_text("stale", encoding="utf-8")
    ranking = Path(inputs[inputs.index("--ranking") + 1])
    ranking.write_text(
        ranking.read_text(encoding="utf-8").replace(",101,", ",102,"), encoding="utf-8"
    )  # duplicate source ID

    assert load_script("build_verified_top_100").main([*inputs, "--output-dir", str(out)]) == 1
    assert "stopped" in capsys.readouterr().err
    assert not (out / OUTPUT_FILES["top"]).exists()


def test_cli_from_sheets_reads_only_the_three_catalog_tabs(tmp_path: Path, capsys) -> None:
    inputs = cli_inputs(tmp_path)
    tables = {
        "TARGET_MASTER": target_values(target("TGT-000001")),
        "SOURCE_MODEL_MAP": [
            SOURCE_MODEL_MAP_HEADERS,
            map_row(source_model_id="101", target_id="TGT-000001"),
        ],
        "MATCH_REVIEW": [MATCH_REVIEW_HEADERS, review("101")],
    }
    ranking = inputs[: inputs.index("--target-master")]
    with (
        patch("laptop_tracker.config.Settings.from_env", return_value=settings()),
        patch(
            "laptop_tracker.sheets_source.read_target_table_values", return_value=tables
        ) as reader,
    ):
        code = load_script("build_verified_top_100").main(
            [*ranking, "--from-sheets", "--output-dir", str(tmp_path / "out")]
        )
    assert code == 0
    assert reader.call_args.args[1] == ("TARGET_MASTER", "SOURCE_MODEL_MAP", "MATCH_REVIEW")
    assert "Qualifying unique configurations: 1" in capsys.readouterr().out
