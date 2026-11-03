"""Non-laptop results are rejected before they take a laptop candidate's place (2026-11-03).

Every title below is a real search result from the 2026-10-22 to 2026-11-03 discovery runs
(``candidates.csv``): components, memory, storage, graphics cards, phones, tablets, power banks,
a gaming chair and accessories that the title head or the missing product noun let through as
laptops of another model. A Laptop Factory ``Yoga Tab`` was even collected for a laptop target.
"""

from __future__ import annotations

import csv
from argparse import Namespace
from pathlib import Path

import pytest
from scraping_fakes import no_network  # noqa: F401
from test_product_discovery import NITRO, discover, hit, record

from laptop_tracker.product_discovery import (
    ACCESSORY,
    CODE_MATCH,
    NOT_A_LAPTOP,
    accessory_reason,
    non_laptop_reason,
    rank_hit,
    rank_target_hits,
)

pytestmark = pytest.mark.usefixtures("no_network")
ROOT = Path(__file__).resolve().parents[1]
DATABLITZ = "https://ecommerce.datablitz.com.ph/products/"

# (retailer product type, title, expected class)
REAL_REJECTIONS = [
    # motherboard: "(AM4)" cut the title head before its noun, so it ranked as another model
    ("PC Parts & Components", "Asus TUF Gaming B550M-Plus Wifi II (AM4) Motherboard", NOT_A_LAPTOP),
    (
        "PC Parts & Components",
        "ASUS ROG Strix B850-A Gaming WiFi7 Neo DDR5 AMD Motherboard",
        NOT_A_LAPTOP,
    ),
    # memory and storage
    ("Computer RAM", "Acer SD100 3200MHz CL22 DDR4 SODIMM (8GB, 16GB)", NOT_A_LAPTOP),
    (
        "Computer RAM",
        "Hiksemi Hiker DDR5 16GB (1x16GB) 5600MHz SODIMM Memory RAM (HSC516S56Z1)",
        NOT_A_LAPTOP,
    ),
    (
        "Internal SSD",
        "Lexar 16GB DDR5 5600MT/s CL46 SODIMM Laptop Memory (LD5S16G56C46ST-BGS)",
        NOT_A_LAPTOP,
    ),
    ("DIMM", "KINGSTON 8GB DDR4 3200MHZ FURY BEAST RAM", NOT_A_LAPTOP),
    (
        "UDIMM",
        "KINGSTON FURY Beast 16GB (1x 16GB) DDR5 5600MHz Desktop Memory - Black",
        NOT_A_LAPTOP,
    ),
    ("Solid State Drives (SSD)", "LEXAR 512GB NM620 M.2 PCIe NVMe Solid State Drive", NOT_A_LAPTOP),
    (
        "Solid State Drives (SSD)",
        "SAMSUNG 500GB 980 M.2 PCIE NVME (MZ-V8V500BW) Solid State Drive",
        NOT_A_LAPTOP,
    ),
    (
        "Memory Card",
        "Sandisk Extreme Pro UHS-II V90 300MB/s Read & Write SD Card (256GB, 512GB)",
        NOT_A_LAPTOP,
    ),
    (
        "Memory Card",
        "Samsung T7 UHS-I C10 U3 V30 A2 170MB/s Read microSDXC Card (512GB, 1TB)",
        NOT_A_LAPTOP,
    ),
    # graphics cards, phones, furniture
    (
        "Graphics Cards",
        "MSI GeForce RTX™ 5050 GAMING OC 8GB (G5050-8GC) GDDR6 128-bit Graphics Card",
        NOT_A_LAPTOP,
    ),
    ("VIDEO CARDS", "ASUS DUAL RTX3050 O6G", NOT_A_LAPTOP),
    ("Mobile Phones", "SAMSUNG GALAXY A17 LTE 8GB 256GB BLACK", NOT_A_LAPTOP),
    (
        "Gaming Chairs",
        "Asus ROG Destrier Core Ergo Gaming Chair with Futuristic Cyborg Aesthetic (Black, White)",
        NOT_A_LAPTOP,
    ),
    # tablets: one was a FAMILY_LISTED_SPEC_CANDIDATE and was collected for a laptop target
    ("", "Lenovo Yoga Tab ZAG60109PH Gen 3 12GB 256GB", NOT_A_LAPTOP),
    ("Electronics", "Lenovo Yoga Tab 11 YT-J706F 4GB+128GB (Storm Grey) ZA8W0011PH", NOT_A_LAPTOP),
    (
        "Tablet",
        'Lenovo Idea Tab Plus ZAGF0151PH 12.1" 2.5K 8GB+256GB 5G Sub-6 GHz (Luna Grey)',
        NOT_A_LAPTOP,
    ),
    # established classes, kept
    (
        "Mini PC",
        "Apple Mac Mini M6 Chip with 12-Core CPU and 12-Core GPU | 16GB Unified Memory + "
        "256GB SSD (Silver)",
        NOT_A_LAPTOP,
    ),
    (
        "Gaming Desktops",
        "Sigma A305 White Desktop Gaming PC | AMD Ryzen 5 5600 | 16GB (2x8GB) RAM "
        "| 1TB SSD | RTX 5060",
        NOT_A_LAPTOP,
    ),
    (
        "Gaming Monitor",
        'Acer Nitro ED270 ZBMIIPX 27" FHD (1920x1080) 240Hz Gaming Monitor',
        NOT_A_LAPTOP,
    ),
    ("Console System", "Steam Deck OLED (512GB, 1TB)", NOT_A_LAPTOP),
    # bags, cases, chargers, power banks and other accessories
    (
        "Accessories",
        "Tomtoc Defender-A14 Laptop Briefcase For 13-Inch Macbook Pro & Air",
        ACCESSORY,
    ),
    ("Bag", 'Tomtoc Defender-A14 Laptop Handbag for 14" MacBook Pro (Black) (A14D2D1)', ACCESSORY),
    ("Essentials", "Asus Vigour Backpack (AP2600)", ACCESSORY),
    ("Accessories", "Apple 35W Dual USB-C Port Compact Power Adapter (MW2K3CH/A)", ACCESSORY),
    ("Accessories", "Baseus GaN5 Pro Fast Charger 2C+U 140W (Black) (CCGP100101)", ACCESSORY),
    (
        "Power Bank",
        "UGREEN PB205/90597A 25000MAH POWERBANK 145W TWO-WAY FAST CHARGING - SPACE GRAY",
        ACCESSORY,
    ),
    ("", "UGREEN PB205/90597A 25000mAh Fast Charging Powerbank 145w", ACCESSORY),
    (
        "Keyboards",
        "Apple Magic Keyboard with Touch ID for Mac Models with Apple Silicon (MK293ZA/A)",
        ACCESSORY,
    ),
]

# Real laptop listings (every retailer's title style) that must never be rejected.
REAL_LAPTOPS = [
    ("Laptops", "LENOVO IdeaPad Slim 3 15AMN8 (82XQ00YHPH) AMD Ryzen™ 5 Laptop (Arctic Grey)"),
    (
        "Computer Non-Gaming Laptop",
        'Lenovo IdeaPad Slim 3 15IWC11 83RR00LHPH Laptop (Luna Grey) | 15.3" WUXGA (1920x1200) IPS',
    ),
    ("", "Lenovo IdeaPad Slim 3 15AMN8 82XQ01YCPH"),
    (
        "Laptop",
        'LENOVO IDEAPAD SLIM 3 151WC11 83RR00TSPH | 15.3" | INTEL CORE 5- 320 | 8GB DDR5 '
        "RAM | 512GB SSD",
    ),
    ("Laptop", "ASUS X1504VA-BQ4301WSM/ CORE 3-100U/ 8GB/ 512GB SSD/ 15.6 FHD/ WIN 11"),
    ("", "Macbook Neo A18 Pro 13″ 8GB 256GB SSD (Indigo)"),
    (
        "Laptop",
        'Apple MacBook Neo | Apple A18 Pro Chip | 13" Retina IPS Display | 8GB Unified '
        "Memory |Touch ID | PC Central",
    ),
    (
        "Computer Non-Gaming Laptop",
        'Apple MacBook Neo 13" 8GB+512GB A18 Pro Chip with 6-Core CPU'
        " & 5-Core GPU (Silver) (MHFC4PP/A)",
    ),
    (
        "Computer Non-Gaming Laptop",
        "Lenovo IdeaPad Slim 3 15AMN8 82XQ01FQPH Laptop (Arctic Grey) | "
        '15.6" FHD | Ryzen 5 40 | 16GB RAM | 512GB SSD | Windows 11 | '
        "Lenovo B210 Backpack",
    ),
    (
        "Gaming Laptops",
        "ACER Predator Helios Neo Slim 16 PHN16S-71-57XQ (NH.QX8SP.003) GeForce "
        "RTX™ 5050 Intel® Core™ Ultra 5 Laptop (Abyssal Black)",
    ),
    (
        "Laptop",
        "NINGMEI NLR500 | 15.6' | RYZEN 5 7430U | 16GB DDR4 RAM | 512GB SSD | AMD RADEON "
        "GRAPHICS | WIN 11 PRO",
    ),
    ("", "Acer Aspire Lite 14 AL14-45P-R10U"),
]


@pytest.mark.parametrize(("product_type", "title", "expected"), REAL_REJECTIONS)
def test_real_non_laptop_results_are_rejected_before_any_page_request(
    product_type, title, expected
) -> None:
    candidate = rank_hit(NITRO, hit(title, f"{DATABLITZ}x", product_type=product_type), [])
    assert candidate.category == expected, candidate.reasons
    assert "Rejected before any page request" in " ".join(candidate.reasons)


@pytest.mark.parametrize(("product_type", "title"), REAL_LAPTOPS)
def test_real_laptop_titles_are_never_rejected_as_non_laptops(product_type, title) -> None:
    assert non_laptop_reason(title, product_type) == ""
    assert accessory_reason(title, product_type) == ""


def test_a_component_url_handle_rejects_a_bare_title_only_without_a_laptop_word() -> None:
    url = "https://laptopfactory.com.ph/product/ugreen-pb205-90597a-25000mah-powerbank/"
    assert accessory_reason("UGREEN PB205/90597A 25000mAh Fast Charging Powerbank 145w") != ""
    assert non_laptop_reason("Some New Memory Listing", "", f"{DATABLITZ}acer-sodimm-8gb")
    # A laptop whose handle ends in a bundled backpack is untouched (bags are not handle tokens).
    laptop = f"{DATABLITZ}acer-aspire-go-15-ag15-43p-r19x-laptop-pure-silver-backpack"
    assert (
        non_laptop_reason("Acer Aspire Go 15 AG15-43P-R19X Laptop (Pure Silver)", "", laptop) == ""
    )
    assert url  # the Laptop Factory power bank above is classified by its title noun


def test_a_rejected_result_never_takes_a_candidate_slot_or_stops_the_search() -> None:
    """The motherboard, tablet and SSD precede the real laptop in search order; with room for one
    candidate the laptop is still the one chosen, and none of them counts as plausible."""
    target = NITRO
    cards = [
        hit(
            "Lenovo Yoga Tab ZAG60109PH Gen 3 12GB 256GB", "https://laptopfactory.com.ph/product/a/"
        ),
        hit(
            "Asus TUF Gaming B550M-Plus Wifi II (AM4) Motherboard",
            "https://laptopfactory.com.ph/product/b/",
            product_type="PC Parts & Components",
        ),
        hit(
            "LEXAR 512GB NM620 M.2 PCIe NVMe Solid State Drive",
            "https://laptopfactory.com.ph/product/c/",
            product_type="Solid State Drives (SSD)",
        ),
        hit("Acer Nitro V 15 ANV15-42-R2UH", "https://laptopfactory.com.ph/product/d/"),
    ]
    ranked = rank_target_hits(target, "laptop_factory", [record(target, *cards)])
    assert [c.category for c in ranked][0] == CODE_MATCH
    job = discover.SearchJob(target, "laptop_factory", 1)
    args = Namespace(max_candidates_per_target_retailer=1)
    manifest, inspections, variants = discover.select_and_inspect(
        ranked, job, None, None, args, {"product_fetches": 0}, None, print
    )
    assert [(c.hit.title, url) for c, url, _ in manifest] == [
        ("Acer Nitro V 15 ANV15-42-R2UH", "https://laptopfactory.com.ph/product/d/")
    ]
    decisions = {c.hit.title.split()[0]: c.decision for c in ranked}
    assert decisions["Lenovo"] == "REJECTED_PRODUCT_TYPE"
    assert decisions["Asus"] == "REJECTED_PRODUCT_TYPE"
    assert decisions["LEXAR"] == "REJECTED_PRODUCT_TYPE"
    assert inspections == [] and variants == []
    from laptop_tracker.product_discovery import plausible_candidate

    only_rejected = [record(target, *cards[:3])]
    assert plausible_candidate(target, only_rejected) == ""


def saved_candidates() -> list[dict[str, str]]:
    found = []
    for path in sorted(
        (ROOT / "data" / "output" / "weekly_runs").glob("*/discovery/candidates.csv")
    ):
        with path.open(newline="", encoding="utf-8") as handle:
            found += list(csv.DictReader(handle))
    return found


def test_saved_runs_no_page_that_was_collected_and_confirmed_is_now_rejected() -> None:
    """Regression on the tracker's own saved discovery: the filter rejects every non-laptop it
    rejected before and more, and rejects no candidate that earlier runs collected for a laptop
    except the two known non-laptops (a motherboard and a Lenovo Yoga Tab)."""
    candidates = saved_candidates()
    if not candidates:
        pytest.skip("no saved discovery runs in this checkout")
    now_rejected = []
    for row in candidates:
        if row["category"] in {"HISTORICAL_VERIFIED_URL", "SUPPLIED_URL"}:
            continue
        reason = non_laptop_reason(row["title"], row["product_type"], row["product_url"]) or (
            accessory_reason(row["title"], row["product_type"])
        )
        if row["category"] in {"NOT_A_LAPTOP", "ACCESSORY"}:
            assert reason, row["title"]  # an earlier rejection is never lost
        elif reason and row["decision"].startswith("COLLECT"):
            now_rejected.append(row["title"])
    assert set(now_rejected) <= {
        "Lenovo Yoga Tab ZAG60109PH Gen 3 12GB 256GB",
        "Asus TUF Gaming B550M-Plus Wifi II (AM4) Motherboard",
    }
