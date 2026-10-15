"""Offline checks for owner-reviewed listing equivalence before the fresh pilot."""
from __future__ import annotations

import csv
from dataclasses import replace

import pytest
from scraping_fakes import no_network  # noqa: F401
from test_extraction_layouts import OCT_ROWS, TARGET, offer, primary

from laptop_tracker import evidence_approvals
from laptop_tracker.extraction import ExtractionStatus, extract_offer_attributes
from laptop_tracker.matching import ExactMatcher, MatchStatus
from laptop_tracker.price_tracking import matching_rules_sha256

pytestmark = pytest.mark.usefixtures("no_network")
DBZ_URL = "https://shop.example.test/products/aspire?variant=1001"
OCT_URL = "https://shop.example.test/products/nitro?variant=1002"


@pytest.fixture
def approvals(tmp_path, monkeypatch):
    rows = [dict(row) for row in evidence_approvals.read_approvals()]
    for row in rows:
        row["offer_url"] = DBZ_URL if row["retailer_id"] == "datablitz" else OCT_URL
    path = tmp_path / "approvals.csv"
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=evidence_approvals.APPROVAL_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    monkeypatch.setattr(evidence_approvals, "APPROVALS_PATH", path)
    return path


def aspire(**changes):
    specs = {
        "Model": "A14-11M-X3BP OPI", "Size": '14"', "Processor": "Snapdragon X",
        "Memory": "16GB", "Storage": "512GB SSD", "Part Number": "NX.JP3SP.001",
    }
    specs.update(changes)
    return offer("datablitz", tuple(specs.items()), url=DBZ_URL)


def nitro(rows=OCT_ROWS, **kwargs):
    return offer(
        "oct", rows, title="Acer ANV15-42-R2UH OPI Ryzen 5 7640HS laptop",
        url=OCT_URL, **kwargs,
    )


def test_approved_aspire_matches_and_retains_original_model(approvals):
    page = aspire()
    before = page.to_full_record()
    target = TARGET | {"model": "A14-11M-X3BP", "cpu_series": "Snapdragon X"}
    result = ExactMatcher([target]).match_observation(page)
    assert result.status is MatchStatus.EXACT
    assert "OWNER_APPROVED_MODEL" in result.reason_codes
    kept = [e for e in result.extraction_evidence if e.field == "model"]
    assert any(e.value == "A14-11M-X3BP OPI" for e in kept)
    assert any("OWNER_APPROVED" in e.source_label for e in kept)
    assert page.to_full_record() == before


@pytest.mark.parametrize("change", [
    {"Processor": "Core i5"}, {"Size": '15.6"'}, {"Memory": "8GB"},
    {"Part Number": "NX.OTHER.001"}, {"Part Number": ""},
    {"Model": "A14-11M-OTHER OPI"},
])
def test_aspire_requires_the_observed_configuration_and_part(approvals, change):
    assert primary(aspire(**change))["model"][0] != "EXTRACTED"


@pytest.mark.parametrize("retailer,url", [
    ("datablitz", DBZ_URL.replace("1001", "1003")),
    ("datablitz", DBZ_URL.split("?")[0]),
    ("pcx", DBZ_URL),
])
def test_approval_never_spreads_to_other_urls_variants_or_retailers(approvals, retailer, url):
    page = replace(aspire(), retailer_id=retailer, offer_url=url)
    extracted = extract_offer_attributes(page)
    assert "OWNER_APPROVED_MODEL" not in extracted.notes


def test_multiple_model_rows_are_not_overridden(approvals):
    page = aspire()
    details = replace(page.details, raw_specifications=(
        *page.details.raw_specifications, ("Model", "ANV15-42-R2UH"),
    ))
    assert primary(replace(page, details=details))["model"][0] != "EXTRACTED"


def test_owner_unit_and_model_approval_preserve_processor_conflict(approvals):
    page = nitro()
    target = TARGET | {
        "model": "ANV15-42-R2UH", "screen_size_in": "15.6", "cpu_series": "Ryzen 5",
    }
    result = ExactMatcher([target]).match_observation(page)
    assert result.status is MatchStatus.EXACT
    assert result.offer_attributes["storage_capacity_gb"] == "512"
    assert {
        "OWNER_APPROVED_MODEL", "OWNER_APPROVED_STORAGE_CAPACITY_GB",
        "CPU_NUMBER_TITLE_DIFFERS", "PAGE_STORAGE_UNIT_MISSING_OWNER_APPROVED_GB",
    } <= set(result.reason_codes)
    storage = [e for e in result.extraction_evidence if e.field == "storage_capacity_gb"]
    assert any(e.status is ExtractionStatus.SUPPORTING and e.value == "512" for e in storage)
    assert any("16 Gb/s" in e.source_text for e in storage)


@pytest.mark.parametrize("storage", [
    "2x 512 PCIe Gen4, 16 Gb/s, NVMe", "1x 256 PCIe Gen4, 16 Gb/s, NVMe",
    "1x 512 + 1x 512 PCIe Gen4", "512GB SSD + 512GB HDD", "16 Gb/s NVMe",
])
def test_unit_approval_cannot_resolve_other_amounts_or_ambiguous_drives(approvals, storage):
    rows = tuple(
        (storage, "") if "1x 512" in label else (label, value) for label, value in OCT_ROWS
    )
    extracted = extract_offer_attributes(nitro(rows))
    assert "OWNER_APPROVED_STORAGE_CAPACITY_GB" not in extracted.notes


def test_explicit_different_storage_is_not_overwritten(approvals):
    rows = tuple(
        ("256GB SSD", "") if "1x 512" in label else (label, value)
        for label, value in OCT_ROWS
    )
    assert primary(nitro(rows))["storage_capacity_gb"] == ("EXTRACTED", "256")


def test_approval_policy_bytes_change_the_matching_context_fingerprint(approvals):
    before = matching_rules_sha256()
    approvals.write_text(approvals.read_text() + "\n")
    assert matching_rules_sha256() != before
