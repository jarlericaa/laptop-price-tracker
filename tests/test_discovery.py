"""Offline candidate discovery: synthetic observations only; no requests or searches."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from scraping_fakes import no_network  # noqa: F401
from test_extraction_layouts import DBZ_ROWS, offer
from test_verified_top import load_script

from laptop_tracker.discovery import (
    COLLECTED,
    DIFFERENT,
    LABELED_MODEL,
    LABELED_WITH_SUFFIX,
    NEEDS_VERIFICATION,
    NOT_FETCHED,
    POSSIBLE,
    STRONG,
    TITLE_CORROBORATED,
    TITLE_ONLY,
    URL_ONLY,
    Reference,
    assess,
    build_candidates,
    normalize,
    plan_queries,
    rank_candidates,
)
from laptop_tracker.matching import ExactMatcher
from laptop_tracker.offers import ENUMERATED_VARIANT, IDENTIFIER, SELECTED_VARIANT

pytestmark = pytest.mark.usefixtures("no_network")
discover_candidates = load_script("discover_candidates")

CORE = {
    "model": "A14-11M-X3BP",
    "screen_size_in": "14",
    "cpu_series": "Snapdragon X",
    "ram_capacity_gb": "16",
    "storage_capacity_gb": "512",
}
REFERENCE = Reference("TGT-000001", "1", "Acer", "Aspire", "A14-11M-X3BP", CORE)
DBZ = "https://ecommerce.datablitz.com.ph/products"


def candidate(observation):
    candidates = build_candidates([(observation, "saved.csv")])
    [item] = [c for c in candidates if c.url == observation.offer_url]
    return item


def dbz(model: str, *, title: str = "Acer Aspire 14 AI Laptop", url: str = f"{DBZ}/x", **kwargs):
    rows = (("Size", '14"'), *((("Model", model),) if model else ()), *DBZ_ROWS)
    return offer("datablitz", rows, title=title, url=url, **kwargs)


def test_normalize_handles_case_whitespace_and_trademarks() -> None:
    assert normalize("  Acer®  Aspire™\tA14-11M-X3BP ") == "acer aspire a14-11m-x3bp"


def test_labeled_model_with_agreeing_fields_is_a_strong_candidate() -> None:
    item = assess(REFERENCE, candidate(dbz("A14-11M-X3BP")))
    assert (item.category, item.provenance, item.suffix) == (STRONG, LABELED_MODEL, "")
    assert all(value == "AGREE" for value in item.fields.values())


def test_unexplained_suffix_is_retained_and_needs_verification() -> None:
    item = assess(REFERENCE, candidate(dbz("A14-11M-X3BP OPI")))
    assert (item.category, item.provenance, item.suffix) == (
        NEEDS_VERIFICATION,
        LABELED_WITH_SUFFIX,
        "OPI",
    )
    assert item.fields["model"] == "UNKNOWN (UNRESOLVED: A14-11M-X3BP OPI)"
    assert "'OPI'" in item.next_step


def test_a_corroborated_title_code_is_labeled_as_such() -> None:
    # The other four fields come from labeled rows, so the extractor accepts the title code
    # (balanced policy, 2026-10-15); discovery reports that provenance, not LABELED_MODEL.
    titled = assess(REFERENCE, candidate(dbz("", title="Acer Aspire A14-11M-X3BP Laptop")))
    assert (titled.category, titled.provenance) == (STRONG, TITLE_CORROBORATED)
    assert titled.fields["model"] == "AGREE"


def test_uncorroborated_title_and_url_codes_are_discovery_tokens_only() -> None:
    bare = offer("datablitz", (("Size", '14"'),), title="Acer Aspire A14-11M-X3BP Laptop",
                 url=f"{DBZ}/x")
    titled = assess(REFERENCE, candidate(bare))
    assert (titled.category, titled.provenance) == (NEEDS_VERIFICATION, TITLE_ONLY)
    assert titled.fields["model"] == "UNKNOWN (MISSING)"
    slug = assess(REFERENCE, candidate(dbz("", title="Laptop", url=f"{DBZ}/acer-a14-11m-x3bp")))
    assert (slug.category, slug.provenance) == (NEEDS_VERIFICATION, URL_ONLY)


def test_family_words_without_a_code_are_a_possible_candidate() -> None:
    item = assess(REFERENCE, candidate(dbz("", title="Acer Aspire 14 AI Laptop")))
    assert (item.category, item.matched_code) == (POSSIBLE, "")


def test_same_code_with_different_specifications_is_a_different_configuration() -> None:
    rows = (("Model", "A14-11M-X3BP"), ("Memory", "8 GB LPDDR5X"), ("Storage", "512 GB SSD"))
    item = assess(REFERENCE, candidate(offer("datablitz", rows)))
    assert item.category == DIFFERENT
    assert item.fields["ram_capacity_gb"] == "DIFFER (8)"


def test_unrelated_pages_are_not_candidates() -> None:
    assert assess(REFERENCE, candidate(dbz("ZZ15-00-0000", title="Other Brand Laptop"))) is None


def test_supporting_prose_never_counts_as_agreement() -> None:
    from laptop_tracker.offers import PRODUCT_DESCRIPTION

    def page(sentence: str):
        return offer(
            "datablitz",
            (("Model", "A14-11M-X3BP"), ("Size", "14.0"), *DBZ_ROWS),
            scoped=((PRODUCT_DESCRIPTION, "description paragraph", sentence),),
        )

    # Accessory wording stays supporting only and never counts as agreement.
    item = assess(REFERENCE, candidate(page("The sleeve fits 14-inch laptops.")))
    assert item.fields["screen_size_in"] == "UNKNOWN (UNPARSEABLE); supporting only: agrees"
    assert "screen_size_in" not in [n for n, v in item.fields.items() if v.startswith("AGREE")]
    # Explicit product prose is a trusted value under the owner policy (2026-10-13).
    product = assess(REFERENCE, candidate(page("A 14-inch display.")))
    assert product.fields["screen_size_in"] == "AGREE"


def test_discovery_never_changes_matching() -> None:
    page = dbz("A14-11M-X3BP OPI")
    matcher = ExactMatcher([{"target_id": "TGT-000001", "status": "ACTIVE", **CORE}])
    before = matcher.match_observation(page)
    rank_candidates([REFERENCE], build_candidates([(page, "saved.csv")]))
    after = matcher.match_observation(page)
    assert (before.status, before.target_id, before.reason_codes) == (
        after.status,
        after.target_id,
        after.reason_codes,
    )


# Variants


PCC = "https://pccentralcomputerstore.com/products/example-thin-15"
PCC_ROWS = (("Example Thin 15 B13UC-0000PH", ""), ("Memory", ""), ("8GB DDR4", ""))
PCC_REF = Reference(
    "TGT-000002",
    "2",
    "MSI",
    "Thin 15",
    "B13UC-0000PH",
    {
        "model": "B13UC-0000PH",
        "screen_size_in": "15.6",
        "cpu_series": "Core i5",
        "ram_capacity_gb": "16",
        "storage_capacity_gb": "512",
    },
)


def pcc(variant: str, enumerated: tuple[tuple[str, str], ...]):
    return offer(
        "pcc",
        PCC_ROWS,
        title="Example Thin 15 B13UC-0000PH | 8GB",
        configuration="16GB / 512GB | Example Thin 15 B13UC-0000PH | Memory | 8GB DDR4",
        url=f"{PCC}?variant={variant}",
        scoped=(
            (SELECTED_VARIANT, "binding status", "CONFIRMED"),
            (SELECTED_VARIANT, "variant id", variant),
            (SELECTED_VARIANT, "RAM", "16GB (2x8) DDR4 RAM"),
            (SELECTED_VARIANT, "SSD", "512GB SDD"),
            *((ENUMERATED_VARIANT, title, url) for title, url in enumerated),
        ),
    )


def test_enumerated_variants_are_discovered_not_fetched_and_never_invented() -> None:
    page = pcc("2", (("8GB DDR4 RAM / 512GB SDD", f"{PCC}?variant=1"),))
    candidates = build_candidates([(page, "saved.csv")])
    assert [(c.collection_status, c.variant_id) for c in candidates] == [
        (COLLECTED, "2"),
        (NOT_FETCHED, "1"),
    ]
    other = assess(PCC_REF, candidates[1])
    assert other.category == DIFFERENT
    assert other.fields["ram_capacity_gb"] == "DIFFER (variant option 8)"
    assert other.fields["cpu_series"] == "UNKNOWN (variant not collected)"
    assert "Collect this variant's own page" in other.next_step
    selected = assess(PCC_REF, candidates[0])
    assert selected.fields["ram_capacity_gb"] == "AGREE"
    assert selected.category == NEEDS_VERIFICATION  # Title-only code.


def test_a_collected_variant_is_not_listed_again() -> None:
    first = pcc("2", (("8GB DDR4 RAM / 512GB SDD", f"{PCC}?variant=1"),))
    second = pcc("1", (("16GB (2x8) DDR4 RAM / 512GB SDD", f"{PCC}?variant=2"),))
    statuses = sorted(
        (c.variant_id, c.collection_status)
        for c in build_candidates([(first, "a.csv"), (second, "b.csv")])
    )
    assert statuses == [("1", COLLECTED), ("2", COLLECTED)]


# Query planning


def test_query_tiers_are_ordered_and_unverified_identifiers_are_marked() -> None:
    page = dbz(
        "A14-11M-X3BP OPI",
        scoped=(
            (IDENTIFIER, "JSON-LD Product.sku", "4711111111111"),
            (IDENTIFIER, "JSON-LD Product.mpn", "4711111111111"),
        ),
    )
    page = offer(
        "datablitz",
        (("Model", "A14-11M-X3BP OPI"), ("Part Number", "NX.EXAMPLE.001"), *DBZ_ROWS),
        title="Acer Aspire A14 11M X3BP Laptop",
        scoped=page.details.scoped_evidence,
    )
    reference = Reference(
        REFERENCE.target_id, "1", "Acer", "Aspire", "A14-11M-X3BP", CORE, "NX.VERIFIED.001"
    )
    assessments = rank_candidates([reference], build_candidates([(page, "saved.csv")]))
    rows = plan_queries([reference], assessments, {"datablitz": "ecommerce.datablitz.com.ph"})
    plan = [(r["tier"], r["verified_identifier"], r["query"]) for r in rows]
    host = "site:ecommerce.datablitz.com.ph"
    assert plan == [
        ("A_COMPLETE_MODEL_CODE", "TRUE", f'{host} "A14-11M-X3BP"'),
        ("B_FORMATTING_VARIATION", "TRUE", f'{host} "A14 11M X3BP"'),
        ("B_FORMATTING_VARIATION", "FALSE", f'{host} "A14-11M-X3BP OPI"'),
        ("C_VERIFIED_MANUFACTURER_PART_NUMBER", "TRUE", f'{host} "NX.VERIFIED.001"'),
        ("C_SUPPLEMENTARY_UNVERIFIED", "FALSE", f'{host} "NX.EXAMPLE.001"'),
        ("D_BRAND_FAMILY_SPECS", "TRUE", f'{host} Acer "Aspire" 14 "Snapdragon X" 16GB 512GB'),
    ]
    assert not any("4711111111111" in r["query"] for r in rows)  # Retailer SKU/barcode.


# Command


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_command_writes_reports_into_a_new_folder(tmp_path: Path, capsys) -> None:
    from laptop_tracker.offers import OBSERVATION_COLUMNS, OFFER_DETAIL_COLUMNS

    references = tmp_path / "references.csv"
    write_csv(references, [{"rank": "1", "target_id": "TGT-000001", "brand": "Acer",
                            "series": "Aspire", **CORE}])
    observations = tmp_path / "observations.csv"
    record = dbz("A14-11M-X3BP OPI").to_full_record()
    write_csv(observations, [{c: record[c] for c in (*OBSERVATION_COLUMNS, *OFFER_DETAIL_COLUMNS)}])
    out = tmp_path / "discovery"
    args = ["--references", str(references), "--observations", str(observations),
            "--retailers", "datablitz", "--output-dir", str(out)]
    assert discover_candidates.main(args) == 0
    [row] = list(csv.DictReader((out / "discovery_candidates.csv").open(encoding="utf-8")))
    assert (row["category"], row["retained_suffix"]) == (NEEDS_VERIFICATION, "OPI")
    summary = json.loads((out / "discovery_summary.json").read_text(encoding="utf-8"))
    assert summary["categories"] == {NEEDS_VERIFICATION: 1}
    assert (out / "discovery_queries.csv").is_file()
    assert discover_candidates.main(args) == 1  # Never writes into an existing folder.
    assert "already exists" in capsys.readouterr().err


def test_command_rejects_unknown_targets_and_retailers(tmp_path: Path) -> None:
    references = tmp_path / "references.csv"
    write_csv(references, [{"rank": "1", "target_id": "TGT-000001", "brand": "Acer",
                            "series": "Aspire", **CORE}])
    base = ["--references", str(references)]
    assert discover_candidates.main(
        [*base, "--targets", "TGT-999999", "--output-dir", str(tmp_path / "a")]
    ) == 1
    assert discover_candidates.main(
        [*base, "--retailers", "example_shop", "--output-dir", str(tmp_path / "b")]
    ) == 1
    assert not (tmp_path / "b").exists()
