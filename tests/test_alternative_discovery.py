"""Discovery before final matching, under the close-alternative policy (2026-10-27).

A search title that states one or two differences from the target is retained and inspected (the
page's own evidence decides); more than two are not fetched; a field the title does not state is
unknown, never agreement. Synthetic fixtures only; nothing is fetched.
"""

from __future__ import annotations

import dataclasses
from argparse import Namespace

import pytest
from scraping_fakes import FakeClock, no_network  # noqa: F401
from test_extraction import FIXTURE_PAGES, FIXTURES
from test_product_discovery import (
    NITRO,
    PCC,
    Routes,
    discover,
    hit,
    html,
    record,
)

from laptop_tracker.adapters.pcc import HOST as PCC_HOST
from laptop_tracker.alternatives import MAX_DIFFERENCES
from laptop_tracker.product_discovery import (
    CANDIDATE_COLUMNS,
    DIFFERENT_MODEL,
    FAMILY_SPEC_CANDIDATE,
    SearchTarget,
    VariantInfo,
    candidate_row,
    rank_target_hits,
    title_alternative_potential,
    title_alternative_potential_from_row,
    variant_evaluation,
)
from laptop_tracker.retailer_search import SEARCH_METHODS, BudgetedTransport, SearchHit
from laptop_tracker.scraping import FetchConfig, Fetcher

pytestmark = pytest.mark.usefixtures("no_network")

ASPIRE = SearchTarget(
    "TGT-000900", "5", "Acer", "", "Aspire Lite", "",
    {"model": "Aspire Lite", "screen_size_in": "15.6", "cpu_series": "Ryzen 5",
     "ram_capacity_gb": "8", "storage_capacity_gb": "512"},
    identity_kind="FAMILY_NAME", record={"brand": "Acer", "model": "Aspire Lite"},
)
BASE = "Acer Aspire Lite 15 15.6 Ryzen 5"
NITRO_CODE = "ANV15-42-R2UH"


def url(handle: str) -> str:
    return f"https://{SEARCH_METHODS['laptop_factory'].host}/product/{handle}/"


def run(titles: list[tuple[str, str]], target: SearchTarget = NITRO, tmp_path=None, **overrides):
    """Rank ``(handle, title)`` results for ``target`` at Laptop Factory and decide them."""
    job = discover.SearchJob(target, "laptop_factory", 1)
    found = [hit(title, url(handle)) for handle, title in titles]
    ranked = rank_target_hits(target, "laptop_factory", [record(target, *found)])
    args = Namespace(max_candidates_per_target_retailer=5, max_product_fetches=5, **overrides)
    manifest, inspections, variants = discover.select_and_inspect(
        ranked, job, None, None, args, {"product_fetches": 0}, tmp_path, lambda _: None)
    return {c.hit.product_url.rsplit("/", 2)[-2]: c for c in ranked}, manifest


# One- and two-difference titles are collected, not skipped


def test_a_code_title_with_one_or_two_differences_is_collected() -> None:
    ranked, manifest = run([
        ("one", f"Acer Nitro V 15 {NITRO_CODE} Ryzen 5 8GB RAM 512GB SSD"),
        ("two", f"Acer Nitro V 15 {NITRO_CODE} Ryzen 5 8GB RAM 256GB SSD"),
    ])
    assert ranked["one"].decision == ranked["two"].decision == "COLLECT_CHECK_CONFIGURATION"
    assert [c.target.target_id for c, _, _ in manifest] == ["TGT-000059"] * 2
    assert title_alternative_potential(ranked["one"]).tier == "ALTERNATIVE_1"
    assert title_alternative_potential(ranked["two"]).tier == "ALTERNATIVE_2"


def test_a_family_listing_with_a_ram_difference_is_now_retained() -> None:
    ranked, manifest = run([("alt", f"{BASE} 16GB RAM 512GB SSD")], ASPIRE)
    candidate = ranked["alt"]
    assert candidate.category == FAMILY_SPEC_CANDIDATE
    assert candidate.decision == "COLLECT_CLOSE_ALTERNATIVE_CANDIDATE"
    assert "ram" in candidate.decision_reason
    assert "A title never supplies a price." in candidate.decision_reason
    assert len(manifest) == 1


def test_more_than_two_established_differences_are_not_fetched() -> None:
    ranked, manifest = run([
        ("many", f"Acer Nitro V 14-inch {NITRO_CODE} Core i7 8GB RAM 256GB SSD"),
        ("family", "Acer Aspire Lite 14-inch Core i7 16GB RAM 1TB SSD"),
    ])
    assert ranked["many"].decision == "SKIPPED_TOO_MANY_TITLE_DIFFERENCES"
    assert "4 differences" in ranked["many"].decision_reason
    assert "screen, cpu, ram, storage" in ranked["many"].decision_reason
    assert f"at most {MAX_DIFFERENCES}" in ranked["many"].decision_reason
    assert manifest == [] and ranked["family"].decision != "COLLECT_CLOSE_ALTERNATIVE_CANDIDATE"


def test_a_partial_title_is_inspected_without_assuming_its_missing_fields_agree() -> None:
    ranked, manifest = run([("partial", "Acer Aspire Lite laptop")], ASPIRE)
    candidate = ranked["partial"]
    potential = title_alternative_potential(candidate)
    assert candidate.decision == "COLLECT" and len(manifest) == 1
    assert potential.differences == () and potential.agrees == ()
    assert set(potential.unknown) == {"screen", "cpu", "ram", "storage", "gpu"}
    assert potential.tier == "INCOMPLETE_TITLE"
    # The row keeps the unknown fields and no agreement.
    row = candidate_row(candidate)
    assert row["title_unknown_dimensions"] == "screen;cpu;ram;storage;gpu"
    assert row["title_difference_count"] == "0" and row["alternative_tier"] == "INCOMPLETE_TITLE"


def test_accessories_are_still_rejected_before_any_request() -> None:
    ranked, manifest = run([
        ("sleeve", f"Tomtoc Sleeve for Acer Nitro V 15 {NITRO_CODE} 16GB 512GB"),
        ("monitor", "Acer 27 inch Monitor"),
    ])
    assert ranked["sleeve"].decision == "REJECTED_ACCESSORY"
    assert ranked["monitor"].decision in {"REJECTED_PRODUCT_TYPE", "NOT_SELECTED"}
    assert manifest == []


def test_no_collect_title_differences_still_disables_difference_candidates() -> None:
    ranked, manifest = run([
        ("code", f"Acer Nitro V 15 {NITRO_CODE} Ryzen 5 8GB RAM 512GB SSD"),
        ("match", f"Acer Nitro V 15 {NITRO_CODE} Ryzen 5 16GB RAM 512GB SSD"),
    ], no_collect_title_differences=True)
    assert ranked["code"].decision == "REVIEW_DIFFERENT_CONFIGURATION_BY_TITLE"
    assert ranked["match"].decision == "COLLECT" and len(manifest) == 1
    family, manifest = run([("alt", f"{BASE} 16GB RAM 512GB SSD")], ASPIRE,
                           no_collect_title_differences=True)
    assert family["alt"].decision == "REVIEW_DIFFERENT_CONFIGURATION_BY_TITLE"
    assert manifest == []
    other, manifest = run([("model", "Acer Nitro V 15 ANV15-41-R19Z Ryzen 5 16GB 512GB")],
                          no_collect_title_differences=True)
    assert other["model"].decision == "NOT_SELECTED" and not manifest  # as before


# Model differences need the same brand and a recognised family


def test_a_same_family_different_model_is_retained_as_one_model_difference() -> None:
    ranked, manifest = run([("model", "Acer Nitro V 15 ANV15-41-R19Z Ryzen 5 16GB 512GB SSD")])
    candidate = ranked["model"]
    assert candidate.category == DIFFERENT_MODEL
    assert title_alternative_potential(candidate).differences == ("model",)
    assert candidate.decision == "COLLECT_CLOSE_ALTERNATIVE_CANDIDATE" and len(manifest) == 1


def test_a_different_model_in_another_family_or_brand_is_not_collected() -> None:
    ranked, manifest = run([
        ("swift", "Acer Swift Go 14 SFG14-75-55MB Core Ultra 5 16GB 512GB SSD"),
        ("asus", "ASUS Vivobook 15 X1504VA-NJ1527W Core i5 16GB 512GB SSD"),
    ])
    assert ranked["swift"].category == DIFFERENT_MODEL
    assert not title_alternative_potential(ranked["swift"]).model_difference
    assert ranked["swift"].decision == "NOT_SELECTED"
    assert ranked["asus"].decision in {"NOT_SELECTED", "REJECTED_ACCESSORY"}
    assert manifest == []


def test_a_model_difference_counts_toward_the_two_difference_budget() -> None:
    ranked, manifest = run([
        ("two", "Acer Nitro V 15 ANV15-41-R19Z Ryzen 5 8GB RAM 512GB SSD"),
        ("three", "Acer Nitro V 15 ANV15-41-R19Z Ryzen 5 8GB RAM 256GB SSD"),
    ])
    assert title_alternative_potential(ranked["two"]).differences == ("model", "ram")
    assert ranked["two"].decision == "COLLECT_CLOSE_ALTERNATIVE_CANDIDATE"
    assert title_alternative_potential(ranked["three"]).count == 3
    assert ranked["three"].decision == "SKIPPED_TOO_MANY_TITLE_DIFFERENCES"
    assert len(manifest) == 1


def test_a_title_gpu_difference_counts_once_and_an_unknown_gpu_never_does() -> None:
    target = dataclasses.replace(NITRO, record={"gpu_model": "RTX 5050"})
    ranked, _ = run([
        ("gpu", f"Acer Nitro V 15 {NITRO_CODE} Ryzen 5 16GB 512GB RTX 4050"),
        ("none", f"Acer Nitro V 15 {NITRO_CODE} Ryzen 5 16GB 512GB"),
        ("same", f"Acer Nitro V 15 {NITRO_CODE} Ryzen 5 16GB 512GB RTX 5050"),
    ], target)
    assert title_alternative_potential(ranked["gpu"]).differences == ("gpu",)
    assert title_alternative_potential(ranked["none"]).differences == ()
    assert "gpu" in title_alternative_potential(ranked["none"]).unknown
    assert title_alternative_potential(ranked["same"]).agrees.count("gpu") == 1
    unrecorded, _ = run([("gpu", f"Acer Nitro V 15 {NITRO_CODE} 16GB 512GB RTX 4050")])
    assert title_alternative_potential(unrecorded["gpu"]).differences == ()


# Priority and limits


def test_priority_is_match_then_alt_1_then_alt_2_then_incomplete_titles() -> None:
    ranked, _ = run([
        ("incomplete", "Acer Aspire Lite laptop"),
        ("alt2", f"{BASE} 16GB RAM 1TB SSD"),
        ("alt1", f"{BASE} 16GB RAM 512GB SSD"),
        ("match", f"{BASE} 8GB RAM 512GB SSD"),
    ], ASPIRE)
    order = sorted(ranked, key=lambda handle: ranked[handle].rank)
    assert order == ["match", "alt1", "alt2", "incomplete"]
    assert [title_alternative_potential(ranked[h]).tier for h in order] == [
        "MATCH_CONFIGURATION", "ALTERNATIVE_1", "ALTERNATIVE_2", "INCOMPLETE_TITLE"]


def test_the_candidate_limit_still_applies_to_alternatives() -> None:
    job = discover.SearchJob(ASPIRE, "laptop_factory", 1)
    found = [hit(f"{BASE} 16GB RAM 512GB SSD", url("a")), hit(f"{BASE} 16GB RAM 1TB SSD", url("b")),
             hit(f"{BASE} 8GB RAM 512GB SSD", url("c"))]
    ranked = rank_target_hits(ASPIRE, "laptop_factory", [record(ASPIRE, *found)])
    args = Namespace(max_candidates_per_target_retailer=2, max_product_fetches=5)
    manifest, _, _ = discover.select_and_inspect(
        ranked, job, None, None, args, {"product_fetches": 0}, None, lambda _: None)
    decisions = {c.hit.product_url.rsplit("/", 2)[-2]: c.decision for c in ranked}
    assert decisions == {"c": "COLLECT", "a": "COLLECT_CLOSE_ALTERNATIVE_CANDIDATE",
                         "b": "NOT_SELECTED_LIMIT"}
    assert len(manifest) == 2


# Saved candidates.csv rows


def test_a_saved_row_can_be_reassessed_and_an_old_row_stays_readable() -> None:
    row = {"category": "FAMILY_LISTED_SPEC_CANDIDATE", "title": f"{BASE} 16GB RAM 1TB SSD",
           "clue_screen_size_in": "AGREES", "clue_cpu_series": "AGREES",
           "clue_ram_capacity_gb": "DIFFERS (16)", "clue_storage_capacity_gb": "DIFFERS (1000)"}
    potential = title_alternative_potential_from_row(
        row, {"brand": "Acer", "model": "Aspire Lite", "gpu_model": ""})
    assert potential.differences == ("ram", "storage") and potential.tier == "ALTERNATIVE_2"
    bare = title_alternative_potential_from_row({"category": "UNRELATED", "title": "Laptop bag"})
    assert bare.tier == "" and bare.sort_tier == 9
    assert {"title_difference_count", "title_difference_dimensions",
            "title_unknown_dimensions", "alternative_tier"} <= set(CANDIDATE_COLUMNS)
    assert CANDIDATE_COLUMNS[-4:] == ("title_difference_count", "title_difference_dimensions",
                                      "title_unknown_dimensions", "alternative_tier")


# Variants


def option(variant_id: str, title: str) -> VariantInfo:
    return VariantInfo(variant_id, f"https://{PCC_HOST}/products/x?variant={variant_id}", title,
                       "InStock", "1000")


def alternatives(titles, target, potential, limit):
    variants = [option(str(n), title) for n, title in enumerate(titles, start=1)]
    evaluations = {v.variant_id: variant_evaluation(v, target) for v in variants}
    decisions = [(v, evaluations[v.variant_id].decision, "") for v in variants]
    return discover.close_alternative_variants(decisions, evaluations, potential, limit)


def test_colours_are_not_extra_alternatives_and_the_limit_bounds_the_rest() -> None:
    potential = title_alternative_potential(
        rank_target_hits(NITRO, "pcc", [record(NITRO, SearchHit(
            "pcc", f"https://{PCC_HOST}/products/x", f"Acer Nitro V 15 {NITRO_CODE}",
            "", 1, 1))])[0])
    titles = ["8GB RAM / 512GB SSD / Black", "8GB RAM / 512GB SSD / Silver",
              "16GB RAM / 256GB SSD", "8GB RAM / 256GB SSD"]
    one = alternatives(titles, NITRO, potential, 1)
    # Fewest differences first, ties by configuration then variant ID (never price or position);
    # the two 8GB/512GB colours are one configuration.
    assert [(v.variant_id, dims) for v, dims in one] == [("3", ["storage"])]
    both = alternatives(titles, NITRO, potential, 2)
    assert [(v.variant_id, dims) for v, dims in both] == [("3", ["storage"]), ("1", ["ram"])]
    assert alternatives(titles, NITRO, potential, 0) == []
    three = alternatives(titles, NITRO, potential, 5)
    assert len(three) == 3 and len({v.variant_id for v, _ in three}) == 3  # one per configuration
    assert ("4", ["ram", "storage"]) in [(v.variant_id, d) for v, d in three]


def test_variant_differences_and_title_differences_share_the_budget() -> None:
    potential = title_alternative_potential(
        rank_target_hits(NITRO, "pcc", [record(NITRO, SearchHit(
            "pcc", f"https://{PCC_HOST}/products/x",
            f"Acer Nitro V 14-inch {NITRO_CODE} Core i7", "", 1, 1))])[0])
    assert potential.differences == ("screen", "cpu")
    assert alternatives(["8GB RAM / 512GB SSD"], NITRO, potential, 1) == []
    # A difference in a dimension the title already counts is not counted twice.
    ram_title = title_alternative_potential(
        rank_target_hits(NITRO, "pcc", [record(NITRO, SearchHit(
            "pcc", f"https://{PCC_HOST}/products/x",
            f"Acer Nitro V 15 {NITRO_CODE} Core i7 8GB RAM", "", 1, 1))])[0])
    assert ram_title.differences == ("cpu", "ram")
    assert len(alternatives(["8GB RAM / 512GB SSD"], NITRO, ram_title, 1)) == 1


def variant_page_target(ram: str) -> SearchTarget:
    return SearchTarget("TGT-000001", "1", "ExampleBrand", "Example", "15-EX0001AU", "",
                        {"model": "15-EX0001AU", "screen_size_in": "15.6",
                         "cpu_series": "Ryzen 7", "ram_capacity_gb": ram,
                         "storage_capacity_gb": "256"})


@pytest.mark.parametrize(("limit", "collected"), [(0, 0), (1, 1), (2, 2)])
def test_a_multi_variant_page_collects_a_bounded_number_of_alternative_variants(
    tmp_path, limit: int, collected: int
) -> None:
    _, path, fixture_url = FIXTURE_PAGES["pcc_variants"]
    method = SEARCH_METHODS["pcc"]
    handle = fixture_url.split("?")[0].rsplit("/", 1)[-1]
    page = f"https://{method.host}/products/{handle}"
    transport = Routes({page: html(page, (FIXTURES / path).read_text(encoding="utf-8"))})
    target = variant_page_target("32")
    job = discover.SearchJob(target, "pcc", 1)
    found = SearchHit("pcc", page, "ExampleBrand 15-EX0001AU Ryzen 7 15.6 256GB SSD", page, 1, 1)
    ranked = rank_target_hits(target, "pcc", [record(target, found)])
    args = Namespace(max_candidates_per_target_retailer=2, max_product_fetches=5,
                     max_alternative_variants_per_candidate=limit)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    manifest, _, variants = discover.select_and_inspect(
        ranked, job, Fetcher(transport, FetchConfig(), FakeClock()),
        BudgetedTransport(transport, 5), args, {"product_fetches": 0}, evidence, lambda _: None)
    assert len(manifest) == collected and len(variants) == 2
    assert ranked[0].decision == ("COLLECT_VARIANT_ALTERNATIVE" if collected
                                  else "REVIEW_NO_MATCHING_VARIANT")
    assert all(variant_id for _, _, variant_id in manifest)
    assert len({variant_id for _, _, variant_id in manifest}) == collected
    assert PCC.host == method.host
