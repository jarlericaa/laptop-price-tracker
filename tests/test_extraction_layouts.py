"""Synthetic regression tests for the 2026-10-13 retailer layouts and boundary cases.

Every page and value here is synthetic. The layouts follow the reviewed DataBlitz, PC Express,
PC Central, and Octagon pages; no real page content is stored in the repository.
"""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from scraping_fakes import FakeClock, FakeTransport, no_network  # noqa: F401

from laptop_tracker.adapters.pcc import HOST as PCC_HOST
from laptop_tracker.adapters.pcc import PCCAdapter
from laptop_tracker.extraction import ExtractionStatus, extract_offer_attributes
from laptop_tracker.match_run import read_observations_csv
from laptop_tracker.matching import ExactMatcher, MatchStatus
from laptop_tracker.observation_store import ObservationStore, canonical_record, observation_id
from laptop_tracker.offers import (
    ENUMERATED_VARIANT,
    IDENTIFIER,
    OBSERVATION_COLUMNS,
    OFFER_DETAIL_COLUMNS,
    PRODUCT_DESCRIPTION,
    SELECTED_VARIANT,
    Availability,
    OfferDetails,
    OfferObservation,
    PriceType,
    RetrievalMetadata,
)
from laptop_tracker.scraping import FetchConfig, Fetcher, ScrapeRequest, TransportResponse, scrape

pytestmark = pytest.mark.usefixtures("no_network")

E, M, A, U, N, R = (
    "EXTRACTED",
    "MISSING",
    "AMBIGUOUS",
    "UNPARSEABLE",
    "NOT_EXTRACTED",
    "UNRESOLVED",
)


def offer(
    retailer_id: str,
    specs: tuple[tuple[str, str], ...],
    *,
    title: str = "Synthetic laptop",
    configuration: str | None = None,
    scoped: tuple[tuple[str, str, str], ...] = (),
    url: str = "https://shop.example.test/products/synthetic",
    bundle: bool | None = None,
    bundle_evidence: tuple[str, ...] = ("Synthetic backpack bundle",),
    price_type: PriceType | None = None,
) -> OfferObservation:
    joined = " | ".join(f"{label}: {value}" if value else label for label, value in specs)
    return OfferObservation(
        retailer_id=retailer_id,
        offer_url=url,
        retrieved_at=datetime(2026, 10, 13, tzinfo=UTC),
        raw_title=title,
        raw_configuration_text=joined if configuration is None else configuration,
        price=Decimal("50000"),
        currency="PHP",
        raw_price_text="PHP 50,000",
        availability=Availability.IN_STOCK,
        availability_evidence="Synthetic fixture",
        offer_index=0,
        source=RetrievalMetadata(
            requested_url=url,
            final_url=url,
            http_status=200,
            attempts=1,
            content_sha256="1" * 64,
            content_type="text/html",
            adapter_version="fixture-2",
        ),
        details=OfferDetails(
            raw_specifications=specs,
            scoped_evidence=scoped,
            bundle_flag=bundle,
            bundle_evidence=bundle_evidence if bundle else (),
            price_type=price_type,
        ),
    )


def primary(observation: OfferObservation) -> dict[str, tuple[str, str]]:
    return {
        item.field: (item.status.value, item.value)
        for item in extract_offer_attributes(observation).evidence
        if item.status is not ExtractionStatus.SUPPORTING
    }


def supporting(observation: OfferObservation) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for item in extract_offer_attributes(observation).evidence:
        if item.status is ExtractionStatus.SUPPORTING:
            found.setdefault(item.field, []).append(item.value)
    return found


def notes(observation: OfferObservation) -> tuple[str, ...]:
    return extract_offer_attributes(observation).notes


TARGET = {
    "target_id": "TGT-000001",
    "status": "ACTIVE",
    "model": "A14-11M-X3BP",
    "screen_size_in": "14",
    "cpu_series": "Snapdragon X",
    "ram_capacity_gb": "16",
    "storage_capacity_gb": "512",
}

DBZ_ROWS = (
    ("Marketing Name", "Aspire"),
    ("Color", "Steel Gray"),
    ("Processor", "Qualcomm Snapdragon X, X1-26-100 processor"),
    ("Memory", "16 GB of onboard LPDDR5X system memory"),
    ("Memory Frequency", "6400Mhz LPDDR5X"),
    ("Storage", "512 GB SSD"),
)


# DataBlitz: Model, Size, and Display Panel/ Resolution rows


def test_datablitz_model_row_and_agreeing_inch_sizes_extract() -> None:
    page = offer(
        "datablitz",
        (
            ("Size", '14"'),
            ("Model", "A14-11M-X3BP"),
            ("Display Panel/ Resolution", '14.0" WUXGA 1920 x 1200 IPS'),
            *DBZ_ROWS,
        ),
    )
    assert primary(page) == {
        "model": (E, "A14-11M-X3BP"),
        "screen_size_in": (E, "14"),
        "cpu_series": (E, "Snapdragon X"),
        "ram_capacity_gb": (E, "16"),
        "storage_capacity_gb": (E, "512"),
        "resolution": (E, "1920x1200"),
        "display_type": (E, "IPS"),
        "cpu_model": (E, "X1-26-100"),
    }
    result = ExactMatcher([TARGET]).match_observation(page)
    assert (result.status, result.target_id) == (MatchStatus.EXACT, "TGT-000001")


def test_datablitz_conflicting_inch_sizes_are_ambiguous() -> None:
    page = offer(
        "datablitz",
        (("Size", '14"'), ("Display Panel/ Resolution", '15.6" FHD IPS'), *DBZ_ROWS),
    )
    assert primary(page)["screen_size_in"] == (A, "14 | 15.6")


def test_unexplained_suffix_is_preserved_and_never_matched() -> None:
    page = offer("datablitz", (("Size", '14"'), ("Model", "A14-11M-X3BP OPI"), *DBZ_ROWS))
    assert primary(page)["model"] == (R, "A14-11M-X3BP OPI")
    result = ExactMatcher([TARGET]).match_observation(page)
    assert result.status is MatchStatus.REVIEW_REQUIRED
    assert result.target_id is None
    assert "UNRESOLVED_MODEL" in result.reason_codes


@pytest.mark.parametrize("suffix", ["OPI", "XYZ", "WSM2"])
def test_no_suffix_is_special_cased(suffix: str) -> None:
    page = offer("datablitz", (("Model", f"A14-11M-X3BP {suffix}"),))
    assert primary(page)["model"] == (R, f"A14-11M-X3BP {suffix}")


def test_two_codes_in_a_model_row_are_ambiguous() -> None:
    page = offer("datablitz", (("Model", "A14-11M-X3BP / A14-11M-X7BQ"),))
    assert primary(page)["model"][0] == A


def test_explicit_product_prose_supplies_units_and_preserves_bundle() -> None:
    page = offer(
        "datablitz",
        (("Model", "SFG14-75-55MB"), ("Size", "14.0"), *DBZ_ROWS),
        scoped=(
            (PRODUCT_DESCRIPTION, "description paragraph", "A 14-inch CineCrystal display."),
            (PRODUCT_DESCRIPTION, "JSON-LD Product.description", "Thin 14 inch laptop."),
        ),
        bundle=True,
    )
    fields = primary(page)
    assert fields["model"] == (E, "SFG14-75-55MB")
    assert fields["screen_size_in"] == (E, "14")
    assert supporting(page)["screen_size_in"] == ["14", "14"]
    assert "display_type" not in fields  # CineCrystal is not OLED or IPS.
    result = ExactMatcher([TARGET | {"model": "SFG14-75-55MB"}]).match_observation(page)
    assert result.status is MatchStatus.EXACT
    assert "SUPPORTING_EVIDENCE_SCREEN_SIZE_IN" not in result.reason_codes
    assert page.details.bundle_flag is True


def test_prose_without_a_unit_or_outside_laptop_sizes_is_ignored() -> None:
    page = offer(
        "datablitz",
        (("Size", "14.0"),),
        scoped=(
            (PRODUCT_DESCRIPTION, "description paragraph", "Up to 14 hours; 65 inch TV mode."),
        ),
    )
    assert "screen_size_in" not in supporting(page)


def test_part_number_row_is_supporting_and_never_a_verified_mpn() -> None:
    page = offer("datablitz", (("Part Number", "NX.EXAMPLE.001"), *DBZ_ROWS))
    assert supporting(page)["part_number_candidate"] == ["NX.EXAMPLE.001"]
    assert "manufacturer_part_number" not in primary(page)


def test_color_row_differing_from_title_is_recorded() -> None:
    page = offer("datablitz", DBZ_ROWS, title="Example Aspire 14 Laptop (Steam Blue)")
    assert "COLOR_TITLE_DIFFERS" in notes(page)
    assert supporting(page)["color"] == ["specification: Steel Gray; title: Steam Blue"]


# PC Express: identifiers and unchanged core fields


PCX_ROWS = (
    ("Processor", "Intel Core 5 120U"),
    ("Display", '16" WUXGA IPS'),
    ("Memory", "16GB DDR5"),
    ("Storage", "512GB M.2 NVMe SSD"),
)


def test_pc_express_identifier_provenance_and_four_core_fields() -> None:
    scoped = (
        (IDENTIFIER, "JSON-LD Product block 1 sku", "LT-EXAMPLE-000001"),
        (IDENTIFIER, "JSON-LD Product block 1 mpn", "LT-EXAMPLE-000001"),
        (IDENTIFIER, "JSON-LD Product block 2 mpn", "(blank)"),
        (IDENTIFIER, "product form variant id", "49000000000001"),
    )
    page = offer("pcx", PCX_ROWS, scoped=scoped,
                 title="Example Vivobook S16 S3607VA-RP181WSM 90NB0000-M00000 Laptop")
    fields = primary(page)
    assert {name: fields[name] for name in TARGET if name not in {"target_id", "status"}} == {
        # One full title code, corroborated by four labeled rows (balanced policy, 2026-10-15).
        "model": (E, "S3607VA-RP181WSM"),
        "screen_size_in": (E, "16"),
        "cpu_series": (E, "Core 5"),
        "ram_capacity_gb": (E, "16"),
        "storage_capacity_gb": (E, "512"),
    }
    # The identifier evidence changes no core field; an MPN repeating the SKU is a warning.
    assert fields == primary(offer("pcx", PCX_ROWS, title=page.raw_title))
    assert "MPN_REPEATS_RETAILER_SKU" in notes(page)
    assert "TITLE_MODEL_CORROBORATED" in notes(page)
    assert supporting(page)["identifier"] == [value for _, _, value in scoped]
    # The ASUS part number beside the code is recorded as such, never as a second model code.
    assert supporting(page)["part_number_candidate"] == ["90NB0000-M00000"]
    assert "manufacturer_part_number" not in primary(page)


def test_core_5_is_never_core_i5() -> None:
    assert primary(offer("pcx", PCX_ROWS))["cpu_series"] == (E, "Core 5")
    labeled = offer(
        "laptop_factory",
        (
            ("Model Name", "Example S3607VA-RP181WSM"),
            ("Processor", "Intel Core 5 120U"),
            ("Display Size", '16"'),
            ("RAM", "16GB"),
            ("Storage", "512GB SSD"),
        ),
    )
    target = TARGET | {"model": "S3607VA-RP181WSM", "screen_size_in": "16"}
    assert ExactMatcher([target | {"cpu_series": "Core 5"}]).match_observation(
        labeled
    ).status is MatchStatus.EXACT
    result = ExactMatcher([target | {"cpu_series": "Core i5"}]).match_observation(labeled)
    assert result.status is not MatchStatus.EXACT


# PC Central: paragraph headings and the selected variant


PCC_DESCRIPTION = (
    ("Example Thin 15 B13UC-0000PH", ""),
    ("CPU", ""),
    ("Intel Core i5-13420H Processor", ""),
    ("Display", ""),
    ('15.6" FHD (1920*1080), 144Hz, IPS-Level', ""),
    ("Memory", ""),
    ("8GB DDR4", ""),
    ("2x DDR4 SO-DIMM slots", ""),
    ("Up to 64GB", ""),
    ("Storage", ""),
    ("512GB NVMe PCIe Gen4 SSD", ""),
    ("Graphics", ""),
    ("NVIDIA GeForce RTX 3050 Laptop GPU 4GB", ""),
)
PCC_TITLE = 'Example Thin 15 B13UC-0000PH | 15.6" | i5-13420H | 8GB | 512GB SSD'


def pcc_offer(scoped: tuple[tuple[str, str, str], ...], prefix: str = "16GB / 512GB") -> object:
    joined = " | ".join(f"{label}: {value}" if value else label for label, value in PCC_DESCRIPTION)
    return offer(
        "pcc", PCC_DESCRIPTION, title=PCC_TITLE, configuration=f"{prefix} | {joined}",
        scoped=scoped,
    )


BOUND = (
    (SELECTED_VARIANT, "binding status", "CONFIRMED"),
    (SELECTED_VARIANT, "variant id", "50000000000002"),
    (SELECTED_VARIANT, "RAM", "16GB (2x8) DDR4 RAM"),
    (SELECTED_VARIANT, "SSD", "512GB SDD"),
)


def test_pc_central_paragraph_groups_are_supporting_on_a_shared_description() -> None:
    page = pcc_offer(())
    fields, support = primary(page), supporting(page)
    assert fields["screen_size_in"] == (N, "")
    assert fields["cpu_series"] == (N, "")
    assert fields["storage_capacity_gb"] == (N, "")
    assert support["screen_size_in"] == ["15.6"]
    assert support["cpu_series"] == ["Core i5"]
    assert support["storage_capacity_gb"] == ["512"]
    assert support["ram_capacity_gb"] == ["8"]
    assert support["ram_maximum_gb"] == ["64"]
    assert support["ram_slots"] == ["2"]
    assert "MULTI_VARIANT_SHARED_DESCRIPTION" in notes(page)


def test_pc_central_bound_selected_variant_supplies_installed_capacities() -> None:
    page = pcc_offer(BOUND)
    fields, support = primary(page), supporting(page)
    assert fields["ram_capacity_gb"] == (E, "16")
    assert fields["storage_capacity_gb"] == (E, "512")
    assert fields["screen_size_in"] == (N, "")  # Shared description: never inherited.
    assert fields["cpu_series"] == (N, "")
    assert fields["model"][0] != E
    assert support["ram_capacity_gb"] == ["8"]  # Shared 8GB kept separately.
    assert support["ram_maximum_gb"] == ["64"]
    assert "SHARED_DESCRIPTION_DIFFERS_RAM_CAPACITY_GB" in notes(page)
    ram = next(
        e for e in extract_offer_attributes(page).evidence
        if e.field == "ram_capacity_gb" and e.status is ExtractionStatus.EXTRACTED
    )
    assert ram.source_label == "RAM [selected variant 50000000000002]"
    assert ram.source_text == "16GB (2x8) DDR4 RAM"


@pytest.mark.parametrize(
    "binding",
    [
        "NOT_CONFIRMED: the checked option values differ from the variant data",
        "NOT_CONFIRMED: the selected <option> names another variant",
    ],
)
def test_pc_central_unbound_selection_supplies_nothing(binding: str) -> None:
    page = pcc_offer(((SELECTED_VARIANT, "binding status", binding),))
    fields = primary(page)
    assert fields["ram_capacity_gb"] == (N, "")
    assert fields["storage_capacity_gb"] == (N, "")
    assert "SELECTED_VARIANT_NOT_CONFIRMED" in notes(page)


def test_pc_central_option_rows_without_a_binding_are_ignored() -> None:
    page = pcc_offer(((SELECTED_VARIANT, "RAM", "16GB DDR4 RAM"),))
    assert primary(page)["ram_capacity_gb"] == (N, "")


def test_pc_central_enumerated_variants_stay_supporting() -> None:
    url = f"https://{PCC_HOST}/products/example?variant=50000000000003"
    page = pcc_offer((*BOUND, (ENUMERATED_VARIANT, "32GB / 512GB", url)))
    assert supporting(page)["enumerated_variant"] == [url]


# PC Central adapter: binding the requested variant to its option controls


PCC_FIXTURE = Path(__file__).resolve().parent / "scraping_fixtures" / "pcc"
VARIANTS_URL = (
    f"https://{PCC_HOST}/products/example-15-ex0001au-ryzen-7-8gb-256gb-ssd-win-11-home-pc-central"
)
V1, V2 = "50000000000001", "50000000000002"
SSD_FIELDSET = (
    '<fieldset class="js product-form__input"><legend class="form__label">SSD</legend>'
    '<input type="radio" id="ssd-0" name="SSD" value="256GB SSD" checked>'
    '<label for="ssd-0">256GB SSD</label></fieldset>\n<script type="application/json">['
)


def pcc_page(body: str, url: str) -> OfferObservation:
    clock = FakeClock()
    transport = FakeTransport(clock, TransportResponse(200, url, body.encode(), {
        "content-type": "text/html; charset=utf-8"
    }))
    [result] = scrape(
        PCCAdapter(), [ScrapeRequest("pcc", url)], Fetcher(transport, FetchConfig(), clock)
    )
    assert result.ok, result.failures
    [observation] = result.observations
    return observation


def variants_body() -> str:
    text = (PCC_FIXTURE / "pcc_product_variants_sale.html").read_text(encoding="utf-8")
    marker = '</fieldset>\n<script type="application/json">['
    assert marker in text
    return text.replace(marker, "</fieldset>\n" + SSD_FIELDSET, 1)


def scoped_of(observation: OfferObservation, scope: str) -> list[tuple[str, str]]:
    return [(label, value) for s, label, value in observation.details.scoped_evidence if s == scope]


def test_adapter_binds_the_selected_variant_when_every_signal_agrees() -> None:
    page = pcc_page(variants_body(), f"{VARIANTS_URL}?variant={V1}")
    assert scoped_of(page, SELECTED_VARIANT) == [
        ("binding status", "CONFIRMED"),
        ("variant id", V1),
        ("RAM", "8GB DDR4 RAM"),
        ("SSD", "256GB SSD"),
    ]
    # The other variant is listed with the URL from the page's own JSON-LD, never built.
    assert scoped_of(page, ENUMERATED_VARIANT) == [
        ("16GB (2x8) DDR4 RAM / 256GB SSD", f"{VARIANTS_URL}?variant={V2}")
    ]


def test_adapter_reports_a_checked_option_that_disagrees() -> None:
    body = variants_body().replace('value="8GB DDR4 RAM" checked', 'value="8GB DDR4 RAM"', 1)
    body = body.replace('value="16GB (2x8) DDR4 RAM">', 'value="16GB (2x8) DDR4 RAM" checked>', 1)
    page = pcc_page(body, f"{VARIANTS_URL}?variant={V1}")
    assert scoped_of(page, SELECTED_VARIANT) == [
        ("binding status", "NOT_CONFIRMED: the checked option values differ from the variant data")
    ]
    assert primary(page)["ram_capacity_gb"] == (N, "")


def test_adapter_reports_missing_option_controls() -> None:
    body = (PCC_FIXTURE / "pcc_product_variants_sale.html").read_text(encoding="utf-8")
    body = body.replace(" checked>", ">")
    page = pcc_page(body, f"{VARIANTS_URL}?variant={V1}")
    assert scoped_of(page, SELECTED_VARIANT) == [
        ("binding status", "NOT_CONFIRMED: no checked option control was found")
    ]


def test_adapter_never_invents_a_variant_url() -> None:
    body = variants_body().replace(f"?variant={V2}", "", 1)
    page = pcc_page(body, f"{VARIANTS_URL}?variant={V1}")
    assert scoped_of(page, ENUMERATED_VARIANT) == []


def test_adapter_keeps_price_and_hidden_text_safeguards() -> None:
    page = pcc_page(variants_body(), f"{VARIANTS_URL}?variant={V1}")
    assert page.availability is Availability.IN_STOCK  # Hidden "sold out" labels ignored.
    assert page.price == Decimal("41999.00")


# Octagon: Tech Specification paragraphs with footnotes


OCT_ROWS = (
    ("Specification", ""),
    ("CPU and chipset1 AMD Ryzen 5 6600H", ""),
    ("Memory1, 2, 3", ""),
    ("1x 16GB DDR5 4800 MT/s", ""),
    ("Up to 32GB", ""),
    ("Display4", ""),
    ('15.6" FHD (1920 x 1080) IPS, 16', "9 aspect ratio, 144Hz"),
    ("Storage1, 5", ""),
    ("1x 512 PCIe Gen4, 16 Gb/s", ""),
    ("Graphics", ""),
    ("NVIDIA GeForce RTX 4050", ""),
)
OCT_TITLE = "Acer Nitro V ANV15-00-XXXX OPI | Ryzen 5 7640HS | 16GB DDR5 | 512GB SSD | 15.6 FHD"


def test_octagon_footnoted_headings_and_installed_memory() -> None:
    page = offer("oct", OCT_ROWS, title=OCT_TITLE)
    fields, support = primary(page), supporting(page)
    assert fields["screen_size_in"] == (E, "15.6")
    assert fields["cpu_series"] == (E, "Ryzen 5")
    assert fields["ram_capacity_gb"] == (E, "16")
    assert fields["storage_capacity_gb"] == (U, "")
    assert fields["model"] == (M, "")
    assert support["ram_maximum_gb"] == ["32"]
    assert support["ram_frequency"] == ["4800 MT/s"]
    assert support["storage_amount_without_unit"] == ["512"]
    assert support["storage_throughput"] == ["16 Gb/s"]
    assert support["cpu_model"] == ["title: 7640HS; specification: 6600H"]
    assert {"STORAGE_CAPACITY_UNIT_MISSING", "CPU_NUMBER_TITLE_DIFFERS"} <= set(notes(page))


def test_octagon_values_keep_ratios_and_counts() -> None:
    evidence = extract_offer_attributes(offer("oct", OCT_ROWS)).evidence
    screen = next(e for e in evidence if e.field == "screen_size_in")
    ram = next(e for e in evidence if e.field == "ram_capacity_gb")
    assert "16:9 aspect ratio" in screen.source_text
    assert "1x 16GB" in ram.source_text
    assert screen.source_label == "Display"


def test_maximum_memory_is_never_installed_memory() -> None:
    page = offer("oct", (("Memory", ""), ("Up to 32GB DDR5", "")))
    assert primary(page)["ram_capacity_gb"][0] != E
    assert supporting(page)["ram_maximum_gb"] == ["32"]


def test_throughput_is_never_a_capacity() -> None:
    page = offer("oct", (("Storage", ""), ("PCIe Gen4 SSD, 16 GB/s", "")))
    assert primary(page)["storage_capacity_gb"][0] != E
    assert supporting(page)["storage_throughput"] == ["16 GB/s"]


def test_lines_before_the_first_heading_and_unknown_headings_are_not_values() -> None:
    page = offer("oct", (("Memory Frequency 4800", ""), ("16GB", "")))
    assert primary(page)["ram_capacity_gb"] == (M, "")


# Legacy records and the append-only store


def test_legacy_record_without_scoped_evidence_keeps_its_identity(tmp_path: Path) -> None:
    legacy = offer("pcx", PCX_ROWS)
    record, text, _ = canonical_record(legacy)
    assert "scoped_evidence" not in text
    path = tmp_path / "legacy.csv"
    columns = [c for c in (*OBSERVATION_COLUMNS, *OFFER_DETAIL_COLUMNS) if c != "scoped_evidence"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerow(legacy.to_full_record())
    [reread] = read_observations_csv(path)
    assert observation_id(reread.observation) == observation_id(legacy)
    assert canonical_record(reread.observation)[2] == canonical_record(legacy)[2]
    with ObservationStore.open(tmp_path / "store.sqlite3") as store:
        store.import_observations([legacy], source_label="a", imported_at=datetime.now(UTC))
        report = store.import_observations(
            [reread.observation], source_label="b", imported_at=datetime.now(UTC)
        )
        assert (report.new, report.duplicates_in_store) == (0, 1)


def test_changed_content_under_an_old_identity_is_refused(tmp_path: Path) -> None:
    from laptop_tracker.observation_store import ObservationConflictError

    legacy = offer("pcx", PCX_ROWS)
    newer = offer("pcx", PCX_ROWS, scoped=((IDENTIFIER, "JSON-LD Product.sku", "X-1"),))
    assert observation_id(newer) == observation_id(legacy)
    with ObservationStore.open(tmp_path / "store.sqlite3") as store:
        store.import_observations([legacy], source_label="a", imported_at=datetime.now(UTC))
        with pytest.raises(ObservationConflictError):
            store.import_observations([newer], source_label="b", imported_at=datetime.now(UTC))
        assert store.counts()["observations"] == 1
