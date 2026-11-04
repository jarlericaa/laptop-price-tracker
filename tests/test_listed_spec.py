"""Listed-spec comparison for broad-family targets (2026-10-20, listed_spec.py).

A family-name target (``Lenovo | IdeaPad | 15.3 | Ryzen 7 | 16 | 512``) can be priced by an offer
whose own evidence states the same brand, family, screen, CPU series, installed RAM and installed
storage, labelled LISTED_SPEC and never an exact product match. Exact-code targets keep the exact
rules; recorded codes, identity conflicts and secondary conflicts are never bypassed.
"""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from scraping_fakes import no_network  # noqa: F401
from test_observation_store import T0
from test_observation_store import offer as stored_offer
from test_price_tracking import AS_OF, TM_SHA, WEEK, ledger, list_rows, target
from test_target_master import target_values

from laptop_tracker.extraction import extract_offer_attributes
from laptop_tracker.listed_spec import (
    BRAND_DIFFERS,
    BRAND_NOT_STATED,
    FAMILY_NOT_STATED,
    IDENTITY_EVIDENCE_CONFLICT,
    LISTED_SPEC_AMBIGUOUS,
    LISTED_SPEC_MATCH,
    LISTED_SPECS_DIFFER,
    NOT_FAMILY_TARGET,
    QUALIFIES,
    RECORDED_CODE_CONFLICT,
    REQUIRED_SPECS_MISSING,
    SECONDARY_CONFLICT,
    ListedSpecMatcher,
    check_target,
)
from laptop_tracker.match_run import matcher_from_validated
from laptop_tracker.matching import ExactMatcher, MatchStatus, configuration_outcome
from laptop_tracker.observation_store import ObservationStore, observation_id
from laptop_tracker.offer_terms import BUNDLE_FLAG_POLICY
from laptop_tracker.offers import Availability
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
    _status_text,
    comparison_plan,
    load_pilot_report,
    summary_plan,
)
from laptop_tracker.target_master import validate_target_master
from laptop_tracker.verified_top import CONFIGURATION_COLUMNS

pytestmark = pytest.mark.usefixtures("no_network")
FAMILY = dict(target_id="TGT-000001", status="ACTIVE", brand="Lenovo", model="IdeaPad",
              screen_size_in="15.3", cpu_series="Ryzen 7", ram_capacity_gb="16",
              storage_capacity_gb="512", manufacturer_model_code="", gpu_model="",
              display_type="", resolution="", cpu_model="Ryzen 7 7735HS")
OFFER = {"brand": "Lenovo", "model": "83K700HXPH", "screen_size_in": "15.3",
         "cpu_series": "Ryzen 7", "ram_capacity_gb": "16", "storage_capacity_gb": "512"}
TITLE = "Lenovo IdeaPad Slim 3 15ARP10 (83K700HXPH) AMD Ryzen 7 7735HS Laptop"


def check(target=None, attributes=None, title=TITLE, codes=()):
    return check_target(FAMILY | (target or {}), OFFER | (attributes or {}), title, codes)


# Unit rules


def test_a_valid_broad_family_offer_is_a_listed_spec_match_never_an_exact_one() -> None:
    result = check()
    assert result.outcome == QUALIFIES
    # The retailer's own code and processor are kept as stated, never replaced by "IdeaPad".
    assert "model 83K700HXPH" in result.offer_details and "7735HS" in result.offer_details
    # The recorded processor is the one the title states: nothing to warn about. (A recorded
    # number that the page contradicts is a SECONDARY_CONFLICT; see test_exact_cpu.py.)
    assert result.warnings == ()
    outcome, detail = configuration_outcome("UNMATCHED", "", ("NO_CORE_MATCH",), OFFER,
                                            "TGT-000001", FAMILY, title=TITLE)
    assert outcome == "LISTED_SPEC_MATCH" and detail.startswith("Listed-spec match —")
    # The exact matcher is unchanged: the family target is not an exact product match.
    assert configuration_outcome("UNMATCHED", "", (), OFFER, "TGT-000001", FAMILY)[0] == (
        "CATALOG_IDENTITY_UNRESOLVED")
    # Spelling variations of the family are presentation only.
    assert check(title="LENOVO Idea Pad Slim 3 (83K700HXPH)").outcome == QUALIFIES


@pytest.mark.parametrize(
    ("attributes", "title", "expected"),
    [
        ({"brand": "HP"}, "HP Victus 15 Ryzen 7", BRAND_DIFFERS),
        ({"brand": ""}, "IdeaPad Slim 3 (83K700HXPH)", BRAND_NOT_STATED),
        ({}, "Lenovo LOQ 15ARP9 Ryzen 7 Laptop", FAMILY_NOT_STATED),  # another family
        ({}, "Lenovo IdeaPadX 15 Ryzen 7", FAMILY_NOT_STATED),  # no prefix matching
    ],
)
def test_another_brand_or_family_is_rejected(attributes, title, expected) -> None:
    assert check(attributes=attributes, title=title).outcome == expected


@pytest.mark.parametrize(
    ("attributes", "expected", "detail"),
    [
        ({"ram_capacity_gb": "8"}, LISTED_SPECS_DIFFER, "ram_capacity_gb: offer 8 vs target 16"),
        ({"screen_size_in": "15.6"}, LISTED_SPECS_DIFFER, "screen_size_in: offer 15.6"),
        ({"storage_capacity_gb": "1000"}, LISTED_SPECS_DIFFER, "storage_capacity_gb"),
        # Distinct CPU series are never collapsed.
        ({"cpu_series": "Ryzen 5"}, LISTED_SPECS_DIFFER, "cpu_series: offer Ryzen 5"),
        # A field the page does not establish is never supplied from the target.
        ({"storage_capacity_gb": ""}, REQUIRED_SPECS_MISSING, "installed storage"),
        ({"cpu_series": ""}, REQUIRED_SPECS_MISSING, "CPU series"),
    ],
)
def test_required_specifications_must_be_established_and_equal(attributes, expected,
                                                                detail) -> None:
    result = check(attributes=attributes)
    assert result.outcome == expected and detail in result.detail


def test_cpu_series_forms_stay_distinct() -> None:
    target = {"cpu_series": "Core i5"}
    assert check(target, {"cpu_series": "Core 5"}).outcome == LISTED_SPECS_DIFFER
    assert check(target, {"cpu_series": "Core Ultra 5"}).outcome == LISTED_SPECS_DIFFER
    assert check(target, {"cpu_series": "Core i5"}).outcome == QUALIFIES


def test_known_conflicts_are_never_bypassed() -> None:
    # A secondary specification the target records and the page contradicts.
    gpu = {"gpu_model": "RTX 4050"}
    assert check(gpu, {"gpu_model": "RTX 3050"}).outcome == SECONDARY_CONFLICT
    assert check(gpu, {"gpu_model": "GeForce RTX4050"}).outcome == QUALIFIES  # same GPU
    assert check(gpu, {}).outcome == QUALIFIES  # the page does not state it: unknown
    assert check({"display_type": "OLED"}, {"display_type": "IPS"}).outcome == (
        SECONDARY_CONFLICT)
    assert check(gpu, codes=("TITLE_SPEC_CONFLICT_GPU_MODEL",)).outcome == SECONDARY_CONFLICT
    # A manufacturer code the target records is an identity requirement: another code in the
    # title, or another designation as the page's model (a machine type such as 83K700HXPH).
    recorded = {"manufacturer_model_code": "AL14-45P-R10U"}
    assert check(recorded).outcome == RECORDED_CODE_CONFLICT
    assert check(recorded, {"model": ""}, title="Lenovo IdeaPad AL14-42P-R8ST").outcome == (
        RECORDED_CODE_CONFLICT)
    assert check(recorded, {"model": "AL14-45P-R10U"},
                 title="Lenovo IdeaPad AL14-45P-R10U").outcome == QUALIFIES
    assert check(recorded, {"model": ""}, title="Lenovo IdeaPad Slim 3").outcome == QUALIFIES
    # Contradictory identity evidence on the page.
    assert check(codes=("MODEL_CLAIM_CONFLICT",)).outcome == IDENTITY_EVIDENCE_CONFLICT
    # Exact-code and product-name targets never use the fallback.
    msi = {"brand": "MSI", "model": "B13UC-3474PH"}
    assert check(msi, title="MSI Thin 15 B13UC-3474PH").outcome == NOT_FAMILY_TARGET
    neo = {"brand": "Apple", "model": "MacBook Neo"}
    assert check(neo, title="Apple MacBook Neo 13").outcome == NOT_FAMILY_TARGET


def test_the_ambiguity_rule() -> None:
    broad = FAMILY
    slim = FAMILY | {"target_id": "TGT-000002", "model": "IdeaPad Slim 3"}
    twin = FAMILY | {"target_id": "TGT-000003", "gpu_model": "RTX 4050"}
    # The most specific family name stated in the title wins; the broader one is recorded.
    result = ListedSpecMatcher([broad, slim]).associate(OFFER, TITLE)
    assert (result.status, result.target_ids, result.broader_ids) == (
        LISTED_SPEC_MATCH, ("TGT-000002",), ("TGT-000001",))
    # Equally specific targets the evidence cannot tell apart: none is chosen.
    result = ListedSpecMatcher([broad, twin]).associate(OFFER, TITLE)
    assert (result.status, result.target_ids, result.ambiguous_ids) == (
        LISTED_SPEC_AMBIGUOUS, (), ("TGT-000001", "TGT-000003"))
    # A verified secondary conflict excludes a target before the rule applies.
    result = ListedSpecMatcher([broad, twin]).associate(OFFER | {"gpu_model": "RTX 3050"}, TITLE)
    assert result.target_ids == ("TGT-000001",)
    # Inactive and listing-only targets never take part.
    inactive = FAMILY | {"target_id": "TGT-000004", "status": "INACTIVE"}
    assert ListedSpecMatcher([inactive]).associate(OFFER, TITLE).status == ""


# Weekly reports


PLAIN = dict(series="", manufacturer_model_code="", resolution="", display_type="")


def catalog(*, family_gpu: str = ""):
    """TGT-000001 a Lenovo IdeaPad family target; TGT-000002 an unrelated code target."""
    family = target("TGT-000001", "IdeaPad", "15.3", "Ryzen 7", "16", "512", brand="Lenovo",
                    gpu_model=family_gpu, cpu_model="Ryzen 7 7735HS", **PLAIN)
    return [family, target("TGT-000002", "EB15-0002", "15.6", "Core 7", "16", "1000")]


def lenovo(slug: str, *, retailer="laptop_factory", price="1000.00", ram="16GB", gpu="",
           code="83K700HXPH", availability=Availability.IN_STOCK, bundle=False, at=T0,
           family="IdeaPad Slim 3", brand="Lenovo", screen="15.3", cpu="AMD Ryzen 7 7735HS"):
    """A title-only offer: every field is the page's own title statement."""
    title = (f"{brand} {family} {code} {screen}\" {cpu} {ram} RAM 512GB SSD "
             f"{gpu}").strip()
    observation = stored_offer(slug, retailer=retailer, slug=slug, specs=(), price=price,
                               availability=availability, bundle=bundle, at=at)
    return replace(observation, raw_title=title)


def report(tmp_path, observations, targets, *, policy=BUNDLE_FLAG_POLICY, list_sha="e" * 64):
    tm = validate_target_master(target_values(*targets))
    assert tm.is_valid, tm.errors.to_dict(orient="records")
    selected = validate_selected_list(list_rows(("TGT-000001", "TGT-000002"), targets),
                                      ledger(), tm, target_master_sha256=TM_SHA,
                                      list_sha256=list_sha)
    with ObservationStore.open(tmp_path / "store.sqlite3") as store:
        store.import_observations(observations, source_label="test", imported_at=AS_OF)
        context = matching_context(tm, target_master_sha256=TM_SHA, source_label="test")
        match_stored_observations(store, context, matcher_from_validated(tm), now=AS_OF)
        stored, matches = store.observations(), store.match_results(context.context_id)
    result = build_price_tracking(stored, matches, selected, context, week=WEEK, as_of=AS_OF,
                                  offer_terms_policy=policy,
                                  listed_spec=ListedSpecMatcher(tm.valid_rows))
    return result, tm


def row(result, target_id="TGT-000001"):
    return next(r for r in result.comparison if r["target_id"] == target_id)


def history(result, observation):
    return next(r for r in result.history if r["observation_id"] == observation_id(observation))


def test_a_listed_spec_price_is_comparable_and_labelled(tmp_path) -> None:
    lf = lenovo("ideapad-lf")
    result, _ = report(tmp_path, [lf], catalog())
    cell = row(result)
    assert cell["LF_Price"] == "1000.00" and cell["LF_Price_Status"] == "SELECTED"
    assert cell["LF_Match_Basis"] == "LISTED_SPEC"
    assert cell["LF_Warnings"].startswith("SPECIFIC_IDENTITY_UNRESOLVED")
    assert "CPU_NUMBER_DIFFERS_FROM_TARGET_RECORD" not in cell["LF_Warnings"]
    assert "83K700HXPH" in cell["LF_Offer_Details"]  # the title, quoted: no model extracted
    assert (cell["Comparison_Basis"], cell["Lowest_Price_Basis"], cell["Lowest_Price_Label"]) == (
        "LISTED_SPEC", "LISTED_SPEC", "Lowest price matching listed specs")
    assert cell["Lowest_In_Stock_Label"] == "Lowest in-stock price matching listed specs"
    # The stored identity is not rewritten: no exact assignment, the association beside it.
    observed = history(result, lf)
    assert (observed["match_status"], observed["target_id"]) == ("REVIEW_REQUIRED", "")
    assert (observed["listed_spec_status"], observed["listed_spec_target_ids"]) == (
        "LISTED_SPEC_MATCH", "TGT-000001")
    assert any(r["review_reason"].startswith("LISTED_SPEC_MATCH TGT-000001")
               for r in result.review)
    assert result.counts["selected_retailer_prices_listed_spec"] == 1


def test_counts_both_minimums_stock_and_bundles(tmp_path) -> None:
    offers = [
        lenovo("lf-bundle", price="900.00", availability=Availability.OUT_OF_STOCK, bundle=True),
        lenovo("lf-plain", price="950.00"),
        lenovo("dbz", retailer="datablitz", price="1000.00"),
        lenovo("pcx-8gb", retailer="pcx", price="500.00", ram="8GB"),  # another configuration
    ]
    result, _ = report(tmp_path, offers, catalog())
    cell = row(result)
    # One price per retailer: the lowest comparable LF offer; the 8GB offer is never a price.
    assert (cell["LF_Price"], cell["DBZ_Price"], cell["PCX_Price"]) == ("900.00", "1000.00", "")
    assert cell["Comparable_Retailer_Count"] == "2"
    assert (cell["Lowest_Price"], cell["Lowest_Price_Stock"], cell["Lowest_Price_Notes"]) == (
        "900.00", "OUT_OF_STOCK", "LF:BUNDLE_OFFER")
    # An out-of-stock price never wins the in-stock minimum.
    assert (cell["Lowest_In_Stock_Price"], cell["Lowest_In_Stock_Price_Retailers"]) == (
        "950.00", "LF")
    assert cell["Lowest_In_Stock_Basis"] == "LISTED_SPEC"
    assert result.counts["selected_retailer_prices_listed_spec"] == 2


def test_previous_week_listed_spec_offers_do_not_price_this_week(tmp_path) -> None:
    old = lenovo("old", at=T0 - timedelta(days=7))
    result, _ = report(tmp_path, [old], catalog())
    cell = row(result)
    assert cell["LF_Price"] == "" and "not carried forward" in cell["LF_Reason"]


def test_unknown_secondary_hardware_stays_visible_across_retailers(tmp_path) -> None:
    # The target records no GPU: both offers satisfy the listed specs, but their GPU differs.
    offers = [lenovo("lf-3050", gpu="RTX 3050"),
              lenovo("dbz-4050", retailer="datablitz", gpu="RTX 4050", price="1100.00")]
    result, _ = report(tmp_path, offers, catalog())
    cell = row(result)
    assert cell["Comparable_Retailer_Count"] == "2"
    assert "GPU RTX 3050" in cell["LF_Offer_Details"]
    assert "GPU RTX 4050" in cell["DBZ_Offer_Details"]
    assert "OFFERS_DISAGREE_GPU_MODEL" in cell["LF_Warnings"]
    assert any(r["review_reason"].startswith("OFFERS_DISAGREE_GPU_MODEL") for r in result.review)


def test_a_recorded_secondary_conflict_excludes_the_offer(tmp_path) -> None:
    conflicting = lenovo("lf-3050", gpu="RTX 3050")
    result, _ = report(tmp_path, [conflicting], catalog(family_gpu="RTX 4050"))
    assert row(result)["LF_Price"] == ""
    assert history(result, conflicting)["listed_spec_status"] == ""


def test_an_exact_assignment_keeps_its_identity_beside_a_broad_association(tmp_path) -> None:
    # Acer: an "Aspire Lite" family target and the coded AL14-45P-R10U target, same specs.
    acer = dict(brand="Acer", series="", resolution="", display_type="")
    targets = [
        target("TGT-000001", "Aspire Lite", "14", "Ryzen 3", "8", "512",
               manufacturer_model_code="", **acer),
        target("TGT-000002", "AL14-45P-R10U", "14", "Ryzen 3", "8", "512", **acer),
    ]
    offer = lenovo("aspire-lite", brand="Acer", family="Aspire Lite", code="AL14-45P-R10U",
                   screen="14", cpu="AMD Ryzen 3 5400U", ram="8GB")
    result, _ = report(tmp_path, [offer], targets)
    observed = history(result, offer)
    # Exact for the code target; listed-spec for the family target; nothing is rewritten.
    assert (observed["match_status"], observed["target_id"]) == ("EXACT", "TGT-000002")
    assert observed["listed_spec_target_ids"] == "TGT-000001"
    coded, family = row(result, "TGT-000002"), row(result)
    assert (coded["LF_Price"], family["LF_Price"]) == ("1000.00", "1000.00")
    assert coded["LF_Match_Basis"] != "LISTED_SPEC" and family["LF_Match_Basis"] == "LISTED_SPEC"
    assert (coded["Comparison_Basis"], coded["Lowest_Price_Label"]) == (
        "EXACT_PRODUCT", "Lowest observed price")
    assert (family["Comparison_Basis"], family["Lowest_Price_Label"]) == (
        "LISTED_SPEC", "Lowest price matching listed specs")
    assert result.counts["selected_retailer_prices_exact_product"] == 1
    assert result.counts["selected_retailer_prices_listed_spec"] == 1


def test_indistinguishable_targets_are_not_priced_and_stay_in_review(tmp_path) -> None:
    # Two equally specific family names that the title both states (a synthetic catalog: the
    # validated catalog refuses two targets with one model text and the same five fields).
    targets = [
        target("TGT-000001", "IdeaPad Slim", "15.3", "Ryzen 7", "16", "512", brand="Lenovo",
               gpu_model="",
               **PLAIN),
        target("TGT-000002", "Slim 3", "15.3", "Ryzen 7", "16", "512", brand="Lenovo",
               gpu_model="RTX 4050", **PLAIN),
    ]
    ambiguous = lenovo("no-gpu")
    result, _ = report(tmp_path, [ambiguous], targets)
    assert row(result)["LF_Price"] == "" and row(result, "TGT-000002")["LF_Price"] == ""
    assert history(result, ambiguous)["listed_spec_ambiguous_target_ids"] == (
        "TGT-000001;TGT-000002")
    assert any(r["review_reason"].startswith("LISTED_SPEC_AMBIGUOUS") for r in result.review)
    assert row(result)["LF_Candidate_Status"] == "AMBIGUOUS_TARGET"
    # A verified GPU conflict tells them apart: TGT-000001 is priced, TGT-000002 is not.
    result, _ = report(tmp_path / "b", [lenovo("gpu-3050", gpu="RTX 3050")], targets)
    assert row(result)["LF_Price"] == "1000.00" and row(result, "TGT-000002")["LF_Price"] == ""


def test_the_exact_matcher_never_assigns_a_family_target_from_listed_specs() -> None:
    observation = lenovo("ideapad")
    tm = validate_target_master(target_values(*catalog()))
    result = ExactMatcher(tm.valid_rows).match_observation(observation)
    assert result.status is not MatchStatus.EXACT and result.target_id is None


# Sheets publisher


def write(path: Path, columns, rows) -> Path:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns))
        writer.writeheader()
        writer.writerows(rows)
    return path


def test_the_publisher_accepts_and_labels_listed_spec_prices(tmp_path) -> None:
    targets = catalog()
    selected_path = write(tmp_path / "top.csv", CONFIGURATION_COLUMNS,
                          list_rows(("TGT-000001", "TGT-000002"), targets))
    sha = hashlib.sha256(selected_path.read_bytes()).hexdigest()
    result, tm = report(tmp_path, [lenovo("lf"), lenovo("dbz", retailer="datablitz",
                                                        price="1200.00")], targets, list_sha=sha)
    folder = tmp_path / "report"
    folder.mkdir()
    write(folder / "weekly_price_comparison.csv", COMPARISON_COLUMNS, result.comparison)
    write(folder / "weekly_price_selections.csv", SELECTION_COLUMNS, result.selections)
    write(folder / "price_history.csv", HISTORY_COLUMNS, result.history)
    write(folder / "price_review_queue.csv", REVIEW_COLUMNS, result.review)
    (folder / "price_tracking_run_summary.json").write_text(json.dumps({
        "report_week": {"start": "2026-10-05", "end_exclusive": "2026-10-12",
                        "timezone": "Asia/Singapore", "start_utc": "2026-10-04T16:00:00.000000Z",
                        "end_utc_exclusive": "2026-10-11T16:00:00.000000Z",
                        "status": "CLOSED_HISTORICAL"},
        "as_of_utc": "2026-10-12T01:00:00.000000Z",
        "sales_snapshot": {"run_id": "20260928T112328+0800", "label": "test"},
        "selected_list": {"configurations": 2, "sha256": sha,
                          "built_on_target_master_sha256": TM_SHA},
        "matching_context": {"context_id": result.comparison[0]["matching_context_id"],
                             "target_master_sha256": TM_SHA},
        "input_sha256": {str(selected_path): sha},
    }))
    scope = write(tmp_path / "scope.csv", CONFIGURATION_COLUMNS,
                  list_rows(("TGT-000001",), targets))
    loaded = load_pilot_report(folder, scope, selected_list=selected_path)
    assert loaded.counts["listed_spec_prices"] == 2
    plan = comparison_plan(loaded)
    header = list(plan.header)
    values = dict(zip(header, [getattr(c, "value", c) for c in plan.rows[0]], strict=True))
    assert values["Laptop Factory status"].startswith(
        "Comparable price; listed-spec match — brand, family, screen, CPU series, installed "
        "RAM and storage agree; specific Shopee model unknown; offer: title")
    assert values["Lowest retailer price basis"] == (
        "Lowest price matching listed specs — winning offer: listed-spec match")
    assert values["Comparison basis"] == "listed-spec match"
    # PILOT_PRICE_SUMMARY: the same price, italic, with a note that it is not an exact match.
    summary = summary_plan(loaded)
    cell = summary.rows[0][summary.header.index("Laptop Factory")]
    assert (cell.value, cell.italic) == (values["Laptop Factory price (PHP)"], True)
    assert cell.note.startswith("LISTED-SPEC MATCH — not an exact product match")
    assert _status_text("SELECTED", "", basis="LISTED_SPEC").startswith(
        "Comparable price; listed-spec match")


# Discovery: broad-family listings are collected within the limits


def family_target(**extra) -> object:
    from laptop_tracker.product_discovery import SearchTarget

    values = dict(target_id="TGT-000001", rank="1", brand="Lenovo", series="", model="IdeaPad",
                  part_number="", core={name: FAMILY[name] for name in (
                      "model", "screen_size_in", "cpu_series", "ram_capacity_gb",
                      "storage_capacity_gb")}, identity_kind="FAMILY_NAME")
    return SearchTarget(**(values | extra))


def ranked(target_, title: str, product_type: str = ""):
    from test_product_discovery import hit, record

    from laptop_tracker.product_discovery import rank_hit

    found = hit(title, product_type=product_type)
    return rank_hit(target_, found, [record(target_, found)])


def test_family_listings_are_collectable_candidates_ranked_by_evidence() -> None:
    from laptop_tracker.product_discovery import (
        COLLECTABLE,
        DIFFERENT_MODEL,
        FAMILY_SPEC_CANDIDATE,
        is_plausible,
    )

    full = ranked(family_target(), "Lenovo IdeaPad Slim 3 15.3\" Ryzen 7 16GB RAM 512GB SSD")
    assert full.category == FAMILY_SPEC_CANDIDATE and full.category in COLLECTABLE
    assert is_plausible(full)
    # An incomplete title is still collected, but does not stop the fallback searches.
    bare = ranked(family_target(), "Lenovo IdeaPad Slim 3 Laptop")
    assert bare.category == FAMILY_SPEC_CANDIDATE and not is_plausible(bare)
    # A known different configuration is never plausible (and is not fetched).
    other = ranked(family_target(), "Lenovo IdeaPad Slim 3 15.3\" Ryzen 7 8GB RAM 512GB SSD")
    assert not is_plausible(other)
    # A recorded code is an identity requirement: another code is not collected for it; the
    # target's own code ranks first.
    recorded = family_target(recorded_code="AL14-45P-R10U", brand="Acer", model="Aspire Lite")
    assert ranked(recorded, "Acer Aspire Lite AL14-42P-R8ST").category == DIFFERENT_MODEL
    own = ranked(recorded, "Acer Aspire Lite AL14-45P-R10U")
    plain = ranked(recorded, "Acer Aspire Lite 14 Laptop")
    assert own.category == plain.category == FAMILY_SPEC_CANDIDATE
    assert own.sort_key < plain.sort_key


@pytest.mark.parametrize(
    ("title", "product_type"),
    [
        ("Lenovo IdeaCentre AIO 3 24\" Ryzen 7", ""),
        ("Lenovo Legion Go Handheld Ryzen Z1 Extreme", ""),
        ("ASUS RT-AX58U Router", ""),
        ("Lenovo IdeaPad Desktop Tower", ""),
        ("Lenovo Monitor L24i", "Monitor"),
    ],
)
def test_products_that_are_not_laptops_are_rejected_before_any_request(title,
                                                                        product_type) -> None:
    from laptop_tracker.product_discovery import NOT_A_LAPTOP

    assert ranked(family_target(), title, product_type).category == NOT_A_LAPTOP


def test_a_laptop_with_a_monitor_in_its_bundle_is_still_a_laptop() -> None:
    from laptop_tracker.product_discovery import FAMILY_SPEC_CANDIDATE

    title = "Lenovo IdeaPad Slim 3 Laptop + FREE Monitor"
    assert ranked(family_target(), title).category == FAMILY_SPEC_CANDIDATE


def test_a_proven_shared_title_states_the_gpu_of_every_variant() -> None:
    # The PC Central HP Victus case (2026-10-19 saved data): variants differ only in RAM and
    # storage, so the parent title RTX 5050 is the selected variant GPU too, and it conflicts
    # with a target recording RTX 4050. Without the proven matrix the title supplies nothing.
    from test_extraction_layouts import BOUND, primary
    from test_owner_policy_rules import matrix, pcc

    # The fixture's shared description has a "Graphics" group stating RTX 3050.
    agreeing = "Example Thin 15 B13UC-0000PH | RTX 3050 | 16GB | 512GB SSD"
    proven = pcc((*BOUND, matrix()), title=agreeing)
    assert primary(proven)["gpu_model"] == ("EXTRACTED", "RTX 3050")
    assert primary(proven)["ram_capacity_gb"] == ("EXTRACTED", "16")  # the variant still governs
    # A parent title contradicting the page's own graphics row is kept and routed to review.
    title = "Example Thin 15 B13UC-0000PH | RTX 5050 8GB | 16GB | 512GB SSD"
    assert primary(pcc((*BOUND, matrix()), title=title))["gpu_model"][0] == "AMBIGUOUS"
    assert "gpu_model" not in primary(pcc(BOUND, title=title))  # matrix not proven: nothing


def test_a_labelled_graphics_row_states_the_gpu() -> None:
    # The Laptop Factory HP Victus 15-FA2661TX page (fresh sample, 2026-10-20): "Graphics: NVIDIA
    # GeForce RTX 5050 Laptop GPU". Against a Victus target recording RTX 4050 it is a conflict.
    from test_extraction_layouts import offer as layout_offer
    from test_extraction_layouts import primary

    page = layout_offer("laptop_factory", (
        ("Model Name", "HP Victus Gaming Laptop 15-FA2661TX"),
        ("Processor", "Intel® Core™ i5-13420H"), ("RAM", "16 GB DDR5-5600"),
        ("Storage", "512 GB PCIe® Gen4 NVMe™ M.2 SSD"),
        ("Graphics", "NVIDIA® GeForce RTX™ 5050 Laptop GPU (8 GB GDDR7 dedicated)"),
        ("Display Size", "15.6″ diagonal, FHD (1920 x 1080), 144 Hz, IPS"),
    ), title="HP Victus Gaming Laptop 15-FA2661TX")
    assert primary(page)["gpu_model"] == ("EXTRACTED", "RTX 5050")
    victus = {"target_id": "TGT-000012", "status": "ACTIVE", "brand": "HP", "model": "Victus",
              "screen_size_in": "15.6", "cpu_series": "Core i5", "ram_capacity_gb": "16",
              "storage_capacity_gb": "512", "gpu_model": "RTX 4050"}
    attributes = extract_offer_attributes(page).values
    assert check_target(victus, attributes, page.raw_title).outcome == SECONDARY_CONFLICT
    # Integrated graphics are not read: unknown, never a conflict.
    integrated = layout_offer("laptop_factory", (("Graphics", "Intel® UHD Graphics"),))
    assert "gpu_model" not in primary(integrated)
