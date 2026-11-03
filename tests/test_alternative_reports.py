"""Close alternatives in the weekly report (2026-10-27): selection, minimums, counts, identity.

Title-only offers (every specification is the page's own title statement, as in
``test_listed_spec``) against a small MSI catalog: TGT-000001 is Thin 15 B13UC-3474PH with
16GB/512GB, TGT-000002 the same model code recorded with 8GB (the real TGT-000258 / TGT-000219
pair).
"""

from __future__ import annotations

import csv
from dataclasses import replace
from datetime import timedelta

import pytest
from scraping_fakes import no_network  # noqa: F401
from test_observation_store import T0
from test_observation_store import offer as stored_offer
from test_price_tracking import AS_OF, TM_SHA, WEEK, ledger, list_rows, target
from test_target_master import target_values

from laptop_tracker.alternatives import AlternativeEvaluator
from laptop_tracker.listed_spec import ListedSpecMatcher
from laptop_tracker.match_run import matcher_from_validated
from laptop_tracker.observation_store import ObservationStore, observation_id
from laptop_tracker.offers import Availability
from laptop_tracker.price_tracking import (
    ALTERNATIVE_COLUMNS,
    COMPARISON_COLUMNS,
    EVALUATION_COLUMNS,
    build_price_tracking,
    match_stored_observations,
    matching_context,
    validate_selected_list,
)
from laptop_tracker.target_master import validate_target_master

pytestmark = pytest.mark.usefixtures("no_network")

MSI = dict(brand="MSI", series="Thin 15", resolution="", display_type="",
           cpu_model="Core i5-13420H", gpu_model="RTX 3050")


def catalog():
    return [
        target("TGT-000001", "B13UC-3474PH", "15.6", "Core i5", "16", "512", **MSI),
        target("TGT-000002", "B13UC-3474PH", "15.6", "Core i5", "8", "512", **MSI),
    ]


def msi(slug, *, retailer="laptop_factory", price="1000.00", ram="16", storage="512",
        gpu="RTX 3050", cpu="Core i5-13420H", screen="15.6", availability=Availability.IN_STOCK,
        at=T0, code="B13UC-3474PH", family="Thin 15"):
    title = (f'MSI {family} {code} {screen}" Intel {cpu} {ram}GB RAM {storage}GB SSD {gpu} '
             "Gaming Laptop")
    observation = stored_offer(slug, retailer=retailer, slug=slug, specs=(), price=price,
                               availability=availability, at=at)
    return replace(observation, raw_title=title)


def report(tmp_path, observations, targets=None, *, alternatives=True, context_for=None):
    targets = targets or catalog()
    tm = validate_target_master(target_values(*targets))
    assert tm.is_valid, tm.errors.to_dict(orient="records")
    selected = validate_selected_list(
        list_rows(("TGT-000001", "TGT-000002"), targets), ledger(), tm,
        target_master_sha256=TM_SHA, list_sha256="e" * 64)
    with ObservationStore.open(tmp_path / "store.sqlite3") as store:
        store.import_observations(observations, source_label="test", imported_at=AS_OF)
        context = matching_context(tm, target_master_sha256=TM_SHA, source_label="test")
        match_stored_observations(store, context, matcher_from_validated(tm), now=AS_OF)
        stored, matches = store.observations(), store.match_results(context.context_id)
    return build_price_tracking(
        stored, matches, selected, context, week=WEEK, as_of=AS_OF,
        search_context={observation_id(o): ids for o, ids in (context_for or {}).items()},
        listed_spec=ListedSpecMatcher(tm.valid_rows),
        alternatives=AlternativeEvaluator([i.target for i in selected.configurations])
        if alternatives else None)


def row(result, target_id="TGT-000001"):
    return next(r for r in result.comparison if r["target_id"] == target_id)


def alternatives_of(result, target_id="TGT-000001"):
    return [r for r in result.alternatives if r["target_id"] == target_id]


def history(result, observation):
    return next(r for r in result.history if r["observation_id"] == observation_id(observation))


def test_an_offer_assigned_to_another_target_keeps_its_identity_and_gains_an_alternative(
        tmp_path) -> None:
    offer_8gb = msi("lf-8gb", price="700.00", ram="8")
    result = report(tmp_path, [offer_8gb])
    # MSI 8GB is never an exact 16GB match: its identity is the 8GB target.
    stored = history(result, offer_8gb)
    assert (stored["match_status"], stored["target_id"]) == ("EXACT", "TGT-000002")
    assert stored["alternative_target_ids"] == "TGT-000001"
    assert stored["alternative_tags"] == "TGT-000001:ALT 1 · RAM"
    # It prices TGT-000002 as a match and TGT-000001 as a labelled alternative.
    assert (row(result, "TGT-000002")["LF_Price"], row(result, "TGT-000002")["LF_Price_Status"],
            row(result, "TGT-000002")["LF_Comparison_Tag"]) == ("700.00", "SELECTED", "MATCH")
    cell = row(result)
    assert (cell["LF_Price"], cell["LF_Price_Status"]) == ("700.00", "ALTERNATIVE_SELECTED")
    assert cell["LF_Comparison_Tag"] == "ALT 1 · RAM" and cell["LF_Match_Basis"] == "ALTERNATIVE"
    assert cell["LF_Alternative_Differences"] == "RAM: target 16GB → retailer 8GB."
    # An alternative never becomes a matching price, count or minimum.
    assert (cell["Comparable_Retailer_Count"], cell["Lowest_Price"]) == ("0", "")
    assert cell["Comparison_Retailer_Count"] == "1" and cell["Alternative_Retailer_Count"] == "1"
    assert result.counts["selected_retailer_prices"] == 1
    assert result.counts["selected_retailer_alternative_prices"] == 1


def test_the_initiating_target_the_exact_target_and_the_alternative_stay_separate(
        tmp_path) -> None:
    offer_8gb = msi("lf-8gb", price="700.00", ram="8")
    result = report(tmp_path, [offer_8gb], context_for={offer_8gb: ["TGT-000001"]})
    (alt,) = alternatives_of(result)
    # Discovery looked for TGT-000001, the page is exactly TGT-000002, and it is an alternative
    # for TGT-000001: three different facts, none overwriting another.
    assert (alt["initiating_target_ids"], alt["exact_target_id"], alt["target_id"]) == (
        "TGT-000001", "TGT-000002", "TGT-000001")
    stored = history(result, offer_8gb)
    assert (stored["target_id"], stored["search_context_target_ids"]) == (
        "TGT-000002", "TGT-000001")


def test_price_rules_protect_alternatives_like_matches(tmp_path) -> None:
    preorder = msi("lf-preorder", price="300.00", ram="8", availability=Availability.PREORDER)
    ok = msi("dbz", retailer="datablitz", price="800.00", ram="8")
    result = report(tmp_path, [preorder, ok])
    cell = row(result)
    # The excluded offer is listed with its reason but is neither shown nor in any minimum.
    excluded = next(r for r in alternatives_of(result) if r["retailer_code"] == "LF")
    assert excluded["price_eligibility"] == "NOT_COMPARABLE"
    assert "AVAILABILITY_PREORDER" in excluded["price_exclusion_reasons"]
    assert cell["LF_Price"] == "" and cell["LF_Alternative_Offers"] == "0"
    assert (cell["Lowest_Alternative_Price"], cell["Lowest_Alternative_Price_Retailers"]) == (
        "800.00", "DBZ")
    assert cell["Alternative_Retailer_Count"] == "1"


def test_matching_offers_outrank_cheaper_alternatives_in_the_main_display(tmp_path) -> None:
    offers = [msi("lf-16gb", price="1000.00"), msi("lf-8gb", price="700.00", ram="8")]
    result = report(tmp_path, offers)
    cell = row(result)
    assert (cell["LF_Price"], cell["LF_Price_Status"], cell["LF_Comparison_Tag"]) == (
        "1000.00", "SELECTED", "MATCH")
    # The hidden alternative stays in the detailed output and in the alternative minimum.
    (alt,) = alternatives_of(result)
    assert (alt["price"], alt["alternative_tag"], alt["displayed_in_comparison"],
            alt["retailer_has_matching_offer"]) == ("700.00", "ALT 1 · RAM", "FALSE", "TRUE")
    assert (cell["Lowest_Price"], cell["Lowest_Alternative_Price"],
            cell["Lowest_Alternative_Price_Retailers"]) == ("1000.00", "700.00", "LF")
    assert cell["Lowest_Alternative_Price_Difference_Count"] == "LF:1"
    assert cell["Lowest_Alternative_Price_Differences"] == "LF: RAM: target 16GB → retailer 8GB."


def test_alt_1_outranks_a_cheaper_alt_2_but_the_minimum_uses_the_whole_pool(tmp_path) -> None:
    offers = [msi("dbz-alt1", retailer="datablitz", price="900.00", ram="8"),
              msi("dbz-alt2", retailer="datablitz", price="500.00", ram="8", storage="256")]
    result = report(tmp_path, offers)
    cell = row(result)
    assert (cell["DBZ_Price"], cell["DBZ_Comparison_Tag"]) == ("900.00", "ALT 1 · RAM")
    assert cell["DBZ_Alternative_Offers"] == "2"
    assert (cell["Lowest_Alternative_Price"], cell["Lowest_Alternative_Price_Tag"]) == (
        "500.00", "DBZ:ALT 2 · RAM, Storage")
    # One retailer, counted once, whatever its number of offers.
    assert cell["Alternative_Retailer_Count"] == "1"
    shown = {r["observation_id"]: r["displayed_in_comparison"] for r in alternatives_of(result)}
    assert sorted(shown.values()) == ["FALSE", "TRUE"] and len(shown) == 2


def test_alternative_in_stock_minimum_excludes_out_of_stock_offers(tmp_path) -> None:
    offers = [
        msi("lf-oos", price="500.00", ram="8", availability=Availability.OUT_OF_STOCK),
        msi("dbz", retailer="datablitz", price="800.00", ram="8"),
        msi("pcx", retailer="pcx", price="650.00", ram="8", storage="256"),
    ]
    cell = row(report(tmp_path, offers))
    assert (cell["Lowest_Alternative_Price"], cell["Lowest_Alternative_Price_Retailers"],
            cell["Lowest_Alternative_Price_Stock"]) == ("500.00", "LF", "OUT_OF_STOCK")
    assert (cell["Lowest_Alternative_In_Stock_Price"],
            cell["Lowest_Alternative_In_Stock_Price_Retailers"]) == ("650.00", "PCX")
    assert cell["Alternative_Retailer_Count"] == "3"
    assert cell["LF_Reason"].endswith("PRICE_SHOWN_WHILE_OUT_OF_STOCK")


def test_matching_in_stock_minimums_are_unchanged_beside_alternatives(tmp_path) -> None:
    offers = [msi("lf", price="1200.00"),
              msi("dbz-oos", retailer="datablitz", price="1100.00",
                  availability=Availability.OUT_OF_STOCK),
              msi("pcx", retailer="pcx", price="400.00", ram="8")]
    cell = row(report(tmp_path, offers))
    assert (cell["Lowest_Price"], cell["Lowest_Price_Retailers"]) == ("1100.00", "DBZ")
    assert (cell["Lowest_In_Stock_Price"], cell["Lowest_In_Stock_Price_Retailers"]) == (
        "1200.00", "LF")
    assert cell["Comparable_Retailer_Count"] == "2"
    assert cell["Lowest_Alternative_Price"] == "400.00"


def test_cpu_and_gpu_changes_make_alt_2_and_three_differences_are_excluded(tmp_path) -> None:
    alt2 = msi("lf-i7", cpu="Core i7-13620H", gpu="RTX 4050")
    three = msi("dbz-three", retailer="datablitz", ram="8", storage="256", gpu="RTX 4050")
    result = report(tmp_path, [alt2, three])
    cell = row(result)
    assert (cell["LF_Price_Status"], cell["LF_Comparison_Tag"]) == (
        "ALTERNATIVE_SELECTED", "ALT 2 · CPU, GPU")
    assert cell["DBZ_Price"] == "" and cell["DBZ_Comparison_Tag"] == ""
    audit = {(r["target_id"], r["retailer_code"]): r for r in result.evaluations}
    assert audit[("TGT-000001", "DBZ")]["status"] == "EXCLUDED_TOO_MANY_DIFFERENCES"
    assert audit[("TGT-000001", "DBZ")]["differing_dimensions"] == "ram;storage;gpu"


def test_an_unrelated_family_or_brand_is_never_an_alternative(tmp_path) -> None:
    cyborg = msi("lf-cyborg", family="Cyborg 15", code="B13WEKG-1225PH", ram="16")
    asus = replace(msi("dbz-asus", retailer="datablitz"),
                   raw_title='ASUS Vivobook S3607VA 15.6" Core i5 16GB RAM 512GB SSD')
    result = report(tmp_path, [cyborg, asus])
    assert alternatives_of(result) == []
    assert row(result)["LF_Comparison_Tag"] in {"", "RECHECK"}


def test_a_different_model_code_in_the_family_is_a_model_alternative_and_review_is_relabelled(
        tmp_path) -> None:
    other = msi("lf-b12", code="B12UC-1435PH", price="800.00")
    result = report(tmp_path, [other])
    cell = row(result)
    assert (cell["LF_Price_Status"], cell["LF_Comparison_Tag"]) == (
        "ALTERNATIVE_SELECTED", "ALT 1 · Model")
    stored = history(result, other)
    assert stored["target_id"] == ""  # still unassigned: no identity is invented
    reasons = [r["review_reason"] for r in result.review if r["observation_id"] ==
               stored["observation_id"]]
    assert reasons and reasons[0].startswith("CLOSE_ALTERNATIVE ALT 1 · Model for TGT-0000")


def test_previous_week_offers_never_become_current_alternatives(tmp_path) -> None:
    old = msi("lf-old", ram="8", at=T0 - timedelta(days=7))
    result = report(tmp_path, [old])
    assert row(result)["LF_Price"] == "" and result.alternatives == []
    assert history(result, old)["alternative_target_ids"] == ""


def test_observation_timestamps_are_kept(tmp_path) -> None:
    result = report(tmp_path, [msi("lf-8gb", price="700.00", ram="8")])
    (alt,) = alternatives_of(result)
    assert alt["retrieved_at_utc"] == "2026-10-07T04:00:00.000000Z"
    assert alt["observation_id"] in {r["observation_id"] for r in result.history}


def test_without_the_evaluator_the_report_is_matching_only(tmp_path) -> None:
    offers = [msi("lf-16gb", price="1000.00"), msi("dbz-8gb", retailer="datablitz", ram="8")]
    (tmp_path / "plain").mkdir()
    (tmp_path / "full").mkdir()
    plain = report(tmp_path / "plain", offers, alternatives=False)
    full = report(tmp_path / "full", offers)
    # Every matching cell and minimum is identical; only the alternative cell (DBZ) differs.
    shown = {"DBZ_Price", "DBZ_Price_Status", "DBZ_Offer_URL", "DBZ_Stock", "DBZ_Observed_At",
             "DBZ_Match_Basis", "DBZ_Warnings", "DBZ_Offer_Details", "DBZ_Reason",
             "DBZ_Candidate_URL", "DBZ_Candidate_Status", "DBZ_Candidate_Detail",
             "DBZ_Recheck_Count", "DBZ_Recheck_Note"}
    added = {column for column in COMPARISON_COLUMNS if "Alternative" in column
             or column.endswith("_Comparison_Tag") or column == "Comparison_Retailer_Count"}
    for column in COMPARISON_COLUMNS:
        if column not in shown | added:
            assert row(plain)[column] == row(full)[column], column
    assert row(plain)["DBZ_Price"] == "" and row(full)["DBZ_Price"] != ""
    assert plain.alternatives == [] and plain.evaluations == []
    assert all(row(plain)[column] == "" for column in added
               if column.startswith("Lowest_Alternative"))


def test_every_output_column_is_written(tmp_path) -> None:
    result = report(tmp_path, [msi("lf-8gb", ram="8")])
    assert list(result.alternatives[0]) == list(ALTERNATIVE_COLUMNS)
    assert list(result.evaluations[0]) == list(EVALUATION_COLUMNS)
    assert list(result.comparison[0]) == list(COMPARISON_COLUMNS)
    path = tmp_path / "alt.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, ALTERNATIVE_COLUMNS)
        writer.writeheader()
        writer.writerows(result.alternatives)
    assert path.stat().st_size > 0


# Contradictory evidence stays a review case (real extraction of PC Express-shaped pages)


VIVO_TARGET = {
    "target_id": "TGT-000031", "status": "ACTIVE", "brand": "ASUS", "series": "Vivobook",
    "model": "S3607VA-RP181WSM", "screen_size_in": "16", "cpu_series": "Core 5",
    "ram_capacity_gb": "16", "storage_capacity_gb": "512", "tracking_mode": "CONFIGURATION",
}


def evaluate_page(page, target=VIVO_TARGET):
    from laptop_tracker.alternatives import offer_evidence
    from laptop_tracker.extraction import extract_offer_attributes

    evidence = offer_evidence(extract_offer_attributes(page), page.raw_title)
    return AlternativeEvaluator([target]).evaluate(target, evidence)


def test_contradictory_model_identifiers_remain_a_recheck() -> None:
    from test_balanced_policy import PCX_SPECS, pcx

    # The PC Express TUF shape: a Model row (FA506NC) that conflicts with the title's code.
    page = pcx("ASUS Vivobook S3607VA-RP181WSM Laptop",
               specs=(("Model", "FA506NC"), *PCX_SPECS),
               url="https://pcx.com.ph/products/asus-vivobook-s3607va-rp181wsm-laptop")
    result = evaluate_page(page)
    assert result.status == "RECHECK" and "EVIDENCE_CONFLICT_MODEL" in result.reasons
    assert result.difference_count == 0  # a contradiction is not an ordinary difference


def test_contradictory_screen_statements_remain_a_recheck_not_a_screen_difference() -> None:
    from test_balanced_policy import pcx

    # The page's title states 14" and its display row 16": the page contradicts itself.
    page = pcx('ASUS Vivobook S3607VA-RP181WSM 14" Intel Core 5 Laptop')
    result = evaluate_page(page)
    assert result.dimension("screen").state == "CONFLICTING_EVIDENCE"
    assert result.status == "RECHECK" and "screen" not in result.differing
    assert "EVIDENCE_CONFLICT_SCREEN" in result.reasons


def test_a_title_and_specification_disagreeing_about_ram_is_unresolved() -> None:
    from test_balanced_policy import PCX_SPECS, pcx

    specs = tuple((k, "8GB DDR5" if k == "Memory" else v) for k, v in PCX_SPECS)
    page = pcx("ASUS Vivobook S16 S3607VA-RP181WSM 16GB RAM Intel Core 5 Laptop", specs=specs)
    result = evaluate_page(page)
    assert result.dimension("ram").state == "CONFLICTING_EVIDENCE" and result.status == "RECHECK"
    consistent = pcx("ASUS Vivobook S16 S3607VA-RP181WSM Intel Core 5 Laptop", specs=specs)
    assert evaluate_page(consistent).tag == "ALT 1 · RAM"  # a consistent difference qualifies


# Product-name targets: Neo 256GB and 512GB keep distinct identities


def test_neo_256_and_512_stay_distinct_and_are_storage_alternatives_of_each_other(
        tmp_path) -> None:
    neo = dict(brand="Apple", series="", resolution="", display_type="", cpu_model="A18 Pro",
               gpu_model="", manufacturer_model_code="")
    targets = [target("TGT-000001", "MacBook Neo", "13", "Apple A18 Pro", "8", "512", **neo),
               target("TGT-000002", "MacBook Neo", "13", "Apple A18 Pro", "8", "256", **neo)]

    def neo_offer(slug, storage, price):
        title = f"Apple MacBook Neo 13-inch A18 Pro chip 8GB+{storage}GB Silver"
        observation = stored_offer(slug, retailer="datablitz", slug=slug, specs=(), price=price,
                                   availability=Availability.IN_STOCK, at=T0)
        return replace(observation, raw_title=title)

    offers = [neo_offer("neo-512", "512", "54950.00"), neo_offer("neo-256", "256", "46990.00")]
    result = report(tmp_path, offers, targets)
    big, small = history(result, offers[0]), history(result, offers[1])
    assert (big["target_id"], small["target_id"]) == ("TGT-000001", "TGT-000002")
    assert big["alternative_tags"] == "TGT-000002:ALT 1 · Storage"
    assert small["alternative_tags"] == "TGT-000001:ALT 1 · Storage"
    # Each is a match for its own target and a hidden alternative for the other.
    assert (row(result, "TGT-000001")["DBZ_Price"], row(result, "TGT-000001")["DBZ_Price_Status"]
            ) == ("54950.00", "SELECTED")
    assert row(result, "TGT-000001")["Lowest_Alternative_Price"] == "46990.00"
    assert row(result, "TGT-000002")["Lowest_Alternative_Price"] == "54950.00"
