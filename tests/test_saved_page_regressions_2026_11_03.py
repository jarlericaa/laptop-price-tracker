"""Extraction regressions from the saved 2026-11-03 pages (HP 15-fc0933AU, Nitro V, MacBook Air).

Each fixture under ``tests/scraping_fixtures/saved_2026-11-03/`` is a trimmed copy of a real saved
retailer page (see ``SOURCES.json`` there for URL, retrieval time and the sha256 of the original
evidence file). Tests that need a variation of a page change one field of the parsed observation, so
the unsafe case is always the real page with a single edit. Nothing here touches a network.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest
from scraping_fakes import no_network  # noqa: F401

from laptop_tracker.adapters.datablitz import DataBlitzAdapter
from laptop_tracker.adapters.laptop_factory import LaptopFactoryAdapter
from laptop_tracker.adapters.oct import OCTAdapter
from laptop_tracker.adapters.pcc import PCCAdapter
from laptop_tracker.adapters.pcx import PCXAdapter
from laptop_tracker.alternatives import (
    ALTERNATIVE,
    CONFLICTING,
    DIFFERS,
    EXCLUDED_DIFFERENCES,
    RECHECK,
    AlternativeEvaluator,
    offer_evidence,
)
from laptop_tracker.extraction import (
    MARKETED_SCREEN_CLASSES,
    ExtractionStatus,
    extract_offer_attributes,
)
from laptop_tracker.matching import ExactMatcher, MatchStatus
from laptop_tracker.offers import OfferObservation, clean_text
from laptop_tracker.scraping import FetchedPage, ParseError, build_observation

pytestmark = pytest.mark.usefixtures("no_network")

FIXTURES = Path(__file__).resolve().parent / "scraping_fixtures" / "saved_2026-11-03"
SOURCES = json.loads((FIXTURES / "SOURCES.json").read_text(encoding="utf-8"))["pages"]
ADAPTERS = {
    "datablitz": DataBlitzAdapter,
    "pcx": PCXAdapter,
    "laptop_factory": LaptopFactoryAdapter,
    "oct": OCTAdapter,
    "pcc": PCCAdapter,
}
E = ExtractionStatus.EXTRACTED


def page_of(name: str, url: str | None = None) -> FetchedPage:
    meta = SOURCES[f"{name}.html"]
    url = url or meta["url"]
    return FetchedPage(
        retailer_id=meta["retailer_id"],
        requested_url=url,
        final_url=url,
        status_code=200,
        body=(FIXTURES / f"{name}.html").read_bytes(),
        content_type="text/html; charset=utf-8",
        retrieved_at=datetime.fromisoformat(meta["retrieved_at"].replace("Z", "+00:00")),
        attempts=1,
    )


def saved(name: str, url: str | None = None) -> OfferObservation:
    page = page_of(name, url)
    adapter = ADAPTERS[page.retailer_id]()
    (offer,) = list(adapter.parse(page))
    return build_observation(adapter, page, offer, 0)


def edited(
    obs: OfferObservation,
    *,
    title: str | None = None,
    url: str | None = None,
    config: str | None = None,
    rows=None,
    scoped=None,
    drop_identifiers: bool = False,
    brand: str | None = None,
):
    """The same observation with one piece of page evidence changed."""
    details = obs.details
    config = config if config is not None else obs.raw_configuration_text
    if rows is not None:
        old = tuple(details.raw_specifications)
        new = tuple(rows(list(old)))
        if clean_text(config) == clean_text(joined(old)):
            config = joined(new)  # A single-variant page's summary is its description, joined.
        details = replace(details, raw_specifications=new)
    if scoped is not None:
        details = replace(details, scoped_evidence=tuple(scoped(list(details.scoped_evidence))))
    if drop_identifiers:
        details = replace(
            details,
            scoped_evidence=tuple(s for s in details.scoped_evidence if s[0] != "IDENTIFIER"),
        )
    if brand is not None:
        details = replace(details, brand=brand)
    return replace(
        obs,
        details=details,
        raw_title=title if title is not None else obs.raw_title,
        offer_url=url if url is not None else obs.offer_url,
        raw_configuration_text=config,
    )


def joined(specs) -> str:
    return " | ".join(f"{label}: {value}" if value else label for label, value in specs)


def set_row(label: str, value: str):
    return lambda rows: [(k, value if k == label else v) for k, v in rows]


def drop_rows(*labels: str):
    return lambda rows: [(k, v) for k, v in rows if k not in labels]


def fields(obs: OfferObservation):
    """``{field: (status, value)}`` of the primary (non-supporting) evidence."""
    return {
        e.field: (e.status, e.value)
        for e in extract_offer_attributes(obs).evidence
        if e.status is not ExtractionStatus.SUPPORTING
    }


def supporting(obs: OfferObservation, field: str) -> list[str]:
    return [
        e.value
        for e in extract_offer_attributes(obs).evidence
        if e.field == field and e.status is ExtractionStatus.SUPPORTING
    ]


def target(**overrides) -> dict[str, str]:
    row = {
        "target_id": "TGT-000288",
        "status": "ACTIVE",
        "brand": "HP",
        "series": "HP Laptop",
        "model": "15-fc0933AU",
        "screen_size_in": "15.6",
        "cpu_series": "Ryzen 3",
        "cpu_model": "Ryzen 3 7320U",
        "ram_capacity_gb": "8",
        "storage_capacity_gb": "512",
        "tracking_mode": "CONFIGURATION",
    }
    return row | overrides


def alternative(obs: OfferObservation, row: dict[str, str]):
    return AlternativeEvaluator([row]).evaluate(
        row, offer_evidence(extract_offer_attributes(obs), obs.raw_title)
    )


def match(obs: OfferObservation, row: dict[str, str]):
    return ExactMatcher([row]).match_observation(obs)


# 1. DataBlitz HP 15-fc0933AU: a 15.6" laptop sold with a 16" briefcase.


def test_datablitz_hp_briefcase_size_never_contaminates_the_laptop_screen() -> None:
    obs = saved("datablitz_hp_15_fc0933au")
    assert 'HP Everyday 16" Laptop Briefcase' in obs.raw_title
    extracted = extract_offer_attributes(obs)
    assert extracted.values["screen_size_in"] == "15.6"
    # The spec row also states 39.6 cm: the same diagonal in centimetres, not a third size.
    assert extracted.values["model"] == "15-fc0933AU"
    assert (
        extracted.values["ram_capacity_gb"] == "8"
        and extracted.values["storage_capacity_gb"] == "512"
    )
    assert not any(
        code.startswith(("TITLE_SPEC_CONFLICT", "AMBIGUOUS")) for code in extracted.reason_codes
    )
    # The 16" the description gives the briefcase is not even kept as screen evidence.
    assert supporting(obs, "screen_size_in") and set(supporting(obs, "screen_size_in")) == {"15.6"}
    assert "16" not in {e.value for e in extracted.evidence if e.field == "screen_size_in"}


def test_datablitz_hp_page_is_an_exact_match_for_its_own_code_and_not_for_another() -> None:
    obs = saved("datablitz_hp_15_fc0933au")
    assert match(obs, target()).status is MatchStatus.EXACT
    # A genuinely different manufacturer code still does not match, with everything else equal.
    assert match(obs, target(model="15-fc0993AU")).status is not MatchStatus.EXACT


@pytest.mark.parametrize(
    "accessory",
    [
        'HP Everyday 16" Laptop Briefcase',
        "HP 16-inch Laptop Sleeve",
        '16" Backpack',
        "15.6-inch laptop carrying case",
    ],
)
def test_an_accessory_size_in_the_title_is_not_a_title_screen_claim(accessory: str) -> None:
    obs = saved("datablitz_hp_15_fc0933au")
    title = (
        'HP 15-fc0933AU Laptop (Natural Silver) | 15.6" FHD IPS | Ryzen 3 7320U | 8GB RAM | '
        f"512GB SSD + {accessory}"
    )
    fields_ = fields(edited(obs, title=title))
    assert fields_["screen_size_in"] == (E, "15.6")


def test_a_briefcase_alone_never_becomes_the_laptop_screen() -> None:
    obs = saved("datablitz_hp_15_fc0933au")
    # No display statement anywhere except the briefcase's size: the screen stays unknown.
    bare = edited(
        obs,
        title=(
            "HP 15-fc0933AU Laptop (Natural Silver) | Ryzen 3 7320U | 8GB RAM | 512GB SSD + "
            'HP Everyday 16" Laptop Briefcase'
        ),
        rows=drop_rows("Display", "Display size (diagonal)"),
        scoped=lambda items: [
            s for s in items if "p[2]" not in s[1] and "Product.description" not in s[1]
        ],
    )
    status, value = fields(bare)["screen_size_in"]
    assert status is ExtractionStatus.MISSING and value == ""
    assert "16" not in supporting(bare, "screen_size_in")
    # ... so the offer is a recheck for the screen, never an agreement or a difference.
    result = alternative(bare, target(ram_capacity_gb="16"))
    assert result.status == RECHECK and "REQUIRED_SCREEN_NOT_ESTABLISHED" in result.reasons


def test_a_real_16_inch_laptop_keeps_its_size_even_when_sold_with_a_bag() -> None:
    obs = saved("datablitz_hp_15_fc0933au")
    sixteen = edited(
        obs,
        title='HP OmniBook 16" Laptop | Ryzen 3 7320U | 8GB RAM | 512GB SSD + 16" Laptop Backpack',
        rows=set_row("Display", '40.6 cm (16") diagonal, WUXGA'),
        scoped=lambda items: [s for s in items if "description" not in s[1].casefold()],
    )
    assert fields(sixteen)["screen_size_in"] == (E, "16")
    # The size right before a display word is the laptop's: "15.6" FHD IPS display with a bag".
    mixed = edited(
        obs,
        title=(
            'HP Laptop 14" | 15.6" FHD IPS display with a free bag | Ryzen 3 7320U | 8GB RAM | '
            "512GB SSD"
        ),
        rows=set_row("Display", '14" diagonal'),
        scoped=lambda items: [s for s in items if "description" not in s[1].casefold()],
    )
    assert (
        fields(mixed)["screen_size_in"][0] is ExtractionStatus.AMBIGUOUS
    )  # 14 vs 15.6 stays a conflict


def test_a_metric_figure_is_only_the_same_diagonal_when_it_converts_to_it() -> None:
    obs = saved("datablitz_hp_15_fc0933au")

    def prose(items):
        # Explicit product prose ("15.6-inch laptop") is what makes the metric figure matter.
        return [
            *items,
            (
                "PRODUCT_DESCRIPTION",
                "product description > p[9]",
                "A 15.6-inch laptop for everyday work.",
            ),
        ]

    same = edited(obs, scoped=prose)
    assert fields(same)["screen_size_in"] == (E, "15.6")  # 39.6 cm is 15.6", not 15.59
    wrong = edited(obs, scoped=prose, rows=set_row("Display", '45.6 cm (15.6") diagonal, FHD'))
    status, value = fields(wrong)["screen_size_in"]
    assert status is ExtractionStatus.AMBIGUOUS and "17.95" in value  # 45.6 cm is 17.95"
    whole = edited(obs, scoped=prose, rows=set_row("Display", '40 cm (15.6") diagonal, FHD'))
    assert fields(whole)["screen_size_in"][0] is ExtractionStatus.AMBIGUOUS  # no whole-cm slack


# 2. PC Express HP 15-fc0933AU: the title spells the code with a space.


def test_pcx_hp_title_with_a_space_is_read_as_the_hyphenated_code_the_page_also_writes() -> None:
    obs = saved("pcx_hp_15_fc0933au")
    assert obs.raw_title.startswith("HP 15 FC0933AU (DM8L7PA)")
    extracted = extract_offer_attributes(obs)
    assert extracted.values["model"] == "15-FC0933AU"
    assert "TITLE_MODEL_SPACING_NORMALIZED" in extracted.notes
    label = next(e.source_label for e in extracted.evidence if e.field == "model")
    assert "TITLE_MODEL_CORROBORATED" in label and "'15 FC0933AU' read as 15-FC0933AU" in label
    assert "the product URL path" in label
    # The catalog writes the code with different case; the matcher compares case-folded.
    assert match(obs, target()).status is MatchStatus.EXACT
    assert match(obs, target(model="15-fc0993AU")).status is not MatchStatus.EXACT


def test_the_hyphenated_form_can_come_from_the_retailer_configuration_line() -> None:
    obs = saved("pcx_hp_15_fc0933au")
    url_only_elsewhere = edited(
        obs, url="https://pcx.com.ph/products/hp-laptop-natural-silver", drop_identifiers=True
    )
    assert "LT-HP-15-FC0933AU" in url_only_elsewhere.raw_configuration_text
    assert fields(url_only_elsewhere)["model"] == (E, "15-FC0933AU")


def test_a_spaced_title_code_is_not_read_when_the_page_never_hyphenates_it() -> None:
    obs = saved("pcx_hp_15_fc0933au")
    bare = edited(
        obs,
        url="https://pcx.com.ph/products/hp-laptop-natural-silver",
        config="NATURAL SILVER AMD RYZEN 3 7320U 8GB DDR4 512GB SSD",
        drop_identifiers=True,
    )
    assert fields(bare)["model"][0] is ExtractionStatus.MISSING
    assert "TITLE_MODEL_SPACING_NORMALIZED" not in extract_offer_attributes(bare).notes


def test_a_url_naming_another_code_does_not_corroborate_the_spaced_title() -> None:
    obs = saved("pcx_hp_15_fc0933au")
    other = edited(
        obs,
        url="https://pcx.com.ph/products/hp-15-fc0993au-dm8l7pa-amd-ryzen-3-laptop",
        config="NATURAL SILVER AMD RYZEN 3 7320U 8GB DDR4 512GB SSD",
        drop_identifiers=True,
    )
    assert fields(other)["model"][0] is ExtractionStatus.MISSING


def test_processor_and_quantity_words_are_never_a_spaced_model_code() -> None:
    obs = saved("pcx_hp_15_fc0933au")
    cpu = edited(
        obs,
        title="HP Pavilion Laptop AMD Ryzen 5 7530U 8GB 512GB",
        url="https://pcx.com.ph/products/hp-pavilion-amd-ryzen-5-7530u-8gb-512gb-laptop",
        config="LT-HP-RYZEN-5-7530U 8GB-512GB",
        drop_identifiers=True,
    )
    assert fields(cpu)["model"][0] is ExtractionStatus.MISSING


def test_a_brand_without_a_reviewed_spaced_shape_is_never_read_from_a_url_slug() -> None:
    # The 2026-11-03 PC Express Lenovo page: a model name and a machine-type number, side by side
    # in the title and hyphenated by the slug. They are not one code, and the extractor says so.
    obs = saved("pcx_hp_15_fc0933au")
    lenovo = edited(
        obs,
        brand="Lenovo",
        title="Lenovo IdeaPad Slim 5 14AKP10 83HX00BCPH AMD Ryzen AI 5 Laptop",
        url="https://pcx.com.ph/products/lenovo-ideapad-slim-5-14akp10-83hx00bcph-amd-ryzen-ai-5-laptop",
        config="LT-LEN-IDEAPAD-SLIM-5-14AKP10-83HX00BCPH 8GB 512GB",
        drop_identifiers=True,
    )
    assert fields(lenovo)["model"][0] is ExtractionStatus.MISSING
    # ... and the same title words under HP's shape are not an HP code either.
    assert (
        fields(
            edited(
                obs,
                title="HP 14AKP10 83HX00BCPH Laptop",
                url="https://pcx.com.ph/products/hp-14akp10-83hx00bcph-laptop",
                drop_identifiers=True,
            )
        )["model"][0]
        is ExtractionStatus.MISSING
    )


def test_a_second_spaced_candidate_makes_the_title_ambiguous_not_chosen() -> None:
    obs = saved("pcx_hp_15_fc0933au")
    both = edited(
        obs,
        title="HP 15 FC0933AU 15 FC0993AU Laptop",
        url="https://pcx.com.ph/products/hp-15-fc0933au-15-fc0993au-laptop",
        config="LT-HP-15-FC0933AU 15-FC0993AU",
        drop_identifiers=True,
    )
    assert fields(both)["model"][0] is ExtractionStatus.MISSING


# 3. Octagon Acer Nitro V ANV15-52-572T: free-form Tech Specification section.

OCT_TARGET = {
    "target_id": "TGT-000292",
    "status": "ACTIVE",
    "brand": "Acer",
    "series": "Nitro V",
    "model": "ANV15-52-572T",
    "screen_size_in": "15.6",
    "cpu_series": "Core i5",
    "cpu_model": "Core i5-13420H",
    "ram_capacity_gb": "16",
    "storage_capacity_gb": "512",
    "gpu_model": "RTX 5050",
    "tracking_mode": "CONFIGURATION",
}


def test_octagon_nitro_model_and_display_come_from_the_free_form_tech_specification() -> None:
    obs = saved("oct_acer_nitro_v_anv15_52_572t")
    labels = [label for label, _ in obs.details.raw_specifications]
    assert "Display Panel / Resolution" in labels and "Model" not in labels
    extracted = extract_offer_attributes(obs)
    assert extracted.values["model"] == "ANV15-52-572T"
    assert extracted.values["screen_size_in"] == "15.6"
    assert extracted.values["ram_capacity_gb"] == "16"  # "2x 8GB ... upgradable to 32 GB"
    assert extracted.values["storage_capacity_gb"] == "512"
    label = next(e.source_label for e in extracted.evidence if e.field == "model")
    assert (
        "TITLE_MODEL_CORROBORATED" in label
        and "screen_size_in: Display Panel / Resolution" in label
    )
    assert match(obs, OCT_TARGET).status is MatchStatus.EXACT
    assert match(obs, OCT_TARGET | {"model": "ANV15-52-585U"}).status is not MatchStatus.EXACT


def test_octagon_display_row_is_the_screen_the_offer_states_never_the_catalog_value() -> None:
    obs = saved("oct_acer_nitro_v_anv15_52_572t")
    other = edited(
        obs,
        rows=set_row("Display Panel / Resolution", '17.3" display with IPS technology, Full HD'),
    )
    assert fields(other)["screen_size_in"] == (E, "17.3")
    result = match(other, OCT_TARGET)
    assert result.status is not MatchStatus.EXACT  # 17.3 is not the catalog's 15.6
    # A row that states two sizes is ambiguous, not the first of them.
    two = edited(obs, rows=set_row("Display Panel / Resolution", '15.6" or 17.3" display, Full HD'))
    assert fields(two)["screen_size_in"][0] is ExtractionStatus.AMBIGUOUS
    assert fields(two)["model"][0] is not E  # the title code needs an established screen


# 4. PC Central Nitro ANV15-52-585U: damaged trademark text and the selected RAM variant.

PCC_TARGET = {
    "target_id": "TGT-000292",
    "status": "ACTIVE",
    "brand": "Acer",
    "series": "Nitro V",
    "model": "ANV15-52-585U",
    "screen_size_in": "15.6",
    "cpu_series": "Core 5",
    "ram_capacity_gb": "16",
    "storage_capacity_gb": "512",
    "tracking_mode": "CONFIGURATION",
}
OTHER_VARIANT = "48504754995367"  # 32GB (2x16) DDR4 RAM / 512GB SSD, listed by the same page


def nitro_32gb(obs: OfferObservation) -> OfferObservation:
    """The 32GB variant's evidence: the page's other picker option selected, same description."""

    def swap(items):
        result = []
        for scope, label, value in items:
            if scope == "ENUMERATED_VARIANT":
                continue
            if scope == "SELECTED_VARIANT":
                value = (
                    OTHER_VARIANT
                    if label == "variant id"
                    else "32GB (2x16) DDR4 RAM"
                    if label == "RAM"
                    else value
                )
            elif scope == "VARIANT_DEFINITIONS":
                value = json.dumps(
                    json.loads(value) | {"selected_id": OTHER_VARIANT}, sort_keys=True
                )
            result.append((scope, label, value))
        return result

    return edited(obs, scoped=swap)


def test_pcc_nitro_damaged_trademark_is_read_only_because_the_title_agrees() -> None:
    obs = saved("pcc_acer_nitro_v_anv15_52_585u")
    assert any("Core? 5" in value for _, value in obs.details.raw_specifications) or any(
        "Core? 5" in label for label, _ in obs.details.raw_specifications
    )
    extracted = extract_offer_attributes(obs)
    assert extracted.values["cpu_series"] == "Core 5"
    assert "CPU_DAMAGED_MARK_TITLE_CORROBORATED" in extracted.notes
    label = next(e.source_label for e in extracted.evidence if e.field == "cpu_series")
    assert "damaged trademark mark dropped, same series stated in the title" in label
    assert extracted.values["model"] == "ANV15-52-585U"
    assert match(obs, PCC_TARGET).status is MatchStatus.EXACT


@pytest.mark.parametrize(
    "title",
    [
        "Acer Nitro V 15 ANV15-52-585U 15.6in FHD IPS 165Hz | Intel Core 7 210H | 16GB DDR4 | "
        "512GB SSD",
        "Acer Nitro V 15 ANV15-52-585U 15.6in FHD IPS 165Hz | 16GB DDR4 | 512GB SSD",
        "Acer Nitro V 15 ANV15-52-585U 15.6in FHD IPS 165Hz | Intel Core 5 or Core 7 | 16GB | "
        "512GB SSD",
    ],
)
def test_a_damaged_cpu_row_without_a_matching_title_series_is_not_read(title: str) -> None:
    obs = edited(saved("pcc_acer_nitro_v_anv15_52_585u"), title=title)
    status, value = fields(obs)["cpu_series"]
    assert status is not E and value in ("", "Core 5 | Core 7", "Core 5; Core 7")
    assert "CPU_DAMAGED_MARK_TITLE_CORROBORATED" not in extract_offer_attributes(obs).notes
    assert match(obs, PCC_TARGET).status is not MatchStatus.EXACT


def test_pcc_nitro_binds_ram_and_storage_to_the_selected_variant() -> None:
    sixteen = saved("pcc_acer_nitro_v_anv15_52_585u")
    assert sixteen.price == 69999 and "?variant=48504754897063" in sixteen.source.requested_url
    ram = next(
        e for e in extract_offer_attributes(sixteen).evidence if e.field == "ram_capacity_gb"
    )
    assert (ram.value, ram.scope) == ("16", "SELECTED_VARIANT")

    thirty_two = nitro_32gb(sixteen)
    extracted = extract_offer_attributes(thirty_two)
    ram = next(e for e in extracted.evidence if e.field == "ram_capacity_gb" and e.status is E)
    # The parent's description says 16GB; the selected option says 32GB and governs.
    assert (ram.value, ram.scope) == ("32", "SELECTED_VARIANT")
    assert extracted.values["storage_capacity_gb"] == "512"
    assert "SHARED_DESCRIPTION_DIFFERS_RAM_CAPACITY_GB" in extracted.notes
    # Parent specifications that do not vary (model, screen, CPU) support the child variant.
    for name in ("model", "screen_size_in", "cpu_series"):
        assert extracted.values[name] == extract_offer_attributes(sixteen).values[name]
    # Each variant matches its own configuration and never the other's.
    target_32 = PCC_TARGET | {"ram_capacity_gb": "32"}
    assert match(thirty_two, target_32).status is MatchStatus.EXACT
    assert match(thirty_two, PCC_TARGET).status is not MatchStatus.EXACT
    assert match(sixteen, PCC_TARGET).status is MatchStatus.EXACT
    assert match(sixteen, target_32).status is not MatchStatus.EXACT


def test_pcc_nitro_variant_is_not_bound_when_the_binding_is_not_confirmed() -> None:
    obs = saved("pcc_acer_nitro_v_anv15_52_585u")
    unbound = edited(
        obs,
        scoped=lambda items: [
            (
                s,
                label,
                "NOT_CONFIRMED: the checked option values differ from the variant data"
                if label == "binding status"
                else value,
            )
            for s, label, value in items
            if not (s == "SELECTED_VARIANT" and label in {"RAM", "SSD"})
        ],
    )
    extracted = extract_offer_attributes(unbound)
    assert "SELECTED_VARIANT_NOT_CONFIRMED" in extracted.notes
    assert (
        "ram_capacity_gb" not in extracted.values and "storage_capacity_gb" not in extracted.values
    )
    assert match(unbound, PCC_TARGET).status is not MatchStatus.EXACT


def test_pcc_parent_page_never_prices_a_variant_it_did_not_select() -> None:
    url = SOURCES["pcc_acer_nitro_v_anv15_52_585u.html"]["url"]
    base = url.split("?")[0]
    adapter = PCCAdapter()
    for requested in (url.replace("48504754897063", OTHER_VARIANT), base):
        with pytest.raises(ParseError) as error:
            list(adapter.parse(page_of("pcc_acer_nitro_v_anv15_52_585u", requested)))
        assert error.value.code == "AMBIGUOUS_VARIANT"


# 5. MacBook Air marketed as 13-inch: a reviewed, model-specific class-to-diagonal entry.

AIR_TARGET = {
    "target_id": "TGT-000045",
    "status": "ACTIVE",
    "brand": "Apple",
    "series": "",
    "model": "MacBook Air",
    "screen_size_in": "13",
    "cpu_series": "Apple M5",
    "cpu_model": "M5",
    "ram_capacity_gb": "24",
    "storage_capacity_gb": "1000",
    "tracking_mode": "CONFIGURATION",
}


def test_macbook_air_13_class_title_and_13_6_diagonal_are_one_display_for_the_listed_model() -> (
    None
):
    obs = saved("datablitz_macbook_air_13_m5")
    assert 'MacBook Air 13"' in obs.raw_title and "13.6-inch (diagonal)" in " ".join(
        v for _, v in obs.details.raw_specifications
    )
    extracted = extract_offer_attributes(obs)
    assert extracted.values["screen_size_in"] == "13.6"  # the physical display, never "13"
    assert (
        extracted.values["model"] == "MacBook Air" and extracted.values["cpu_series"] == "Apple M5"
    )
    assert "SCREEN_CLASS_NORMALIZED" in extracted.notes
    assert supporting(obs, "screen_size_in") == ["13"]  # the title's class stays visible
    assert not any(c.startswith("TITLE_SPEC_CONFLICT") for c in extracted.reason_codes)
    (entry,) = MARKETED_SCREEN_CLASSES
    assert (entry.product, entry.chip, entry.marketed, entry.diagonal) == (
        "MacBook Air",
        "Apple M5",
        "13",
        "13.6",
    ) and "13.6-inch" in entry.source


def test_the_normalization_never_edits_the_catalog_value_or_aliases_13_to_13_6() -> None:
    obs = saved("datablitz_macbook_air_13_m5")
    # A target recording the marketed class 13 still differs from the physical 13.6: one screen
    # difference (an ALT 1), resolved by the owner's catalog correction, not by an alias here.
    assert match(obs, AIR_TARGET).status is not MatchStatus.EXACT
    result = alternative(obs, AIR_TARGET)
    assert result.status == ALTERNATIVE and result.differing == ("screen",)
    assert result.tag == "ALT 1 · Screen" and result.dimension("screen").state == DIFFERS
    # The catalog proposal (SCREEN_CLASS_VS_DIAGONAL) records 13.6: then it is an exact match.
    assert match(obs, AIR_TARGET | {"screen_size_in": "13.6"}).status is MatchStatus.EXACT


@pytest.mark.parametrize(
    ("title", "chip_row", "why"),
    [
        (
            'Apple MacBook Air 13" 24GB+1TB M4 Chip (Silver)',
            "Apple M4 chip 10-core CPU",
            "chip not listed",
        ),
        (
            'Apple MacBook Pro 13" 24GB+1TB M5 Chip (Silver)',
            "Apple M5 chip 10-core CPU",
            "product not listed",
        ),
    ],
)
def test_other_chips_and_products_keep_the_title_and_specification_conflict(
    title: str, chip_row: str, why: str
) -> None:
    obs = saved("datablitz_macbook_air_13_m5")
    other = edited(obs, title=title, rows=set_row("Chip", chip_row))
    status, value = fields(other)["screen_size_in"]
    assert status is ExtractionStatus.AMBIGUOUS and value == "13.6 | title: 13", why
    assert "SCREEN_CLASS_NORMALIZED" not in extract_offer_attributes(other).notes
    assert fields(other)["model"][0] is not E  # a conflict blocks the title-supplied product name


def test_a_different_diagonal_or_brand_does_not_use_the_macbook_air_entry() -> None:
    obs = saved("datablitz_macbook_air_13_m5")
    shaped = edited(obs, rows=set_row("Display", "Liquid Retina display 13.3-inch (diagonal)"))
    assert fields(shaped)["screen_size_in"][0] is ExtractionStatus.AMBIGUOUS
    other_brand = edited(obs, brand="Acme")
    assert "SCREEN_CLASS_NORMALIZED" not in extract_offer_attributes(other_brand).notes


def test_there_is_no_generic_screen_tolerance_15_is_not_15_6() -> None:
    obs = saved("datablitz_hp_15_fc0933au")
    fifteen = edited(obs, title=obs.raw_title.replace('15.6"', '15"'))
    status, value = fields(fifteen)["screen_size_in"]
    assert status is ExtractionStatus.AMBIGUOUS and value == "15.6 | title: 15"
    assert "SCREEN_CLASS_NORMALIZED" not in extract_offer_attributes(fifteen).notes
    assert match(fifteen, target()).status is not MatchStatus.EXACT


# 7. Laptop Factory: a genuine contradiction stays a contradiction.


def test_laptop_factory_title_and_model_table_conflict_stays_visible_and_unapproved() -> None:
    obs = saved("laptop_factory_hp_15_fc0933au")
    assert obs.raw_title == "HP Laptop 15-fc0933AU"
    assert ("Model Name", "HP Laptop 15-fc0993AU") in obs.details.raw_specifications
    extracted = extract_offer_attributes(obs)
    model = next(e for e in extracted.evidence if e.field == "model")
    assert model.status is ExtractionStatus.AMBIGUOUS and "model" not in extracted.values
    # Both values stay visible, in the retailer's spelling; neither is corrected or merged.
    assert "15-fc0993AU" in model.value and "15-fc0933AU" in model.value
    assert "MODEL_TITLE_CONFLICT" in extracted.notes
    for code in ("15-fc0933AU", "15-fc0993AU"):
        result = match(obs, target(model=code))
        assert (
            result.status is not MatchStatus.EXACT and "MODEL_TITLE_CONFLICT" in result.reason_codes
        )
    # Every other field agrees, yet the offer is a RECHECK: never an exact match, never an
    # alternative, never a "model difference" that a one-code target could absorb.
    for code in ("15-fc0933AU", "15-fc0993AU"):
        result = alternative(obs, target(model=code))
        assert result.status == RECHECK and result.dimension("model").state == CONFLICTING
        assert result.difference_count == 0 and not result.is_alternative
    # The other retailers' spelling rules do not reach this page: its title has a hyphenated code.
    assert "TITLE_MODEL_SPACING_NORMALIZED" not in extracted.notes


# 8. The close-alternative budget is untouched by the extraction fixes.


def test_the_two_difference_budget_holds_on_the_corrected_extractions() -> None:
    obs = saved("pcx_hp_15_fc0933au")  # HP 15-fc0933AU: Ryzen 3, 8GB, 512GB, 15.6
    assert alternative(obs, target()).status != ALTERNATIVE  # no difference: not an alternative
    one = alternative(obs, target(ram_capacity_gb="16"))
    assert one.status == ALTERNATIVE and one.tag == "ALT 1 · RAM"
    two = alternative(obs, target(ram_capacity_gb="16", storage_capacity_gb="1000"))
    assert two.status == ALTERNATIVE and two.tag == "ALT 2 · RAM, Storage"
    three = alternative(
        obs,
        target(
            ram_capacity_gb="16", storage_capacity_gb="1000", cpu_series="Ryzen 5", cpu_model=""
        ),
    )
    assert three.status == EXCLUDED_DIFFERENCES and three.difference_count == 3
    assert not three.is_alternative


def test_an_unknown_dimension_is_still_not_an_agreement_after_the_fixes() -> None:
    obs = saved("pcx_hp_15_fc0933au")
    # Without the Display row the screen is unknown: RECHECK, even though RAM alone differs.
    unknown = edited(
        obs, rows=drop_rows("Display"), title="HP 15 FC0933AU (DM8L7PA) AMD Ryzen 3 Laptop"
    )
    result = alternative(unknown, target(ram_capacity_gb="16"))
    assert result.status == RECHECK and "REQUIRED_SCREEN_NOT_ESTABLISHED" in result.reasons


def test_a_screen_size_in_the_url_slug_is_not_another_final_segment_of_the_code() -> None:
    obs = saved("datablitz_hp_15_fc0933au")
    assert "silver-15-6-fhd" in obs.offer_url  # the slug's "15-6" follows the code's "15-" stem
    assert "MODEL_URL_DIFFERS" not in extract_offer_attributes(obs).notes
    # A lettered final segment after the same stem is still a conflicting slug (a warning only).
    other = edited(obs, url=obs.offer_url.replace("hp-15-fc0933au", "hp-15-fc0993au"))
    extracted = extract_offer_attributes(other)
    assert "MODEL_URL_DIFFERS" in extracted.notes and extracted.values["model"] == "15-fc0933AU"


# 9. PC Central single-product pages with named RAM/SSD options (restored 2026-11-03 candidates).

X1504 = "pcc_asus_vivobook_15_x1504va_bq4301wsm"
AG14 = "pcc_acer_aspire_go_14_ag14_71m_58jd"
X1504_TARGET = {
    "target_id": "TGT-000293",
    "status": "ACTIVE",
    "brand": "ASUS",
    "series": "Vivobook",
    "model": "X1504VA-BQ4301WSM",
    "screen_size_in": "15.6",
    "cpu_series": "Core 3",
    "ram_capacity_gb": "8",
    "storage_capacity_gb": "512",
    "tracking_mode": "CONFIGURATION",
}
AG14_TARGET = {
    "target_id": "TGT-000095",
    "status": "ACTIVE",
    "brand": "Acer",
    "series": "Aspire Go",
    "model": "AG14-71M-58JD",
    "screen_size_in": "14",
    "cpu_series": "Core Ultra 5",
    "ram_capacity_gb": "16",
    "storage_capacity_gb": "512",
    "tracking_mode": "CONFIGURATION",
}


def sole_matrix(obs: OfferObservation) -> dict:
    (value,) = [v for s, _, v in obs.details.scoped_evidence if s == "VARIANT_DEFINITIONS"]
    return json.loads(value)


@pytest.mark.parametrize(
    ("name", "row", "price", "ram", "storage", "cpu", "screen", "code"),
    [
        (X1504, X1504_TARGET, 36999, "8", "512", "Core 3", "15.6", "X1504VA-BQ4301WSM"),
        (AG14, AG14_TARGET, 50999, "16", "512", "Core Ultra 5", "14", "AG14-71M-58JD"),
    ],
)
def test_pcc_single_variant_page_with_named_options_extracts_model_screen_and_cpu(
    name, row, price, ram, storage, cpu, screen, code
) -> None:
    obs = saved(name)
    # The adapter proves the picker, option values and JSON-LD offers name exactly one variant.
    matrix = sole_matrix(obs)
    assert len(matrix["variants"]) == 1 and matrix["complete_sole_variant"] is True
    assert obs.price == price
    extracted = extract_offer_attributes(obs)
    assert extracted.values["model"] == code
    assert extracted.values["screen_size_in"] == screen and extracted.values["cpu_series"] == cpu
    assert (extracted.values["ram_capacity_gb"], extracted.values["storage_capacity_gb"]) == (
        ram,
        storage,
    )
    assert "SHARED_CPU_DISPLAY_RAM_STORAGE_ONLY" in extracted.notes
    assert "TITLE_MODEL_SCOPE_UNPROVEN" not in extracted.notes
    scopes = {e.field: e.scope for e in extracted.evidence if e.status is E}
    assert scopes["ram_capacity_gb"] == scopes["storage_capacity_gb"] == "SELECTED_VARIANT"
    assert match(obs, row).status is MatchStatus.EXACT
    assert match(obs, row | {"model": "X1504VA-BQ4302WSM"}).status is not MatchStatus.EXACT
    assert match(obs, row | {"ram_capacity_gb": "16" if ram == "8" else "8"}).status is not (
        MatchStatus.EXACT
    )


def test_a_one_variant_matrix_without_the_adapters_proof_still_proves_nothing() -> None:
    obs = saved(X1504)

    def unproven(items):
        out = []
        for scope, label, value in items:
            if scope == "VARIANT_DEFINITIONS":
                value = json.dumps(
                    {k: v for k, v in json.loads(value).items() if k != "complete_sole_variant"},
                    sort_keys=True,
                )
            out.append((scope, label, value))
        return out

    extracted = extract_offer_attributes(edited(obs, scoped=unproven))
    assert "TITLE_MODEL_SCOPE_UNPROVEN" in extracted.notes
    assert not {"model", "screen_size_in", "cpu_series"} & set(extracted.values)
    assert match(edited(obs, scoped=unproven), X1504_TARGET).status is not MatchStatus.EXACT


def test_the_selected_option_still_binds_ram_on_a_single_variant_page() -> None:
    obs = saved(AG14)

    def thirty_two(items):
        return [
            (s, label, "32GB DDR5 RAM" if (s, label) == ("SELECTED_VARIANT", "RAM") else v)
            for s, label, v in items
            if s != "VARIANT_DEFINITIONS"
        ]

    # With the option changed, the matrix (which still lists 16GB) no longer matches the binding,
    # so nothing is inherited: the page never reports the description's 16GB as the 32GB option's.
    changed = edited(obs, scoped=thirty_two)
    assert match(changed, AG14_TARGET).status is not MatchStatus.EXACT
    assert extract_offer_attributes(changed).values.get("ram_capacity_gb") != "16"


def test_pcc_single_variant_page_keeps_genuine_contradictions() -> None:
    x = saved(X1504)
    # The model row names a different designation than the title code: still a conflict.
    conflict = edited(x, rows=set_row("Model", "X1505VA"))
    extracted = extract_offer_attributes(conflict)
    assert "MODEL_CLAIM_CONFLICT" in extracted.notes and "model" not in extracted.values
    assert match(conflict, X1504_TARGET).status is not MatchStatus.EXACT
    # A title naming another processor series than the shared description is not inherited.
    a = saved(AG14)
    other = edited(a, title=a.raw_title.replace("Core Ultra 5", "Core Ultra 7"))
    extracted = extract_offer_attributes(other)
    assert "cpu_series" not in extracted.values
    assert "SHARED_PRODUCT_TITLE_DIFFERS_CPU_SERIES" in extracted.notes
    assert match(other, AG14_TARGET).status is not MatchStatus.EXACT


def test_alternatives_still_count_differences_for_the_single_variant_pages() -> None:
    obs = saved(AG14)
    one = alternative(obs, AG14_TARGET | {"ram_capacity_gb": "8"})
    assert one.status == ALTERNATIVE and one.tag == "ALT 1 · RAM"
    three = alternative(
        obs,
        AG14_TARGET
        | {"ram_capacity_gb": "8", "storage_capacity_gb": "256", "screen_size_in": "15.6"},
    )
    assert three.status == EXCLUDED_DIFFERENCES and not three.is_alternative
