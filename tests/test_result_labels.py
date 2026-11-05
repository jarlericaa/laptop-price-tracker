"""Short result labels of the Sheets dashboard (2026-11-05): what each label may and may not say.

The labels are display only. These tests pin the semantics the owner required: a label names a
structured result, keeps the material qualifiers (stock, listed specs, limited basis, limited
search), never turns an unknown into an agreement and never makes an unresolved or excluded result
look like a comparable price.
"""

from __future__ import annotations

import pytest

from laptop_tracker.pilot_coverage import DiscoveryLogs
from laptop_tracker.recheck import _DECISION_TEXT
from laptop_tracker.result_labels import (
    NO_RESULT_CAVEAT,
    TAG_MEANING,
    compact_specs,
    coverage_summary,
    retailer_result,
    search_limited,
    shopee_freshness,
)

CAPPED = (
    "NO_RELEVANT_CANDIDATE: 3 search page(s) over 3 query tier(s) returned 20 result(s), none "
    "showing this configuration's model code: UNRELATED x20; 2 result page(s) at the retailer's "
    "result cap or with unread next pages; the query plan ran, but the retailer's catalog was "
    "not exhaustively searched; discovery ended: QUERY_PLAN_EXHAUSTED"
)


def make(**fields: str) -> dict[str, str]:
    """A comparison row with one retailer (DBZ) filled from short names."""
    row = {"context_id": "CTX-1", "row_comparison_status": "MATCHING_PRICES_FOUND"}
    names = {
        "price": "Price",
        "status": "Price_Status",
        "tag": "Comparison_Tag",
        "stock": "Stock",
        "basis": "Match_Basis",
        "reason": "Reason",
        "candidate": "Candidate_Status",
        "note": "Recheck_Note",
        "unknowns": "Alternative_Unknowns",
    }
    for short, value in fields.items():
        row[f"DBZ_{names[short]}"] = value
    return row


def label(row: dict[str, str], **kwargs) -> str:
    return retailer_result(row, "DBZ", **kwargs).label


def test_a_matching_offer_and_its_qualifiers() -> None:
    base = {"price": "999.00", "status": "SELECTED", "tag": "MATCH", "stock": "IN_STOCK"}
    assert label(make(**base)) == "MATCH"
    assert label(make(**base | {"basis": "LISTED_SPEC"})) == "MATCH · Listed specs"
    assert label(make(**base | {"stock": "OUT_OF_STOCK"})) == "MATCH · Out of stock"
    # Two qualifiers use two short lines instead of dropping one.
    both = make(**base | {"basis": "LISTED_SPEC", "stock": "OUT_OF_STOCK"})
    assert label(both) == "MATCH · Listed specs\nOut of stock"
    result = retailer_result(make(**base), "DBZ")
    assert (result.kind, result.fill, result.priced) == ("MATCH", "match", True)


@pytest.mark.parametrize(
    ("tag", "fill"),
    [("ALT 1 · CPU", "alt1"), ("ALT 2 · CPU, Storage", "alt2")],
)
def test_alternatives_keep_their_difference_tag_and_are_not_matches(tag: str, fill: str) -> None:
    row = make(price="500.00", status="ALTERNATIVE_SELECTED", tag=tag, stock="IN_STOCK")
    result = retailer_result(row, "DBZ")
    assert result.label == tag and result.fill == fill and result.kind == "ALT"
    assert not result.label.startswith("MATCH")


def test_limited_comparison_basis_comes_from_the_alternative_not_from_unknown_fields() -> None:
    row = make(price="500.00", status="ALTERNATIVE_SELECTED", tag="ALT 2 · CPU, Storage")
    limited = {"comparison_basis": "model FAMILY_ONLY"}
    assert label(row, alt=limited) == "ALT 2 · CPU, Storage · Limited"
    # Unknown fields are listed in details and notes; they never become a difference or a match.
    unknown_gpu = make(
        price="500.00",
        status="ALTERNATIVE_SELECTED",
        tag="ALT 1 · CPU",
        unknowns="GPU: GPU not stated by the target or the retailer",
    )
    assert label(unknown_gpu, alt={"comparison_basis": ""}) == "ALT 1 · CPU"
    # A report row without the alternative record still says Limited from its own words.
    words = make(
        price="500.00",
        status="ALTERNATIVE_SELECTED",
        tag="ALT 1 · CPU",
        unknowns="GPU: not stated; limited basis: model FAMILY_ONLY",
    )
    assert label(words) == "ALT 1 · CPU · Limited"


def test_an_out_of_stock_alternative_uses_a_second_line() -> None:
    row = make(
        price="500.00",
        status="ALTERNATIVE_SELECTED",
        tag="ALT 2 · Screen, CPU",
        stock="OUT_OF_STOCK",
    )
    assert label(row, alt={"comparison_basis": "CPU SERIES_ONLY"}) == (
        "ALT 2 · Screen, CPU · Limited\nOut of stock"
    )


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        (
            {"status": "CANDIDATE_NOT_FETCHED", "candidate": "CANDIDATE_NOT_FETCHED"}
            | {"note": _DECISION_TEXT["REVIEW_SEVERAL_MATCHING_VARIANTS"]},
            "RECHECK · Variant ambiguous",
        ),
        (
            {"status": "CANDIDATE_NOT_CONFIRMED", "candidate": "EXTRACTION_INCOMPLETE"},
            "RECHECK · Specs incomplete",
        ),
        (
            {"status": "CANDIDATE_NOT_CONFIRMED", "candidate": "IDENTITY_CONFLICT"},
            "RECHECK · Spec conflict",
        ),
        (
            {"status": "CANDIDATE_NOT_FETCHED", "candidate": "CANDIDATE_NOT_FETCHED"}
            | {"note": _DECISION_TEXT["NOT_COLLECTED_BUDGET"]},
            "PENDING · Not fetched",
        ),
        (
            {"status": "FETCH_FAILED", "reason": "FETCH_FAILED: https://x (RATE_LIMITED)"},
            "DEFERRED · Rate limited",
        ),
        (
            {"status": "FETCH_FAILED", "reason": "FETCH_FAILED: https://x (TIMEOUT)"},
            "PENDING · Fetch failed",
        ),
        ({"status": "DISCOVERY_NOT_ATTEMPTED"}, "PENDING · Not searched"),
    ],
)
def test_unresolved_candidates_are_recheck_pending_or_deferred_never_prices(
    fields: dict[str, str], expected: str
) -> None:
    result = retailer_result(make(**fields), "DBZ")
    assert result.one_line == expected
    assert not result.priced and not result.label.startswith(("MATCH", "ALT"))


def test_every_decision_that_keeps_a_variant_page_apart_has_its_own_recheck_label() -> None:
    wanted = {
        "REVIEW_SEVERAL_MATCHING_VARIANTS": "RECHECK · Variant ambiguous",
        "REVIEW_NO_MATCHING_VARIANT": "RECHECK · No matching variant",
        "REVIEW_VARIANTS_UNREADABLE": "RECHECK · Variants unreadable",
        "REVIEW_MULTI_VARIANT_NOT_SUPPORTED": "RECHECK · Variants unreadable",
        "REVIEW_DIFFERENT_CONFIGURATION_BY_TITLE": "RECHECK · Different config",
        "NOT_SELECTED_LIMIT": "PENDING · Not fetched",
        "NOT_INSPECTED_BUDGET": "PENDING · Not fetched",
        "NOT_SENT_REPLAY_ONLY": "PENDING · Not fetched",
    }
    for decision, expected in wanted.items():
        row = make(
            status="CANDIDATE_NOT_FETCHED",
            candidate="CANDIDATE_NOT_FETCHED",
            note=_DECISION_TEXT[decision],
        )
        assert retailer_result(row, "DBZ").one_line == expected, decision


def test_no_results_and_no_relevant_never_say_the_retailer_does_not_sell_it() -> None:
    none = make(
        status="NO_CANDIDATE_FOUND",
        reason="NO_CANDIDATE_FOUND: 2 search page(s) over 2 "
        "query tier(s) returned no results; discovery ended: QUERY_PLAN_EXHAUSTED",
    )
    assert label(none) == "NO RESULTS"
    capped = make(status="NO_RELEVANT_CANDIDATE", reason=CAPPED)
    assert label(capped) == "NO RELEVANT · Search limited"
    plain = make(
        status="NO_RELEVANT_CANDIDATE",
        reason="NO_RELEVANT_CANDIDATE: 2 search page(s) returned 3 result(s), none showing "
        "this configuration's model code: ACCESSORY x3; discovery ended: QUERY_PLAN_EXHAUSTED",
    )
    assert label(plain) == "NO RELEVANT"
    assert label(make(status="NO_CANDIDATE_FOUND", reason=CAPPED)) == "NO RESULTS · Search limited"
    stopped = make(status="DISCOVERY_INCOMPLETE")
    assert label(stopped) == "INCOMPLETE · Search limited"
    for tag in ("NO RESULTS", "NO RELEVANT", "INCOMPLETE", "SEARCH FAILED"):
        meaning = TAG_MEANING[tag]
        assert "does not mean the retailer does not sell" in meaning or tag == "SEARCH FAILED"
        assert "exhaustive" in meaning
    assert "exhaustive" in NO_RESULT_CAVEAT


def test_search_limited_reads_the_real_discovery_notes() -> None:
    """The qualifier follows the wording ``DiscoveryLogs.stage`` records, so a reworded note
    cannot silently turn a capped search into a complete one."""
    key = ("TGT-1", "pcx")

    def notes(truncated: str) -> str:
        logs = DiscoveryLogs(
            search_plan=[{"target_id": "TGT-1", "retailer_id": "pcx"}],
            search_requests=[
                {
                    "target_id": "TGT-1",
                    "retailer_id": "pcx",
                    "outcome": "RESULTS",
                    "tier": "1_MODEL",
                    "possibly_truncated": truncated,
                }
            ],
            candidates=[{"target_id": "TGT-1", "retailer_id": "pcx", "category": "UNRELATED"}],
            search_jobs=[
                {
                    "target_id": "TGT-1",
                    "retailer_id": "pcx",
                    "discovery_outcome": "QUERY_PLAN_EXHAUSTED",
                }
            ],
        )
        status, found = logs.stage(key)
        assert status == "NO_RELEVANT_CANDIDATE"
        return "; ".join(found)

    assert search_limited(notes("TRUE")) is True
    assert search_limited(notes("")) is False
    assert search_limited("discovery ended: SEARCH_INCOMPLETE_RESULT_CAP") is True
    assert search_limited("discovery ended: BUDGET_EXHAUSTED") is True
    assert search_limited("search coverage INCOMPLETE_RESULT_CAP") is True
    assert (
        search_limited("search coverage COMPLETE; discovery ended: QUERY_PLAN_EXHAUSTED") is False
    )


def test_a_matching_offer_excluded_by_its_terms_is_excluded_with_no_price() -> None:
    row = make(status="NO_COMPARABLE_OFFER")
    selection = {"not_comparable_reasons": "PURCHASE_TERMS_UNKNOWN:1"}
    result = retailer_result(row, "DBZ", selection)
    assert result.one_line == "EXCLUDED · Bundle terms" and not result.priced
    assert result.fill == "review"
    mixed = {"not_comparable_reasons": "BUNDLE_OFFER:1;RESTRICTED_PRICE:1;AVAILABILITY_PREORDER:1"}
    assert retailer_result(row, "DBZ", mixed).one_line == (
        "EXCLUDED · Bundle terms, Restricted price +1"
    )


def test_unresolved_rows_without_a_context_are_not_compared() -> None:
    row = {"context_id": "", "row_comparison_status": "CONFIGURATION_REVIEW_PENDING"}
    assert retailer_result(row, "DBZ").one_line == "NOT COMPARED · Config review"


def test_results_without_a_price_use_neutral_or_review_fills_never_comparison_fills() -> None:
    for status in ("NO_CANDIDATE_FOUND", "NO_RELEVANT_CANDIDATE", "DISCOVERY_NOT_ATTEMPTED"):
        assert retailer_result(make(status=status), "DBZ").fill == "missing"
    assert retailer_result(make(status="NO_COMPARABLE_OFFER"), "DBZ").fill == "review"
    review = make(status="CANDIDATE_NOT_CONFIRMED", candidate="DIFFERENT_MODEL")
    assert retailer_result(review, "DBZ").fill == "review"


# Shopee snapshot freshness


def shopee(**fields: str) -> dict[str, str]:
    return {
        "shopee_price_php": "1000.00",
        "shopee_price_observed_at": "2026-11-05",
        "shopee_price_date_basis": "SALES_EXTRACT_DATE",
        "shopee_price_status": "SNAPSHOT_PRICE",
    } | fields


def test_the_snapshot_shows_its_source_date_and_what_kind_of_date_it_is() -> None:
    assert shopee_freshness(shopee()) == ("2026-11-05 · sales extract", False)
    monthly = shopee(
        shopee_price_observed_at="2026-10-01",
        shopee_price_date_basis="M_MINUS_1_RECORD_DATE",
        shopee_price_status="SNAPSHOT_PRICE_STALE",
    )
    text, stale = shopee_freshness(monthly)
    assert stale and text == "2026-10-01 · monthly record\nSTALE"  # a record date, not a time
    assert shopee_freshness(shopee(shopee_price_php="")) == ("", False)


# Compact specifications and coverage


def test_compact_specs_use_recorded_fields_only() -> None:
    row = {
        "screen_size_in": "15.6",
        "cpu_series": "Ryzen 5",
        "cpu_model": "",
        "ram_capacity_gb": "16",
        "storage_capacity_gb": "1000",
        "gpu_model": "",
    }
    assert compact_specs(row) == '15.6" · Ryzen 5 · 16GB · 1TB'
    assert compact_specs(row | {"cpu_model": "Ryzen 5 7520U", "gpu_model": "RTX 3050"}) == (
        '15.6" · Ryzen 5 7520U · 16GB · 1TB · RTX 3050'
    )
    assert compact_specs({}) == ""


def test_row_coverage_counts_each_kind_once() -> None:
    results = [
        retailer_result(make(price="1.00", status="SELECTED", tag="MATCH"), "DBZ"),
        retailer_result(make(price="1.00", status="SELECTED", tag="MATCH"), "DBZ"),
        retailer_result(
            make(price="1.00", status="ALTERNATIVE_SELECTED", tag="ALT 1 · CPU"), "DBZ"
        ),
        retailer_result(make(status="NO_RELEVANT_CANDIDATE"), "DBZ"),
        retailer_result(make(status="NO_COMPARABLE_OFFER"), "DBZ"),
    ]
    assert coverage_summary(results) == "2 match · 1 alt · 1 excluded · 1 no price"
