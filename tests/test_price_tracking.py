"""Phase 9 weekly comparisons and price history. Synthetic targets and observations only."""

from __future__ import annotations

import csv
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from scraping_fakes import no_network  # noqa: F401
from test_observation_store import MODELS, NOW, T0, offer
from test_supporting_tables import map_row
from test_target_master import target_row, target_values
from test_verified_top import load_script

from laptop_tracker.match_run import matcher_from_validated
from laptop_tracker.observation_store import ObservationStore, observation_id
from laptop_tracker.offers import (
    OBSERVATION_COLUMNS,
    OFFER_DETAIL_COLUMNS,
    Availability,
    PriceType,
)
from laptop_tracker.price_tracking import (
    COMPARISON_COLUMNS,
    PRICE_COLUMNS,
    PriceTrackingInputError,
    ReportWeek,
    build_price_tracking,
    build_source_ledger,
    match_stored_observations,
    matching_context,
    parse_as_of,
    parse_week_start,
    validate_selected_list,
    week_start_of,
)
from laptop_tracker.supporting_tables import (
    MATCH_REVIEW_HEADERS,
    SOURCE_MODEL_MAP_HEADERS,
    validate_source_model_map,
)
from laptop_tracker.target_master import validate_target_master
from laptop_tracker.verified_top import CONFIGURATION_COLUMNS, LEDGER_COLUMNS, money

pytestmark = pytest.mark.usefixtures("no_network")

WEEK = ReportWeek(date(2026, 10, 5))
# Monday 2026-10-12 09:00 in Asia/Singapore: the report week has ended.
AS_OF = datetime(2026, 10, 12, 1, 0, tzinfo=UTC)
TM_SHA = "f" * 64


def target(target_id: str, model: str, screen: str, cpu: str, ram: str, storage: str, **extra):
    values = dict(
        target_id=target_id,
        model=model,
        manufacturer_model_code=model,
        series="ExampleBook",
        screen_size_in=screen,
        cpu_series=cpu,
        ram_capacity_gb=ram,
        storage_capacity_gb=storage,
        resolution="1920x1080",
        display_type="IPS",
        refresh_rate_hz="",
    )
    return target_row(**(values | extra))


TARGETS = [
    target("TGT-000001", "EB14-0001", "14", "Core 5", "16", "512"),
    # A stated 60Hz refresh rate lets a 165Hz offer show the secondary-conflict safeguard.
    target("TGT-000002", "EB15-0002", "15.6", "Core 7", "16", "1000", refresh_rate_hz="60"),
    target("TGT-000003", "EB16-0003", "16", "Core 7", "32", "1000", resolution="1920x1200"),
]
SOURCES = {"TGT-000001": [("101", "100.5", "10.25"), ("102", "50", "5")], "TGT-000002": [
    ("201", "80", "8")
]}


def ledger() -> list[dict[str, str]]:
    rows = []
    for target_id, sources in SOURCES.items():
        for model_id, m1, mtd in sources:
            rows.append(
                dict.fromkeys(LEDGER_COLUMNS, "")
                | {
                    "model_id": model_id,
                    "shop_id": "10",
                    "item_id": f"9{model_id}",
                    "item_name": f"Synthetic listing {model_id}",
                    "m_minus_1_gmv_usd": m1,
                    "mtd_gmv_usd": mtd,
                    # Phase 4 stores float sums; the list total is still M-1 + MTD.
                    "ranking_gmv_usd": str(float(m1) + float(mtd)),
                    "in_original_source_top_100": "True",
                    "target_id": target_id,
                    "contribution_status": "CONTRIBUTING",
                }
            )
    rows.append(
        dict.fromkeys(LEDGER_COLUMNS, "")
        | {"model_id": "999", "contribution_status": "EXCLUDED", "reason_code": "UNMAPPED"}
    )
    return rows


def list_rows(ids=("TGT-000001", "TGT-000002"), targets=TARGETS, sha=TM_SHA):
    header = target_values(*targets)[0]
    catalog = {row[0]: dict(zip(header, row, strict=True)) for row in targets}
    rows = []
    for rank, target_id in enumerate(ids, start=1):
        record = catalog.get(target_id, catalog["TGT-000001"])
        sources = SOURCES.get(target_id, SOURCES["TGT-000001"])
        m1 = sum((Decimal(s[1]) for s in sources), Decimal(0))
        mtd = sum((Decimal(s[2]) for s in sources), Decimal(0))
        rows.append(
            dict.fromkeys(CONFIGURATION_COLUMNS, "")
            | {name: record[name] for name in ("brand", "series", "model", "screen_size_in",
                                               "cpu_series", "ram_capacity_gb",
                                               "storage_capacity_gb")}
            | {
                "rank": str(rank),
                "target_id": target_id,
                "m_minus_1_gmv_usd": money(m1),
                "mtd_gmv_usd": money(mtd),
                "ranking_gmv_usd": money(m1 + mtd),
                "contributing_source_count": str(len(sources)),
                "contributing_source_model_ids": ";".join(s[0] for s in sources),
                "sales_run_id": "20260928T112328+0800",
                "sales_extract_date": "2026-09-28",
                "sales_m_minus_1_month": "2026-08",
                "sales_mtd_month": "2026-09",
                "target_master_sha256": sha,
            }
        )
    return rows


def selected_list(ids=("TGT-000001", "TGT-000002"), targets=TARGETS, rows=None, tm_sha=TM_SHA):
    tm = validate_target_master(target_values(*targets))
    return validate_selected_list(
        rows if rows is not None else list_rows(ids, targets),
        ledger(),
        tm,
        target_master_sha256=tm_sha,
        list_sha256="e" * 64,
    )


def run(
    tmp_path: Path,
    observations,
    *,
    ids=("TGT-000001", "TGT-000002"),
    targets=TARGETS,
    tm_sha=TM_SHA,
    week=WEEK,
    as_of=AS_OF,
    history_start=None,
    forced: dict[str, str] | None = None,
    store_name="store.sqlite3",
):
    tm = validate_target_master(target_values(*targets))
    assert tm.is_valid
    selected = validate_selected_list(
        list_rows(ids, targets), ledger(), tm, target_master_sha256=tm_sha, list_sha256="e" * 64
    )
    with ObservationStore.open(tmp_path / store_name) as store:
        if observations:
            store.import_observations(observations, source_label="test", imported_at=NOW)
        context = matching_context(tm, target_master_sha256=tm_sha, source_label="test")
        match_stored_observations(store, context, matcher_from_validated(tm), now=NOW)
        stored = store.observations()
        matches = store.match_results(context.context_id)
    for obs_id, target_id in (forced or {}).items():
        matches[obs_id] = replace(
            matches[obs_id],
            match_status="EXACT",
            target_id=target_id,
            candidate_target_ids=(target_id,),
            reason_codes=(),
        )
    return build_price_tracking(
        stored, matches, selected, context, week=week, as_of=as_of, history_start=history_start
    )


def row(result, target_id: str) -> dict[str, str]:
    return next(r for r in result.comparison if r["target_id"] == target_id)


def selection(result, target_id: str, code: str = "LF") -> dict[str, str]:
    return next(
        r for r in result.selections if r["target_id"] == target_id and r["retailer_code"] == code
    )


def history(result, observation) -> dict[str, str]:
    key = observation_id(observation)
    return next(r for r in result.history if r["observation_id"] == key)


# Matching outcomes


def test_exact_unmatched_and_review_required_offers(tmp_path: Path) -> None:
    exact = offer()
    renamed = tuple(
        ("Model Name", "ExampleBrand ExampleBook 14 EB99-9999") if label == "Model Name" else (
            label,
            value,
        )
        for label, value in MODELS["EB14-0001"]
    )
    # The title names the same code as the Model Name row; a title naming another code would be
    # a genuine model conflict (review), not an unmatched offer.
    unmatched = offer("EB99-9999", slug="eb99-9999", specs=renamed, price="500.00")
    no_ram = tuple(pair for pair in MODELS["EB14-0001"] if pair[0] != "RAM")
    missing_core = offer(slug="eb14-no-ram", specs=no_ram, price="400.00")
    conflict_specs = tuple(
        (label, value + ", 165 Hz") if label == "Display Size" else (label, value)
        for label, value in MODELS["EB15-0002"]
    )
    secondary = offer("EB15-0002", specs=conflict_specs, price="300.00")

    result = run(tmp_path, [exact, unmatched, missing_core, secondary])

    first = row(result, "TGT-000001")
    assert (first["LF_Price"], first["LF_Price_Status"]) == ("1000.00", "SELECTED")
    # The cheaper unmatched and review-required offers never become target prices.
    assert row(result, "TGT-000002")["LF_Price"] == ""
    assert row(result, "TGT-000002")["LF_Price_Status"] == "REVIEW_REQUIRED_ONLY"
    assert history(result, unmatched)["match_status"] == "UNMATCHED"
    assert history(result, unmatched)["comparability_status"] == "NOT_MATCHED"
    assert history(result, missing_core)["comparability_status"] == "REVIEW_REQUIRED"
    assert "SECONDARY_CONFLICT" in history(result, secondary)["match_reason_codes"]
    assert history(result, secondary)["target_id"] == ""
    reasons = sorted(r["review_reason"] for r in result.review)
    assert reasons == ["MATCH_REVIEW_REQUIRED", "MATCH_REVIEW_REQUIRED", "MATCH_UNMATCHED"]
    assert len(result.history) == 4
    assert result.counts["week_match_EXACT"] == 1


# Selection


def test_lowest_comparable_price_is_selected_per_retailer_without_averaging(tmp_path) -> None:
    lf_a = offer(slug="a", price="1000.00")
    lf_b = offer(slug="b", price="950.50", price_type=PriceType.SALE)
    dbz = offer(retailer="datablitz", price="990.00")
    pcx = offer(retailer="pcx", price="950.50")
    forced = {observation_id(o): "TGT-000001" for o in (dbz, pcx)}

    result = run(tmp_path, [lf_a, lf_b, dbz, pcx], forced=forced)

    first = row(result, "TGT-000001")
    assert (first["LF_Price"], first["DBZ_Price"], first["PCX_Price"]) == (
        "950.50",
        "990.00",
        "950.50",
    )
    assert (first["PCC_Price"], first["PCC_Price_Status"]) == ("", "NO_OBSERVATION_IN_WEEK")
    assert first["Lowest_Price"] == "950.50"
    assert first["Lowest_Price_Retailers"] == "LF;PCX"
    assert first["Comparable_Retailer_Count"] == "3"
    chosen = selection(result, "TGT-000001")
    assert chosen["selection_reason"] == "LOWEST_OF_2_COMPARABLE_OFFERS"
    assert (chosen["observation_id"], chosen["price_type"], chosen["regular_price"]) == (
        observation_id(lf_b),
        "SALE",
        "1050.50",
    )
    assert chosen["offer_url"] == lf_b.offer_url
    assert chosen["retrieved_at_utc"] == "2026-10-07T04:00:00.000000Z"
    assert chosen["retrieved_at_local"] == "2026-10-07T12:00:00+08:00"
    # Every underlying observation is preserved.
    assert {r["observation_id"] for r in result.history} == {
        observation_id(o) for o in (lf_a, lf_b, dbz, pcx)
    }


def test_ties_break_deterministically_whatever_the_import_order(tmp_path: Path) -> None:
    offers = [
        offer(slug="a", price="900.00"),
        offer(slug="c", price="900.00", at=T0 + timedelta(hours=1)),
        offer(slug="b", price="900.00", at=T0 + timedelta(hours=1)),
    ]
    forward = run(tmp_path, offers, store_name="forward.sqlite3")
    backward = run(tmp_path, list(reversed(offers)), store_name="backward.sqlite3")

    for result in (forward, backward):
        chosen = selection(result, "TGT-000001")
        # Latest retrieval first, then the offer URL.
        assert chosen["observation_id"] == observation_id(offers[2])
        assert chosen["tied_offers"] == "3"
        assert "TIE_OF_3_BROKEN_BY_LATEST_RETRIEVAL" in chosen["selection_reason"]
    assert forward.comparison == backward.comparison
    assert forward.selections == backward.selections


def test_missing_prices_are_blank_with_a_reason_and_never_zero(tmp_path: Path) -> None:
    result = run(tmp_path, [])

    assert len(result.comparison) == 2
    for comparison in result.comparison:
        assert all(comparison[column] == "" for column in PRICE_COLUMNS)
        assert comparison["LF_Price_Status"] == "NO_OBSERVATION_IN_WEEK"
        assert (comparison["Lowest_Price"], comparison["Comparable_Retailer_Count"]) == ("", "0")
    assert list(result.comparison[0]) == list(COMPARISON_COLUMNS)
    assert result.counts["selected_targets_with_comparable_price"] == 0


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"currency": "USD"}, "CURRENCY_NOT_PHP"),
        ({"availability": Availability.PREORDER}, "AVAILABILITY_PREORDER"),
        ({"availability": Availability.UNKNOWN}, "AVAILABILITY_UNKNOWN"),
        ({"bundle": True}, ""),
        ({"bundle": None}, "BUNDLE_STATUS_UNKNOWN"),
        ({"price_type": None}, "PRICE_TYPE_UNKNOWN"),
    ],
)
def test_price_rules_keep_offers_in_history_with_their_status(tmp_path, changes, reason) -> None:
    excluded = offer(price="10.00", **changes)

    result = run(tmp_path, [excluded])

    if not reason:
        comparison = row(result, "TGT-000001")
        assert comparison["LF_Price"] == "10.00"
        assert comparison["LF_Warnings"] == "BUNDLE_OFFER"
        assert comparison["Lowest_Price_Notes"] == "LF:BUNDLE_OFFER"
        assert comparison["Lowest_In_Stock_Notes"] == "LF:BUNDLE_OFFER"
        assert selection(result, "TGT-000001")["price_notes"] == "BUNDLE_OFFER"
        kept = history(result, excluded)
        assert (kept["comparability_status"], kept["comparability_reasons"]) == ("COMPARABLE", "")
        assert kept["bundle_flag"] == "TRUE"
        assert result.review == []
        return
    assert row(result, "TGT-000001")["LF_Price"] == ""
    assert row(result, "TGT-000001")["LF_Price_Status"] == "NO_COMPARABLE_OFFER"
    assert selection(result, "TGT-000001")["not_comparable_reasons"] == f"{reason}:1"
    kept = history(result, excluded)
    assert (kept["match_status"], kept["comparability_status"]) == ("EXACT", "NOT_COMPARABLE")
    assert reason in kept["comparability_reasons"].split(";")
    assert kept["price"] == "10.00"
    assert [r["review_reason"] for r in result.review] == ["NOT_COMPARABLE"]


# Weeks, time zone, cut-off, staleness


def test_weeks_follow_monday_boundaries_in_asia_singapore() -> None:
    assert WEEK.start_utc == datetime(2026, 10, 4, 16, tzinfo=UTC)
    assert WEEK.end_utc == datetime(2026, 10, 11, 16, tzinfo=UTC)
    assert week_start_of(datetime(2026, 10, 4, 16, tzinfo=UTC)) == date(2026, 10, 5)
    assert week_start_of(datetime(2026, 10, 4, 15, 59, 59, 999999, tzinfo=UTC)) == date(
        2026, 9, 28
    )
    assert week_start_of(datetime(2026, 10, 11, 15, 59, 59, tzinfo=UTC)) == date(2026, 10, 5)
    assert parse_week_start("2026-10-05") == WEEK
    assert parse_as_of("2026-10-12T09:00:00+08:00") == AS_OF
    for bad in ("2026-10-06", "05/10/2026"):
        with pytest.raises(PriceTrackingInputError):
            parse_week_start(bad)
    for bad in ("2026-10-12T09:00:00", "2026-10-12", "soon"):
        with pytest.raises(PriceTrackingInputError):
            parse_as_of(bad)


def test_observations_are_assigned_to_weeks_in_the_reporting_time_zone(tmp_path: Path) -> None:
    before = offer(slug="a", price="800.00", at=datetime(2026, 10, 4, 15, 59, 59, tzinfo=UTC))
    start = offer(slug="b", price="900.00", at=datetime(2026, 10, 4, 16, tzinfo=UTC))
    end = offer(slug="c", price="950.00", at=datetime(2026, 10, 11, 15, 59, 59, tzinfo=UTC))
    after = offer(slug="d", price="700.00", at=datetime(2026, 10, 11, 16, tzinfo=UTC))

    result = run(
        tmp_path, [before, start, end, after], as_of=datetime(2026, 10, 20, tzinfo=UTC)
    )

    chosen = selection(result, "TGT-000001")
    assert (chosen["price"], chosen["comparable_offers"]) == ("900.00", "2")
    assert history(result, before)["report_week_start"] == "2026-09-28"
    assert history(result, end)["report_week_start"] == "2026-10-05"
    # The next week's observation is outside this report and its history window.
    assert observation_id(after) not in {r["observation_id"] for r in result.history}
    assert result.counts["observations_outside_history_window"] == 1


def test_as_of_cuts_off_later_observations_and_marks_the_week_in_progress(tmp_path) -> None:
    on_time = offer(slug="a", price="1000.00", at=T0)
    later = offer(slug="b", price="10.00", at=T0 + timedelta(seconds=1))

    result = run(tmp_path, [on_time, later], as_of=T0)

    assert row(result, "TGT-000001")["week_status"] == "IN_PROGRESS"
    assert row(result, "TGT-000001")["LF_Price"] == "1000.00"
    assert len(result.history) == 1
    assert run(tmp_path, [], store_name="x.sqlite3").comparison[0]["week_status"] == (
        "CLOSED_HISTORICAL"
    )
    with pytest.raises(PriceTrackingInputError):
        run(tmp_path, [], as_of=WEEK.start_utc - timedelta(seconds=1), store_name="y.sqlite3")


def test_stale_prices_are_not_carried_forward(tmp_path: Path) -> None:
    old = offer(price="1000.00", at=T0 - timedelta(days=7))

    result = run(tmp_path, [old])

    assert row(result, "TGT-000001")["LF_Price"] == ""
    assert row(result, "TGT-000001")["LF_Price_Status"] == "NO_OBSERVATION_IN_WEEK"
    reference = selection(result, "TGT-000001")
    assert reference["stale_last_comparable_price"] == "1000.00"
    assert reference["stale_last_comparable_retrieved_at_utc"] == "2026-09-30T04:00:00.000000Z"
    series = [
        r for r in result.series if r["target_id"] == "TGT-000001" and r["retailer_code"] == "LF"
    ]
    assert [(r["report_week_start"], r["price"], r["change_status"]) for r in series] == [
        ("2026-09-28", "1000.00", "FIRST_WEEK_IN_RANGE"),
        ("2026-10-05", "", "NO_PRICE"),
    ]


def test_each_offer_is_represented_by_its_latest_observation_in_the_week(tmp_path) -> None:
    monday = offer(price="900.00", at=datetime(2026, 10, 5, 2, tzinfo=UTC))
    friday = offer(
        price="1000.00",
        at=datetime(2026, 10, 9, 2, tzinfo=UTC),
        availability=Availability.OUT_OF_STOCK,
    )

    result = run(tmp_path, [monday, friday])

    assert row(result, "TGT-000001")["LF_Price_Status"] == "SELECTED"
    assert row(result, "TGT-000001")["LF_Price"] == "1000.00"
    assert selection(result, "TGT-000001")["not_comparable_reasons"] == ""
    assert history(result, friday)["availability"] == "OUT_OF_STOCK"
    assert selection(result, "TGT-000001")["observations_in_week"] == "2"
    assert history(result, monday)["latest_for_offer_in_week"] == "False"
    assert history(result, friday)["latest_for_offer_in_week"] == "True"


def test_price_changes_are_calculated_only_between_comparable_observations(tmp_path) -> None:
    times = [T0 + timedelta(hours=hour) for hour in range(4)]
    first = offer(price="1000.10", at=times[0])
    sold_out = offer(price="950.00", at=times[1], availability=Availability.OUT_OF_STOCK)
    higher = offer(price="1100.10", at=times[2])
    same = offer(price="1100.10", at=times[3])

    result = run(tmp_path, [first, sold_out, higher, same])

    assert history(result, first)["price_change_status"] == "FIRST_COMPARABLE"
    assert history(result, sold_out)["price_change_status"] == "DECREASED"
    assert history(result, sold_out)["price_change"] == "-50.10"
    rise = history(result, higher)
    assert (rise["price_change"], rise["price_change_status"]) == ("150.10", "INCREASED")
    assert rise["previous_comparable_price"] == "950.00"
    assert rise["observations_since_previous_comparable"] == "0"
    unchanged = history(result, same)
    assert (unchanged["price_change"], unchanged["price_change_status"]) == ("0.00", "UNCHANGED")
    assert row(result, "TGT-000001")["LF_Price"] == "1100.10"


def test_weekly_series_separates_missing_weeks_and_representative_offer_changes(tmp_path) -> None:
    def at(day: int, month: int = 10) -> datetime:
        return datetime(2026, month, day, 4, tzinfo=UTC)

    observations = [
        offer(slug="a", price="1000.00", at=at(23, 9)),
        offer(slug="a", price="1000.00", at=at(30, 9), availability=Availability.PREORDER),
        offer(slug="b", price="900.00", at=at(7)),
        offer(slug="a", price="950.00", at=at(7)),
        offer(slug="a", price="880.00", at=at(14)),
    ]

    result = run(
        tmp_path,
        observations,
        week=ReportWeek(date(2026, 10, 12)),
        as_of=datetime(2026, 10, 20, tzinfo=UTC),
        history_start=date(2026, 9, 21),
    )

    series = [
        (r["report_week_start"], r["price_status"], r["price"], r["price_change"],
         r["change_status"], r["representative_offer_changed"])
        for r in result.series
        if r["target_id"] == "TGT-000001" and r["retailer_code"] == "LF"
    ]
    assert series == [
        ("2026-09-21", "SELECTED", "1000.00", "", "FIRST_WEEK_IN_RANGE", ""),
        ("2026-09-28", "NO_COMPARABLE_OFFER", "", "", "NO_PRICE", ""),
        ("2026-10-05", "SELECTED", "900.00", "", "NO_PREVIOUS_WEEK_PRICE", ""),
        ("2026-10-12", "SELECTED", "880.00", "-20.00", "DECREASED", "True"),
    ]
    untracked = {
        r["price_status"]
        for r in result.series
        if r["target_id"] == "TGT-000002" and r["retailer_code"] == "LF"
    }
    assert untracked == {"NO_OBSERVATION_IN_WEEK"}
    assert result.counts["history_weeks"] == 4


# Reruns, catalog changes, selected-list membership


def test_repeated_runs_do_not_duplicate_history_and_give_identical_outputs(tmp_path) -> None:
    observations = [offer(), offer("EB15-0002", price="2000.00")]
    first = run(tmp_path, observations)
    second = run(tmp_path, observations)

    assert first.comparison == second.comparison
    assert first.history == second.history
    assert first.series == second.series
    with ObservationStore.open(tmp_path / "store.sqlite3") as store:
        counts = store.counts()
    assert (counts["observations"], counts["import_batches"], counts["match_results"]) == (2, 2, 2)


def test_a_changed_catalog_adds_a_context_and_keeps_earlier_results(tmp_path: Path) -> None:
    original = run(tmp_path, [offer()])
    renamed = [
        target("TGT-000001", "EB14-0001A", "14", "Core 5", "16", "512"),
        *TARGETS[1:],
    ]
    changed = run(tmp_path, [offer()], targets=renamed, tm_sha="d" * 64)

    assert row(original, "TGT-000001")["LF_Price"] == "1000.00"
    assert row(changed, "TGT-000001")["LF_Price"] == ""
    assert [r["review_reason"] for r in changed.review] == ["MATCH_UNMATCHED"]
    assert original.comparison[0]["matching_context_id"] != changed.comparison[0][
        "matching_context_id"
    ]
    with ObservationStore.open(tmp_path / "store.sqlite3") as store:
        assert store.counts()["matching_contexts"] == 2
        assert store.counts()["observations"] == 1
        earlier = store.match_results(original.comparison[0]["matching_context_id"])
    assert [m.match_status for m in earlier.values()] == ["EXACT"]


def test_selected_list_membership_defines_the_comparison_scope(tmp_path: Path) -> None:
    observations = [offer(), offer("EB15-0002", price="2000.00"), offer("EB16-0003")]
    only_first = run(tmp_path, observations, ids=("TGT-000001",))
    only_second = run(tmp_path, observations, ids=("TGT-000002",))

    assert [r["target_id"] for r in only_first.comparison] == ["TGT-000001"]
    assert history(only_first, observations[1])["in_selected_list"] == "False"
    assert sorted(r["review_reason"] for r in only_first.review) == [
        "EXACT_TARGET_NOT_IN_SELECTED_LIST",
        "EXACT_TARGET_NOT_IN_SELECTED_LIST",
    ]
    assert only_first.counts["week_exact_out_of_selected_list"] == 2
    assert row(only_second, "TGT-000002")["LF_Price"] == "2000.00"
    assert len(only_second.history) == 3


# Selected-list and catalog validation


def _listing_only(target_id: str) -> list[str]:
    return target(
        target_id,
        "",
        "15.6",
        "Core 7",
        "16",
        "1000",
        manufacturer_model_code="",
        tracking_mode="LISTING_ONLY",
        shop_id="1",
        item_id="2",
        source_model_id="3",
        tracking_mode_reason="No verifiable model",
        classification_evidence="Synthetic evidence",
    )


def _mutated(**changes):
    rows = list_rows()
    rows[0] = rows[0] | changes
    return rows


@pytest.mark.parametrize(
    ("targets", "rows", "message"),
    [
        (TARGETS, list_rows(("TGT-000001", "TGT-000009")), "not in TARGET_MASTER"),
        ([TARGETS[0], target("TGT-000002", "EB15-0002", "15.6", "Core 7", "16", "1000",
                             status="INACTIVE")], None, "not ACTIVE"),
        ([TARGETS[0], _listing_only("TGT-000002")], None, "not a CONFIGURATION"),
        (TARGETS, _mutated(ram_capacity_gb="8"), "core identity differs"),
        (TARGETS, _mutated(contributing_source_model_ids="101"), "contributors differ"),
        (TARGETS, _mutated(m_minus_1_gmv_usd="1"), "GMV totals differ"),
        (TARGETS, [list_rows()[0], list_rows()[1] | {"rank": "3"}], "ranks"),
        (TARGETS, list_rows(("TGT-000001", "TGT-000001")), "repeats a target_id"),
        (TARGETS, [], "no rows"),
        (TARGETS + [TARGETS[0]], None, "did not pass validation"),
    ],
)
def test_invalid_selected_lists_and_catalogs_are_refused(targets, rows, message) -> None:
    with pytest.raises(PriceTrackingInputError, match=message):
        selected_list(targets=targets, rows=rows)


def test_a_different_catalog_is_accepted_only_when_selected_identities_agree() -> None:
    assert selected_list().catalog_matches_build is True
    later = selected_list(tm_sha="9" * 64)
    assert later.catalog_matches_build is False
    assert [item.target_id for item in later.configurations] == ["TGT-000001", "TGT-000002"]


# Source ledger


def mapping(*links: tuple[str, str]):
    tm = validate_target_master(target_values(*TARGETS))
    return validate_source_model_map(
        [SOURCE_MODEL_MAP_HEADERS, *(map_row(source_model_id=s, target_id=t) for s, t in links)],
        tm,
    )


def sales_rows(**changes) -> list[dict[str, str]]:
    rows = []
    for item in ledger():
        if item["contribution_status"] != "CONTRIBUTING":
            continue
        rows.append(
            {
                "model_id": item["model_id"],
                "run_id": "20260928T112328+0800",
                "extract_date": "2026-09-28",
                "m_minus_1_gmv_usd": item["m_minus_1_gmv_usd"],
                "mtd_gmv_usd": item["mtd_gmv_usd"],
                "ranking_gmv_usd": item["ranking_gmv_usd"],
                "shopee_current_price_php": "39999",
                "price_status": "SELECTED",
            }
            | changes
        )
    return rows


def test_the_source_ledger_keeps_every_contributing_listing() -> None:
    selected = selected_list()
    links = mapping(("101", "TGT-000001"), ("201", "TGT-000002"))

    rows = build_source_ledger(selected, ledger(), links, sales_rows())
    without_sales = build_source_ledger(selected, ledger(), links)

    assert [(r["target_id"], r["model_id"]) for r in rows] == [
        ("TGT-000001", "101"),
        ("TGT-000001", "102"),
        ("TGT-000002", "201"),
    ]
    assert [r["currently_mapped_to_target"] for r in rows] == ["True", "False", "True"]
    assert rows[0]["shopee_price_php_snapshot"] == "39999"
    assert "as extracted 2026-09-28" in rows[0]["sales_period_label"]
    assert without_sales[0]["shopee_price_status"] == "SALES_RANKING_NOT_SUPPLIED"
    # The comparison row lists every source instead of picking one listing.
    assert row_for_ledger(selected) == "101;102"


def row_for_ledger(selected) -> str:
    return selected.by_target["TGT-000001"].list_row["contributing_source_model_ids"]


@pytest.mark.parametrize(
    "rows",
    [sales_rows(run_id="20261005T000000+0800"), sales_rows(mtd_gmv_usd="1"), sales_rows()[1:]],
)
def test_a_sales_ranking_from_another_run_is_refused(rows) -> None:
    with pytest.raises(PriceTrackingInputError):
        build_source_ledger(selected_list(), ledger(), mapping(("101", "TGT-000001")), rows)


# Command line


def write_rows(path: Path, columns, rows) -> Path:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns))
        writer.writeheader()
        writer.writerows(rows)
    return path


def write_values(path: Path, values) -> Path:
    with path.open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows(values)
    return path


def cli_args(tmp_path: Path, observations, targets=TARGETS) -> list[str]:
    obs = write_rows(
        tmp_path / "observations.csv",
        [*OBSERVATION_COLUMNS, *OFFER_DETAIL_COLUMNS],
        [o.to_full_record() for o in observations],
    )
    return [
        "--observations",
        str(obs),
        "--store",
        str(tmp_path / "history" / "store.sqlite3"),
        "--target-master",
        str(write_values(tmp_path / "tm.csv", target_values(*targets))),
        "--source-model-map",
        str(
            write_values(
                tmp_path / "smm.csv",
                [
                    SOURCE_MODEL_MAP_HEADERS,
                    map_row(source_model_id="101", target_id="TGT-000001"),
                    map_row(source_model_id="201", target_id="TGT-000002"),
                ],
            )
        ),
        "--match-review",
        str(write_values(tmp_path / "mr.csv", [MATCH_REVIEW_HEADERS])),
        "--selected-list",
        str(write_rows(tmp_path / "top.csv", CONFIGURATION_COLUMNS, list_rows())),
        "--contributions",
        str(write_rows(tmp_path / "ledger.csv", LEDGER_COLUMNS, ledger())),
        "--no-sales-ranking",
        "--week-start",
        "2026-10-05",
        "--as-of",
        "2026-10-12T09:00:00+08:00",
        "--output-dir",
        str(tmp_path / "out"),
    ]


def test_cli_runs_twice_without_duplicating_history(tmp_path: Path, capsys) -> None:
    script = load_script("track_prices")
    args = cli_args(tmp_path, [offer(), offer("EB15-0002", price="2000.00", content="2")])

    assert script.main(args) == 0
    first = capsys.readouterr().out
    out = tmp_path / "out"
    snapshot = {name: (out / name).read_bytes() for name in script.OUTPUT_FILES.values()
                if name.endswith(".csv")}
    assert script.main(args) == 0
    second = capsys.readouterr().out

    assert "CLOSED_HISTORICAL" in first
    assert "received 2; new 2; already stored 0" in first
    assert "received 2; new 0; already stored 2" in second
    assert "Store: 2 observations (0 new)" in second
    for printed in (first, second):
        assert "TGT-" not in printed and "http" not in printed  # counts only
    assert {name: (out / name).read_bytes() for name in snapshot} == snapshot
    summary = json.loads((out / "price_tracking_run_summary.json").read_text(encoding="utf-8"))
    assert summary["report_week"]["status"] == "CLOSED_HISTORICAL"
    assert summary["store"]["after"]["observations"] == 2
    assert summary["store"]["after"]["import_batches"] == 2
    assert summary["matching_context"]["new_in_this_run"] is False
    assert summary["sales_snapshot"]["extract_date"] == "2026-09-28"
    with (out / "weekly_price_comparison.csv").open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert [(r["target_id"], r["LF_Price"]) for r in rows] == [
        ("TGT-000001", "1000.00"),
        ("TGT-000002", "2000.00"),
    ]


def test_cli_refuses_bad_inputs_before_writing_the_store(tmp_path: Path, capsys) -> None:
    script = load_script("track_prices")
    args = cli_args(tmp_path, [offer()], targets=TARGETS + [TARGETS[0]])

    assert script.main(args) == 1
    assert "before any store write" in capsys.readouterr().err
    assert not (tmp_path / "history" / "store.sqlite3").exists()
    assert not any((tmp_path / "out").iterdir())

    good = cli_args(tmp_path, [offer()])
    week = good.index("--week-start") + 1
    assert script.main(good[:week] + ["2026-10-06"] + good[week + 1 :]) == 1


def test_cli_refuses_a_contradicting_observation_file(tmp_path: Path, capsys) -> None:
    script = load_script("track_prices")
    assert script.main(cli_args(tmp_path, [offer()])) == 0
    capsys.readouterr()

    assert script.main(cli_args(tmp_path, [offer(price="999.00")])) == 1
    assert "contradict" in capsys.readouterr().err
    with ObservationStore.open(tmp_path / "history" / "store.sqlite3") as store:
        [stored] = store.observations()
    assert stored.price_text == "1000.00"
    assert not (tmp_path / "out" / "weekly_price_comparison.csv").exists()


def test_out_of_stock_displayed_price_is_comparable_and_can_be_lowest(tmp_path) -> None:
    sold_out = offer(slug="sold-out", price="10.00", availability=Availability.OUT_OF_STOCK)
    available = offer(slug="available", price="20.00")
    result = run(tmp_path, [sold_out, available])
    assert row(result, "TGT-000001")["LF_Price"] == "10.00"
    assert row(result, "TGT-000001")["Lowest_Price"] == "10.00"
    assert selection(result, "TGT-000001")["comparable_offers"] == "2"
    kept = history(result, sold_out)
    assert kept["availability"] == "OUT_OF_STOCK"
    assert kept["comparability_status"] == "COMPARABLE"
    assert result.review == []


def test_lowest_observed_and_lowest_in_stock_prices_are_separate(tmp_path) -> None:
    sold_out = offer(slug="sold-out", price="10.00", availability=Availability.OUT_OF_STOCK)
    available = offer(slug="available", price="20.00")
    result = run(tmp_path, [sold_out, available])
    first = row(result, "TGT-000001")
    # The inclusive (observed) minimum keeps the out-of-stock price, labeled as such.
    assert (first["Lowest_Price"], first["Lowest_Price_Stock"]) == ("10.00", "OUT_OF_STOCK")
    assert first["Lowest_Price_Offer_URL"].endswith("/sold-out/")
    assert first["LF_Reason"] == "PRICE_SHOWN_WHILE_OUT_OF_STOCK"
    # The in-stock minimum uses the same comparable set, filtered to IN_STOCK.
    assert (first["Lowest_In_Stock_Price"], first["Lowest_In_Stock_Price_Retailers"]) == (
        "20.00", "LF"
    )
    assert first["Lowest_In_Stock_Offer_URL"].endswith("/available/")
    assert first["Lowest_In_Stock_Observed_At"] == first["Lowest_Price_Observed_At"]
    chosen = selection(result, "TGT-000001")
    assert (chosen["availability"], chosen["in_stock_price"]) == ("OUT_OF_STOCK", "20.00")
    assert chosen["match_basis"] == "LABELED_SPECIFICATION"


def test_no_in_stock_offer_leaves_the_in_stock_minimum_blank(tmp_path) -> None:
    sold_out = offer(price="10.00", availability=Availability.OUT_OF_STOCK)
    result = run(tmp_path, [sold_out])
    first = row(result, "TGT-000001")
    assert first["Lowest_Price"] == "10.00"
    assert first["Lowest_In_Stock_Price"] == first["Lowest_In_Stock_Offer_URL"] == ""
    # A configuration without any offer explains its blank cells and never shows zero.
    second = row(result, "TGT-000002")
    assert (second["LF_Price"], second["LF_Reason"]) == ("", "NO_EXACT_OFFER_THIS_WEEK")
    assert second["Lowest_Price"] == second["Lowest_In_Stock_Price"] == ""


def test_out_of_stock_bundle_is_comparable_with_a_note(tmp_path) -> None:
    bundled = offer(price="10.00", availability=Availability.OUT_OF_STOCK, bundle=True)
    result = run(tmp_path, [bundled])
    assert history(result, bundled)["comparability_reasons"] == ""
    assert history(result, bundled)["comparability_status"] == "COMPARABLE"
    comparison = row(result, "TGT-000001")
    assert comparison["LF_Price"] == "10.00"
    assert comparison["LF_Warnings"] == "BUNDLE_OFFER"
    assert comparison["Lowest_Price_Notes"] == "LF:BUNDLE_OFFER"
    assert comparison["Lowest_In_Stock_Price"] == ""
