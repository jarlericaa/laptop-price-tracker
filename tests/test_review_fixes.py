"""Regressions for the independent review of 2026-10-19 (data/output/independent_review_*).

1. Title capacities keep their component structure: repeated components are summed, never
   collapsed; external, extra and maximum amounts are never installed; an unclear structure
   goes to review with no target.
2. Explicit secondary claims in an accepted title (GPU, panel, resolution) reach the matcher's
   conflict check; missing ones stay unknown; equivalent GPU spellings agree.
3. URL path comparison normalizes escape case only: an escaped "/" is not a separator.
4. Catalog reconciliation compares complete configuration identities, not model codes alone.
5. Family-name leads are ranked by the target's own identifiers and explain unitless numbers.
Plus an integrated Neo ranking / candidate-limit replay over saved DataBlitz responses.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from scraping_fakes import FakeClock, no_network  # noqa: F401
from test_collection import write_dicts
from test_extraction_layouts import BOUND, pcc_offer, primary, supporting
from test_extraction_layouts import offer as layout_offer
from test_price_tracking import ledger, list_rows, target
from test_product_discovery import Routes, discover, rows
from test_target_master import target_values

from laptop_tracker.adapters import pcc, pcx
from laptop_tracker.catalog_reconciliation import (
    DUPLICATE_OF_EXISTING_TARGET,
    SAME_CORE_SECONDARY_CONFLICT,
    SUPPORTED_CORRECTION,
    reconcile,
)
from laptop_tracker.conventions import canonical_gpu_models
from laptop_tracker.extraction import extract_offer_attributes
from laptop_tracker.matching import ExactMatcher, MatchStatus
from laptop_tracker.price_tracking import offer_disagreements
from laptop_tracker.product_names import default_registry
from laptop_tracker.recheck import discovery_rechecks, unitless_sizes, url_key
from laptop_tracker.scraping import ParseError
from laptop_tracker.url_paths import normalized_path
from laptop_tracker.verified_top import CONFIGURATION_COLUMNS, LEDGER_COLUMNS

pytestmark = pytest.mark.usefixtures("no_network")
E, M, A = "EXTRACTED", "MISSING", "AMBIGUOUS"
TARGET = dict(target_id="TGT-000901", status="ACTIVE", model="AB14-1234", screen_size_in="14",
              cpu_series="Core i5", ram_capacity_gb="16", storage_capacity_gb="512")
PREFIX = "Acer AB14-1234 Laptop 14 inch Core i5 "


def title_only(text: str):
    """An Octagon page whose only evidence is its title (the review's reproduction shape)."""
    return layout_offer("oct", (), title=PREFIX + text)


def match(page, **changes):
    return ExactMatcher([TARGET | changes]).match_observation(page)


# 1. Title capacities


@pytest.mark.parametrize(
    ("text", "ram", "storage"),
    [
        # The review's reproductions: never the 16GB/512GB target.
        ("16GB RAM 512GB SSD + 512GB SSD", (E, "16"), (E, "1024")),  # two drives, summed
        ("16GB RAM + 16GB RAM 512GB SSD", (E, "32"), (E, "512")),  # two modules, summed
        ("8GB + 256GB external SSD", (M, ""), (M, "")),  # external drive is not installed
        # Established installed-component and layout rules.
        ("16GB (2x8GB) DDR5 RAM 512GB SSD", (E, "16"), (E, "512")),  # consistent breakdown
        ("16GB (2x16GB) RAM 512GB SSD", (A, "16GB (2x16GB) RAM"), (E, "512")),  # inconsistent
        ("8GB RAM (upgradable up to 32GB) 512GB SSD", (E, "8"), (E, "512")),  # maximum apart
        ("16GB LPDDR5X 512GB SSD", (E, "16"), (E, "512")),  # memory type is not a count
        # Ambiguous alternatives and repeated statements go to review.
        ("16GB RAM 512GB SSD / 1TB SSD", (E, "16"), (A, "512GB SSD; 1TB SSD")),
        ("16GB RAM | 512GB SSD | 16GB RAM", (A, "16GB RAM; 16GB RAM"), (E, "512")),
        ("8GB/16GB RAM 512GB SSD", (A, "16GB RAM"), (E, "512")),
        # The clear joined RAM+storage form keeps working.
        ("8GB+256GB", (E, "8"), (E, "256")),
        ("16GB/ 1TB SSD", (E, "16"), (E, "1000")),
    ],
)
def test_title_capacities_keep_their_structure(text, ram, storage) -> None:
    fields = primary(title_only(text))
    assert fields.get("ram_capacity_gb", (M, "")) == ram
    assert fields.get("storage_capacity_gb", (M, "")) == storage


@pytest.mark.parametrize(
    ("text", "changes"),
    [
        ("16GB RAM 512GB SSD + 512GB SSD", {}),
        ("16GB RAM + 16GB RAM 512GB SSD", {}),
        ("8GB + 256GB external SSD", {"ram_capacity_gb": "8", "storage_capacity_gb": "256"}),
        ("16GB RAM 512GB SSD / 1TB SSD", {}),
        ("16GB RAM | 512GB SSD | 16GB RAM", {}),
    ],
)
def test_reproduced_capacity_failures_never_assign_the_target(text, changes) -> None:
    result = match(title_only(text), **changes)
    assert result.status is not MatchStatus.EXACT and result.target_id is None


def test_summed_components_and_excluded_items_stay_visible() -> None:
    page = title_only("16GB RAM 512GB SSD + 512GB SSD")
    assert supporting(page)["storage_components"] == ["512GB SSD + 512GB SSD"]
    # The summed total matches only a configuration with that total.
    assert match(page, storage_capacity_gb="1024").status is MatchStatus.EXACT
    external = title_only("16GB RAM 512GB SSD + FREE 1TB external HDD")
    assert primary(external)["storage_capacity_gb"] == (E, "512")
    assert "TITLE_NOT_INSTALLED_STORAGE_CAPACITY_GB" in extract_offer_attributes(external).notes
    assert supporting(external)["storage_capacity_gb"] == ["1TB external HDD"]


# 2. Secondary claims in accepted title evidence


def test_explicit_title_gpu_and_panel_conflicts_block_the_exact_match() -> None:
    gpu = match(title_only("16GB RAM 512GB SSD RTX 3050"), gpu_model="RTX 4050")
    assert gpu.status is MatchStatus.REVIEW_REQUIRED and gpu.target_id is None
    assert "SECONDARY_CONFLICT_GPU_MODEL" in gpu.reason_codes
    panel = match(title_only("16GB RAM 512GB SSD IPS"), display_type="OLED")
    assert panel.status is MatchStatus.REVIEW_REQUIRED and panel.target_id is None
    assert "SECONDARY_CONFLICT_DISPLAY_TYPE" in panel.reason_codes


def test_equivalent_gpu_spellings_agree_and_missing_claims_stay_unknown() -> None:
    assert canonical_gpu_models("GeForce RTX™ 4050 Laptop GPU") == {"RTX 4050"}
    assert canonical_gpu_models("RTX4050") == {"RTX 4050"}
    assert canonical_gpu_models("RTX 4060 Ti") == {"RTX 4060 Ti"}
    assert canonical_gpu_models("Intel UHD Graphics") == set()
    page = title_only("16GB RAM 512GB SSD GeForce RTX4050")
    assert match(page, gpu_model="RTX 4050").status is MatchStatus.EXACT
    # The title states no GPU: the target's GPU is not a requirement.
    assert match(title_only("16GB RAM 512GB SSD"), gpu_model="RTX 4050",
                 display_type="OLED").status is MatchStatus.EXACT
    assert match(page).status is MatchStatus.EXACT  # target records no GPU: nothing to compare


def test_a_title_contradicting_the_page_rows_is_kept_and_routed() -> None:
    page = layout_offer("pcx", (("Display", '14" WUXGA OLED'),),
                        title=PREFIX + "16GB RAM 512GB SSD IPS")
    fields = primary(page)
    assert fields["display_type"] == (A, "OLED | title: IPS")
    assert "TITLE_SPEC_CONFLICT_DISPLAY_TYPE" in extract_offer_attributes(page).notes
    assert match(page).target_id is None
    # IPS-Level wording in the rows and IPS in the title is the same panel technology.
    same = layout_offer("pcx", (("Display", '14" IPS-Level'),), title=PREFIX + "16GB 512GB IPS")
    assert primary(same)["display_type"] == (E, "IPS-Level")


def test_a_shared_parent_title_supplies_no_secondary_claim() -> None:
    page = pcc_offer(BOUND)  # parent title states "RTX"-free text; selected variant governs
    shared = layout_offer("pcc", (), title="Example Thin 15 RTX 3050 8GB 512GB")
    assert "gpu_model" not in primary(page)
    assert "gpu_model" in primary(shared)  # not shared: the title applies


def test_resolution_names_precise_versus_loose() -> None:
    wuxga = layout_offer("pcx", (("Display", '15.3" Full-HD WUXGA'),))
    assert primary(wuxga)["resolution"] == (E, "1920x1200")
    # PC Express writes "Full-HD" loosely; it is supporting evidence only, never a value.
    loose = layout_offer("pcx", (("Display", '14" Full-HD IPS'),))
    assert "resolution" not in primary(loose)
    assert supporting(loose)["resolution_name"] == ["Full HD (nominal 1920x1080)"]


@dataclass
class _Stored:
    page: object

    def observation(self):
        return self.page


@dataclass
class _View:
    stored: _Stored


def test_offers_disagreeing_on_display_evidence_are_flagged_not_resolved() -> None:
    # The saved AL14-45P-R10U offers: DataBlitz/LF state WUXGA 1920 x 1200, PC Express Full-HD.
    dbz = layout_offer("datablitz", (("Display", '14.0" IPS, WUXGA 1920 x 1200'),))
    lf = layout_offer("laptop_factory", (("Display Size", "14″ IPS, WUXGA 1920 x 1200"),))
    pcx_page = layout_offer("pcx", (("Display", '14" Full-HD IPS'),))
    found = offer_disagreements({"DBZ": _View(_Stored(dbz)), "LF": _View(_Stored(lf)),
                                 "PCX": _View(_Stored(pcx_page))})
    assert found == {"resolution": "DBZ 1920x1200; LF 1920x1200; "
                                   "PCX Full HD (nominal 1920x1080)"}
    assert offer_disagreements({"DBZ": _View(_Stored(dbz)), "LF": _View(_Stored(lf))}) == {}


# 3. URL paths


@pytest.mark.parametrize(
    ("left", "right", "same"),
    [
        ("/products/x%e2%84%a2", "/products/x%E2%84%A2", True),  # escape case only
        ("/products/x™", "/products/x%E2%84%A2", True),  # literal vs UTF-8 escape
        ("/products/a%2db", "/products/a-b", True),  # escaped unreserved character
        ("/products/a%2Fb", "/products/a/b", False),  # encoded slash is not a separator
        ("/products/a%25", "/products/a%", False),  # encoded percent
        ("/products/a%252F", "/products/a%2F", False),  # double encoding
        ("/products/ABC", "/products/abc", False),  # path case is meaningful
    ],
)
def test_path_normalization_changes_only_spelling(left, right, same) -> None:
    host = "https://pcx.com.ph"
    assert (normalized_path(host + left) == normalized_path(host + right)) is same


def test_adapters_keep_distinct_paths_and_variants_apart() -> None:
    nodes = [{"@type": "Product", "url": "https://pcx.com.ph/products/a%2Fb"},
             {"@type": "Product", "url": "https://pcx.com.ph/products/a/b"}]
    with pytest.raises(ParseError):
        pcx._products([json.dumps(node) for node in nodes])
    same = [{"@type": "Product", "url": "https://pcx.com.ph/products/x%e2%84%a2"},
            {"@type": "Product", "url": "https://pcx.com.ph/products/x%E2%84%A2"}]
    assert len(pcx._products([json.dumps(node) for node in same])) == 2  # one product, 2 blocks
    assert pcc._path("https://p.test/products/a%2Fb") != pcc._path("https://p.test/products/a/b")
    # Variant identity lives in the query and is never folded into the path comparison.
    assert url_key("https://p.test/products/x?variant=1") != url_key(
        "https://p.test/products/x?variant=2")
    assert pcc._path("https://p.test/products/x?variant=1") == "/products/x"


# 4. Catalog reconciliation


def catalog_row(target_id: str, model: str, *, ram="8", gpu="", mmc=""):
    return {"target_id": target_id, "brand": "Acer", "model": model, "series": "",
            "manufacturer_model_code": mmc, "gpu_model": gpu, "notes": "", "screen_size_in": "14",
            "cpu_series": "Ryzen 3", "ram_capacity_gb": ram, "storage_capacity_gb": "512"}


def source(target_id: str, model_id: str, text: str) -> dict[str, str]:
    return {"target_id": target_id, "model_id": model_id, "shop_id": "1", "item_id": model_id,
            "item_name": text, "shopee_model_name": "", "contribution_status": "CONTRIBUTING"}


SOURCE_8 = "Acer AL14-45P-R10U Laptop 14 inch Ryzen 3 8GB RAM 512GB SSD"


def reconciled(catalog, contributions, ids):
    selected = [{"target_id": i, "rank": str(n), "model": "", "series": ""}
                for n, i in enumerate(ids, start=1)]
    result = reconcile(catalog, selected, contributions, default_registry())
    return {row["target_id"]: row for row in result.rows}, result.corrections


def test_one_code_with_another_configuration_is_not_a_duplicate() -> None:
    # The review's reproduction: an 8GB family target and a 16GB coded target share the code.
    catalog = [catalog_row("TGT-000901", "Aspire Lite"),
               catalog_row("TGT-000902", "AL14-45P-R10U", ram="16")]
    rows_, corrections = reconciled(catalog, [source("TGT-000901", "11", SOURCE_8)],
                                    ["TGT-000901"])
    assert rows_["TGT-000901"]["status"] == SUPPORTED_CORRECTION
    assert "TGT-000902, a different configuration (ram_capacity_gb 8 vs 16)" in (
        rows_["TGT-000901"]["flags"])
    assert set(corrections) == {"TGT-000901"}


def test_the_same_configuration_is_a_duplicate_unless_a_secondary_field_differs() -> None:
    catalog = [catalog_row("TGT-000901", "Aspire Lite"),
               catalog_row("TGT-000902", "AL14-45P-R10U")]
    rows_, corrections = reconciled(catalog, [source("TGT-000901", "11", SOURCE_8)],
                                    ["TGT-000901"])
    assert rows_["TGT-000901"]["status"] == DUPLICATE_OF_EXISTING_TARGET
    assert "nothing is merged automatically" in rows_["TGT-000901"]["action"]
    assert corrections == {}
    catalog[0]["gpu_model"], catalog[1]["gpu_model"] = "RTX 3050", "RTX4050"
    rows_, _ = reconciled(catalog, [source("TGT-000901", "11", SOURCE_8)], ["TGT-000901"])
    assert rows_["TGT-000901"]["status"] == SAME_CORE_SECONDARY_CONFLICT
    assert "gpu_model: RTX 3050 vs RTX4050" in rows_["TGT-000901"]["conflicts"]


def test_two_proposals_for_one_code_are_grouped_by_complete_identity() -> None:
    catalog = [catalog_row("TGT-000901", "Aspire Lite"),
               catalog_row("TGT-000903", "Aspire Lite", ram="16")]
    contributions = [source("TGT-000901", "11", SOURCE_8),
                     source("TGT-000903", "12", SOURCE_8.replace("8GB RAM", "16GB RAM"))]
    rows_, corrections = reconciled(catalog, contributions, ["TGT-000901", "TGT-000903"])
    assert {rows_[i]["status"] for i in rows_} == {SUPPORTED_CORRECTION}
    assert set(corrections) == {"TGT-000901", "TGT-000903"}
    same = [catalog_row("TGT-000901", "Aspire Lite"), catalog_row("TGT-000903", "Aspire Lite")]
    rows_, corrections = reconciled(same, [source("TGT-000901", "11", SOURCE_8),
                                           source("TGT-000903", "12", SOURCE_8)],
                                    ["TGT-000901", "TGT-000903"])
    assert {rows_[i]["status"] for i in rows_} == {DUPLICATE_OF_EXISTING_TARGET}
    assert corrections == {}


# 5. Family-name leads


VIVOBOOK_14 = {"screen_size_in": "14", "manufacturer_model_code": "S3407VA-LY135WSM",
               "manufacturer_part_number": ""}


def lead(title: str, url: str, **clues) -> dict[str, str]:
    return {"target_id": "TGT-000041", "retailer_id": "oct",
            "category": "FAMILY_NAME_LEAD_CATALOG_UNRESOLVED",
            "decision": "REVIEW_CATALOG_IDENTITY",
            "product_url": url, "title": title,
            **{f"clue_{name}": clues.get(name, "NOT_STATED")
               for name in ("screen_size_in", "cpu_series", "ram_capacity_gb",
                            "storage_capacity_gb")}}


def test_family_leads_explain_unitless_numbers_and_prefer_the_targets_own_code() -> None:
    sixteen = lead("ASUS VIVOBOOK 16 V3607VH-RP521W/ CORE 5-210H/ 16GB/ 512GB SSD/ 16 WUXGA "
                   "144HZ/ WIN 11", "https://o.test/products/v16", cpu_series="AGREES",
                   storage_capacity_gb="AGREES")
    own = lead("ASUS Vivobook S14 (S3407VA-LY135WSM) Intel Core 5 Laptop",
               "https://o.test/products/s14?variant=3", cpu_series="AGREES")
    plain = [lead(f"ASUS Vivobook 14 X1404VA-EB{n} Laptop", f"https://o.test/products/{n}")
             for n in range(3)]
    found = discovery_rechecks([sixteen, *plain, own], [], {},
                               {"TGT-000041": VIVOBOOK_14})[("TGT-000041", "oct")]
    assert len(found) == 3  # at most three leads
    assert found[0].url == "https://o.test/products/s14?variant=3"  # variant URL kept
    assert "names this target's recorded code S3407VA-LY135WSM" in found[0].explanation
    assert "not a confirmed match" in found[0].explanation
    assert sixteen["product_url"] not in [c.url for c in found]  # ranked below the others
    alone = discovery_rechecks([sixteen], [], {}, {"TGT-000041": VIVOBOOK_14})
    note = alone[("TGT-000041", "oct")][0].explanation
    assert "title has '16' without a unit (target screen 14\"; not a verified screen size)" in (
        note)
    assert "agrees on CPU, storage" in note
    # Windows 11 is not a screen size; a bare 14 equal to the target is not a warning.
    assert unitless_sizes("Laptop 14 WIN 11 Windows 11") == ["14"]


def test_weekly_reports_rank_family_leads_with_the_catalog_rows(tmp_path) -> None:
    # Through track_prices.read_discovery, with a collection lineage present (a loop variable
    # once shadowed the catalog rows there).
    from datetime import date

    from test_acceptance_fixes import track_prices

    from laptop_tracker.price_tracking import ReportWeek

    folder = tmp_path / "discovery"
    (folder / "collection").mkdir(parents=True)
    (folder / "discovery_summary.json").write_text(
        json.dumps({"started_at_utc": "2026-10-19T08:00:00Z"}))
    write_dicts(folder / "search_requests.csv", ("target_id", "retailer_id", "tier", "outcome"),
                [{"target_id": "TGT-000041", "retailer_id": "oct", "tier": "1_PRODUCT_NAME",
                  "outcome": "RESULTS"}])
    leads = [lead("ASUS VIVOBOOK 16 V3607VH-RP521W/ 16 WUXGA", "https://o.test/products/v16"),
             lead("ASUS Vivobook S14 (S3407VA-LY135WSM)", "https://o.test/products/s14")]
    write_dicts(folder / "candidates.csv", list(leads[0]), leads)
    write_dicts(folder / "collection" / "match_results.csv",
                ("observation_id", "expected_target_id"),
                [{"observation_id": "o-1", "expected_target_id": "TGT-000041"}])
    _, _, rechecks = track_prices.read_discovery(
        [folder], ReportWeek(date(2026, 10, 19)), {"TGT-000041": VIVOBOOK_14})
    found = rechecks[("TGT-000041", "OCT")]
    assert [c.url for c in found] == ["https://o.test/products/s14",
                                      "https://o.test/products/v16"]
    assert "'16' without a unit" in found[1].explanation


# Integrated Neo ranking and candidate limit over saved DataBlitz responses (2026-10-19)


NEO_FIXTURE = (Path(__file__).resolve().parent / "scraping_fixtures" / "discovery_replay"
               / "dbz_macbook_neo_2026-10-19")


def neo_inputs(tmp_path: Path) -> list[str]:
    neo = dict(brand="Apple", series="", manufacturer_model_code="", resolution="",
               display_type="")
    targets = [target("TGT-000001", "MacBook Neo", "13", "Apple A18 Pro", "8", "512", **neo),
               target("TGT-000002", "MacBook Neo", "13", "Apple A18 Pro", "8", "256", **neo)]
    (tmp_path / "tm.csv").write_text("", encoding="utf-8")
    import csv

    with (tmp_path / "tm.csv").open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows(target_values(*targets))
    selected = write_dicts(tmp_path / "top.csv", CONFIGURATION_COLUMNS,
                           list_rows(("TGT-000001", "TGT-000002"), targets))
    ledger_path = write_dicts(tmp_path / "ledger.csv", LEDGER_COLUMNS, ledger())
    return ["--target-master", str(tmp_path / "tm.csv"), "--selected-list", str(selected),
            "--contributions", str(ledger_path), "--store", str(tmp_path / "store.sqlite3")]


def test_saved_neo_results_spend_the_candidate_limit_on_the_requested_storage(tmp_path) -> None:
    # The saved search lists a 256GB colour first; the 512GB target must not spend a slot on it.
    transport = Routes({})
    out = tmp_path / "discovery"
    code = discover.main(
        ["--live", "--replay-only", "--search-cache", str(NEO_FIXTURE), "--targets", "TGT-000001",
         "--retailers", "datablitz", "--no-supplied-urls", "--max-discovery-rounds", "1",
         "--max-candidates-per-target-retailer", "2", "--max-total-requests", "20",
         *neo_inputs(tmp_path),
         "--output-dir", str(out)],
        transport=transport, clock=FakeClock(),
    )
    assert code == 0 and transport.calls == []  # saved responses only
    neo = [r for r in rows(out / "candidates.csv") if r["category"] == "PRODUCT_NAME_MATCH"]
    ranked = sorted(neo, key=lambda r: int(r["candidate_rank"]))
    first_256 = next(i for i, r in enumerate(ranked) if "8GB+256GB" in r["title"])
    assert first_256 >= 2 and all("8GB+512GB" in r["title"] for r in ranked[:first_256])
    for row in ranked:
        if "8GB+256GB" in row["title"]:
            assert row["decision"] == "NOT_SELECTED_LIMIT"
            assert row["configuration_difference"] == (
                "storage_capacity_gb: title 256 vs target 512")
    chosen = [r for r in ranked if r["decision"] != "NOT_SELECTED_LIMIT"]
    assert chosen and all("8GB+512GB" in r["title"] for r in chosen)


# Publication transitions: a republished tab never keeps the previous run's price, link or note


def test_republication_clears_stale_prices_links_and_notes(tmp_path) -> None:
    from test_sheet_publication import COMPARISON_TAB, build_reports, row_for
    from test_sheet_publication import run as publish

    world = build_reports(tmp_path)
    code, fake, _ = publish(world)
    assert code == 0
    header = fake.values(COMPARISON_TAB)[0]
    row_index = next(i for i, row in enumerate(fake.values(COMPARISON_TAB))
                     if "TGT-000005" in row)
    stale = {
        "Laptop Factory price (PHP)": {"numberValue": 999.0},
        "Laptop Factory product link": {"stringValue": "https://old.test/p?variant=1"},
        "Laptop Factory recheck link (unconfirmed)": {"stringValue": "https://old.test/r"},
        "Laptop Factory recheck note": {"stringValue": "Not fetched — collection budget reached"},
    }
    for name, value in stale.items():
        cell = fake.sheets[COMPARISON_TAB]["grid"].setdefault((row_index, header.index(name)), {})
        cell["userEnteredValue"] = value
        cell["hyperlink"] = "https://old.test/p?variant=1"
        cell["userEnteredFormat"] = {"textFormat": {"link": {"uri": "https://old.test/p"}}}
    # This week TGT-000005 has no offer at Laptop Factory: every stale cell must be cleared.
    from datetime import UTC, datetime

    code, fake, _ = publish(world, fake=fake, now=datetime(2026, 10, 12, 3, tzinfo=UTC))
    assert code == 0
    row = row_for(fake, COMPARISON_TAB, "TGT-000005")
    assert all(row[name] == "" for name in stale)
    for name in stale:
        cell = fake.cell(COMPARISON_TAB, row_index, header.index(name))
        assert not cell.get("hyperlink")
        assert not cell.get("userEnteredFormat", {}).get("textFormat", {}).get("link")
    # Priced cells are written again with this week's values, not kept from the last run.
    assert row_for(fake, COMPARISON_TAB, "TGT-000002")["Laptop Factory price (PHP)"] == "1500.0"
