"""Title evidence and recheck links (2026-10-19, second review).

1. A product title's explicit statements are matching evidence: model (a code or a reviewed
   product name such as MacBook Neo), screen, CPU (Apple chip modifiers kept: A18 is not
   A18 Pro), RAM and storage. A title statement fills a field the specifications do not state;
   a statement contradicting the specifications makes the field ambiguous; a shared parent
   title never overrides the selected variant; the URL never establishes a field.
2. A blank comparison cell lists the relevant unconfirmed pages to recheck, with a short reason,
   never as a price.
"""

from __future__ import annotations

import pytest
from scraping_fakes import no_network  # noqa: F401
from test_acceptance_fixes import build
from test_extraction_layouts import BOUND, PCC_TITLE, pcc_offer, primary
from test_extraction_layouts import offer as layout_offer
from test_observation_store import MODELS
from test_observation_store import offer as stored_offer
from test_price_tracking import row

from laptop_tracker.conventions import cpu_series_forms
from laptop_tracker.extraction import ExtractionStatus, extract_offer_attributes
from laptop_tracker.matching import ExactMatcher, MatchStatus, configuration_outcome
from laptop_tracker.observation_store import observation_id
from laptop_tracker.product_discovery import accessory_reason
from laptop_tracker.recheck import (
    FETCHED_NOT_CONFIRMED,
    LISTED_NOT_FETCHED,
    RecheckCandidate,
    discovery_rechecks,
    offer_explanation,
)
from laptop_tracker.sheet_publication import _recheck_cells

pytestmark = pytest.mark.usefixtures("no_network")
E, M, A = "EXTRACTED", "MISSING", "AMBIGUOUS"

# The DataBlitz MacBook Neo page of the 2026-10-19 run (TGT-000002 search): no model row, a
# "Chip" row, and the title stating every core field.
NEO_URL = ("https://ecommerce.datablitz.com.ph/products/apple-macbook-neo-13-8gb-256gb-a18-pro-"
           "chip-with-6-core-cpu-5-core-gpu-indigo-mhff4pp-a?variant=62746282983795")
NEO_TITLE = ('Apple MacBook Neo 13" 8GB+256GB A18 Pro Chip with 6-Core CPU & 5-Core GPU (Indigo) '
             "(MHFF4PP/A)")
NEO_ROWS = (
    ("Color", "Indigo"),
    ("Chip", "Apple A18 Pro chip (6-core CPU, 5-core GPU, 16-core Neural Engine)"),
    ("Memory", "8GB unified memory (60GB/s bandwidth)"),
    ("Storage", "256GB SSD"),
    ("Display", "13.0-inch Liquid Retina (2408-by-1506), 500 nits, sRGB"),
)
NEO = {"model": "MacBook Neo", "screen_size_in": "13", "cpu_series": "Apple A18 Pro",
       "ram_capacity_gb": "8"}
NEO_512 = {"target_id": "TGT-000002", "status": "active", **NEO, "storage_capacity_gb": "512"}
NEO_256 = {"target_id": "TGT-000003", "status": "active", **NEO, "storage_capacity_gb": "256"}


def neo(title: str = NEO_TITLE, rows=NEO_ROWS, url: str = NEO_URL):
    return layout_offer("datablitz", rows, title=title, url=url)


def item(page, field: str):
    return next(e for e in extract_offer_attributes(page).evidence
                if e.field == field and e.status is not ExtractionStatus.SUPPORTING)


def without(*labels: str):
    return tuple(r for r in NEO_ROWS if r[0] not in labels)


# 1. Apple model and CPU from a valid product title


def test_apple_model_and_cpu_come_from_the_title_with_modifiers_kept() -> None:
    page = neo(rows=without("Chip"))
    fields = primary(page)
    # The reviewed product name (registry) is the model; "Indigo" and the part number are not.
    assert fields["model"] == (E, "MacBook Neo")
    assert fields["cpu_series"] == (E, "Apple A18 Pro")
    cpu = item(page, "cpu_series")
    assert cpu.source_label.startswith("title [TITLE_STATEMENT")
    assert cpu.source_text == NEO_TITLE  # the raw title is kept as the evidence
    assert cpu.scope == "TITLE"
    # A18 Pro and A18 are different processors; a bare "M5" is still Apple M5.
    assert cpu_series_forms("A18 Pro Chip") == {"Apple A18 Pro"}
    assert cpu_series_forms("A18 chip") == {"Apple A18"}
    plain = neo(title=NEO_TITLE.replace("A18 Pro Chip", "A18 Chip"), rows=without("Chip"))
    assert primary(plain)["cpu_series"] == (E, "Apple A18")
    assert ExactMatcher([NEO_256]).match_observation(plain).status is not MatchStatus.EXACT


# 2. A title statement supplies a field the specifications omit


def test_a_title_statement_fills_a_field_missing_from_the_specification_rows() -> None:
    page = neo(rows=without("Storage"))
    assert primary(page)["storage_capacity_gb"] == (E, "256")
    storage = item(page, "storage_capacity_gb")
    assert storage.source_label.startswith("title [TITLE_STATEMENT")
    assert "TITLE_SUPPLIED_STORAGE_CAPACITY_GB" in extract_offer_attributes(page).notes
    result = ExactMatcher([NEO_256, NEO_512]).match_observation(page)
    assert (result.status, result.target_id) == (MatchStatus.EXACT, "TGT-000003")
    # Neither the rows nor the title: the field stays missing (the URL's "256gb" is not used).
    untitled = neo(title="Apple MacBook Neo (Indigo)", rows=without("Storage"))
    assert primary(untitled)["storage_capacity_gb"][0] == M
    assert ExactMatcher([NEO_256]).match_observation(untitled).status is (
        MatchStatus.REVIEW_REQUIRED)


# 3. Conflicting title and specification statements


def test_a_title_contradicting_the_specifications_is_kept_and_routed() -> None:
    rows = tuple(("Storage", "512GB SSD") if r[0] == "Storage" else r for r in NEO_ROWS)
    page = neo(rows=rows)  # the title says 256GB, the row says 512GB
    storage = item(page, "storage_capacity_gb")
    assert storage.status is ExtractionStatus.AMBIGUOUS
    assert "512" in storage.value and "256" in storage.value
    extracted = extract_offer_attributes(page)
    assert "TITLE_SPEC_CONFLICT_STORAGE_CAPACITY_GB" in extracted.notes
    result = ExactMatcher([NEO_256, NEO_512]).match_observation(page)
    assert result.status is not MatchStatus.EXACT and result.target_id is None
    assert offer_explanation("IDENTITY_CONFLICT", ("TITLE_SPEC_CONFLICT_STORAGE_CAPACITY_GB",),
                             ()) == "Conflicting title and specifications — storage"


# 4. A shared product title never overrides the selected variant


def test_a_shared_parent_title_does_not_override_the_selected_variant() -> None:
    # The PC Central parent title says 8GB; the confirmed selected variant is 16GB / 512GB.
    assert "8GB" in PCC_TITLE
    page = pcc_offer(BOUND)
    fields = primary(page)
    assert fields["ram_capacity_gb"] == (E, "16")
    notes = extract_offer_attributes(page).notes
    assert not [n for n in notes if n.startswith(("TITLE_SUPPLIED_", "TITLE_SPEC_CONFLICT_"))]
    assert not any(e.source_label.startswith("title [TITLE_STATEMENT")
                   for e in extract_offer_attributes(page).evidence)


# 5. The 256GB DataBlitz MacBook Neo stays separate from the 512GB target


def test_the_256gb_datablitz_neo_is_a_different_configuration_for_the_512gb_target() -> None:
    page = neo()
    extracted = extract_offer_attributes(page)
    result = ExactMatcher([NEO_512, NEO_256]).match_observation(page)
    # It is the 256GB configuration's offer, never the 512GB target's (searched for).
    assert (result.status, result.target_id) == (MatchStatus.EXACT, "TGT-000003")
    alone = ExactMatcher([NEO_512]).match_observation(page)
    assert alone.status is not MatchStatus.EXACT and alone.target_id is None
    attributes = {e.field: e.value for e in extracted.evidence
                  if e.status is ExtractionStatus.EXTRACTED}
    status, _ = configuration_outcome(alone.status.value, "", alone.reason_codes, attributes,
                                      "TGT-000002", NEO_512)
    assert status == "SAME_MODEL_DIFFERENT_CONFIGURATION"
    assert offer_explanation(status, (), [("storage_capacity_gb", "256", "512")]) == (
        "Different configuration — 256GB storage; target requires 512GB")


# 6. Recheck links on blank cells


LISTED = RecheckCandidate(40, LISTED_NOT_FETCHED, "CANDIDATE_NOT_FETCHED",
                          "Not fetched — collection budget reached",
                          "https://pcx.example.test/products/eb14-0001?variant=7",
                          "Example EB14-0001")


def with_rechecks(tmp_path):
    """A same-model near match (another model code, priced 100.00) linked to TGT-000001 at LF,
    and an unfetched relevant PC Express result."""
    specs = tuple((label, value.replace("EB14-0001", "EB14-0009")) for label, value in
                  MODELS["EB14-0001"])
    other = stored_offer("EB14-0009", specs=specs, price="100.00")
    result = build(tmp_path, [other], search_context={observation_id(other): ["TGT-000001"]},
                   discovery_rechecks={("TGT-000001", "PCX"): [LISTED]})
    return result, other


def test_blank_cells_show_relevant_recheck_links(tmp_path) -> None:
    result, other = with_rechecks(tmp_path)
    listed = LISTED
    cell = row(result, "TGT-000001")
    # The fetched near match is named with its exact URL, its reason, stock and time...
    assert cell["LF_Price"] == "" and cell["LF_Candidate_URL"] == other.offer_url
    assert cell["LF_Recheck_Count"] == "1"
    lf = next(r for r in result.rechecks if r["retailer_code"] == "LF")
    assert (lf["stage"], lf["url"], lf["stock"]) == (FETCHED_NOT_CONFIRMED, other.offer_url,
                                                     "IN_STOCK")
    assert lf["observed_at_local"] and lf["unconfirmed_price"] == "100.00"
    # ...and the unfetched relevant search result keeps its variant URL and reason.
    assert (cell["PCX_Candidate_URL"], cell["PCX_Recheck_Note"]) == (
        listed.url, "Not fetched — collection budget reached")
    pcx = next(r for r in result.rechecks if r["retailer_code"] == "PCX")
    assert (pcx["stock"], pcx["observed_at_local"], pcx["unconfirmed_price"]) == ("", "", "")
    # The Sheets comparison shows the link only beside a blank price, with the overflow count.
    sheet_row = {"LF_Price": "", "LF_Candidate_URL": "https://a.test/p?variant=1",
                 "LF_Recheck_Note": "Fetch failed — HTTP_403", "LF_Recheck_Count": "3"}
    link, note = _recheck_cells(None, sheet_row, "LF")
    assert link.value == "https://a.test/p?variant=1" and link.link == link.value
    assert note.value == "Fetch failed — HTTP_403 (+2 more in PILOT_PRICE_REVIEW)"
    priced = _recheck_cells(None, sheet_row | {"LF_Price": "999.00"}, "LF")
    assert all(not cell_.value for cell_ in priced)


# 7. Unconfirmed candidate prices never reach a price, a count or a minimum


def test_unconfirmed_candidate_prices_are_excluded_from_counts_and_minima(tmp_path) -> None:
    result, _ = with_rechecks(tmp_path)
    cell = row(result, "TGT-000001")
    assert next(r for r in result.rechecks if r["retailer_code"] == "LF")[
        "unconfirmed_price"] == "100.00"  # shown, labelled unconfirmed...
    # ...but never a comparable price, a retailer counted, or a minimum.
    assert {cell[f"{code}_Price"] for code in ("LF", "PCX", "PCC", "DBZ", "OCT")} == {""}
    assert (cell["Comparable_Retailer_Count"], cell["Lowest_Price"],
            cell["Lowest_In_Stock_Price"]) == ("0", "", "")
    assert not [s for s in result.selections if s["target_id"] == "TGT-000001" and s["price"]]
    assert result.counts["blank_cells_with_recheck_link"] >= 2


# 8. Accessories and unrelated results are never recheck links


def test_accessories_and_unrelated_results_are_not_listed() -> None:
    base = {"target_id": "TGT-000002", "retailer_id": "datablitz", "title": "x",
            "decision": "NOT_COLLECTED_BUDGET"}
    candidates = [
        base | {"category": "ACCESSORY", "product_url": "https://d.test/products/sleeve"},
        base | {"category": "UNRELATED", "product_url": "https://d.test/products/phone"},
        base | {"category": "DIFFERENT_MODEL_CODE", "product_url": "https://d.test/products/o"},
        base | {"category": "PRODUCT_NAME_MATCH", "product_url": "https://d.test/products/neo",
                "collection_url": "https://d.test/products/neo?variant=5"},
    ]
    found = discovery_rechecks(candidates, [], {})
    listed = found[("TGT-000002", "datablitz")]
    assert [c.url for c in listed] == ["https://d.test/products/neo?variant=5"]
    assert listed[0].explanation == "Not fetched — collection budget reached"
    # Accessories are recognised from the listing before any page is inspected.
    assert accessory_reason("Laptop Sleeve for MacBook Neo 13-inch", "")
    assert accessory_reason("Apple MacBook Neo 13 8GB 256GB A18 Pro", "Laptop") == ""


@pytest.mark.parametrize(
    ("title", "ram", "storage"),
    [
        ("Laptop 8GB+256GB", "8", "256"),
        ("Laptop | 16GB / 1TB", "16", "1000"),
        ("Laptop 16GB (8GB + 8GB)", M, M),  # two memory amounts: not RAM + storage
        ("Laptop 8GB+256GB/512GB", M, M),  # a list of options states neither
    ],
)
def test_the_joined_ram_and_storage_title_form(title, ram, storage) -> None:
    page = neo(title=title, rows=without("Memory", "Storage"))
    fields = primary(page)
    assert fields["ram_capacity_gb"][1 if ram != M else 0] == ram
    assert fields["storage_capacity_gb"][1 if storage != M else 0] == storage


def test_discovery_reads_the_joined_form_so_other_storage_listings_rank_apart() -> None:
    from test_catalog_identity import NEO as NEO_TARGET

    from laptop_tracker.product_discovery import clue_comparison, title_clues

    clues_256 = title_clues(NEO_TITLE)
    assert (clues_256["ram_capacity_gb"], clues_256["storage_capacity_gb"]) == ({"8"}, {"256"})
    assert clue_comparison(clues_256, NEO_TARGET)["storage_capacity_gb"].startswith("DIFFERS")
    clues_512 = title_clues(NEO_TITLE.replace("8GB+256GB", "8GB+512GB"))
    assert clue_comparison(clues_512, NEO_TARGET)["storage_capacity_gb"] == "AGREES"
    # Two drives or a RAM breakdown keep the joined clue (not one stated capacity).
    assert title_clues("Laptop 512GB + 1TB SSD")["storage_capacity_gb"] == {"512GB + 1TB"}
    assert title_clues("Laptop 8GB + 8GB RAM")["ram_capacity_gb"] == {"8GB + 8GB"}


def test_a_held_product_name_listing_is_a_relevant_candidate_for_coverage() -> None:
    from laptop_tracker.pilot_coverage import CANDIDATE_NOT_FETCHED, coverage

    candidates = [{"target_id": "TGT-000002", "retailer_id": "pcc",
                   "category": "PRODUCT_NAME_MATCH", "decision": "REVIEW_NO_MATCHING_VARIANT",
                   "product_url": "https://pcc.example.test/products/apple-macbook-neo"}]
    searched = [{"target_id": "TGT-000002", "retailer_id": "pcc", "outcome": "RESULTS",
                 "tier": "1_PRODUCT_NAME"}]
    pairs, _ = coverage(["TGT-000002"], ["pcc"], [], candidates=candidates,
                        search_requests=searched)
    assert pairs[0]["coverage_status"] == CANDIDATE_NOT_FETCHED
