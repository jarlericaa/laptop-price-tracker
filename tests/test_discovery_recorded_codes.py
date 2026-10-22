"""Recorded manufacturer codes drive discovery queries (2026-10-22, stream A).

TGT-000023 ("TUF A16") and TGT-000041 ("Vivobook") record the complete manufacturer model code
in TARGET_MASTER (``manufacturer_model_code``) while their ``model`` is a broad display name.
Until now only the name was searched, so a listing titled with the recorded code was never asked
for. Search words stay discovery hints: a result is a candidate only when it shows the complete
recorded code, and a shorter search term never permits another full code.
"""

from __future__ import annotations

from laptop_tracker.product_discovery import (
    DIFFERENT_MODEL,
    FAMILY_SPEC_CANDIDATE,
    SearchTarget,
    is_plausible,
    plausible_candidate,
    rank_hit,
)
from laptop_tracker.retailer_search import (
    TIER_CODE_SEGMENT,
    TIER_MODEL_CODE,
    TIER_MODEL_CODE_SPACED,
    TIER_PRODUCT_NAME,
    SearchHit,
    SearchQuery,
    SearchRecord,
    search_queries,
)

TUF_CORE = {"model": "TUF A16", "screen_size_in": "16", "cpu_series": "Ryzen 7",
            "ram_capacity_gb": "16", "storage_capacity_gb": "512"}
VIVO_CORE = {"model": "Vivobook", "screen_size_in": "14", "cpu_series": "Core 5",
             "ram_capacity_gb": "16", "storage_capacity_gb": "512"}


def tuf() -> SearchTarget:
    return SearchTarget("TGT-000023", "21", "ASUS", "", "TUF A16", "", TUF_CORE,
                        identity_kind="FAMILY_NAME", recorded_code="FA607NUQ-RL011W")


def vivobook() -> SearchTarget:
    return SearchTarget("TGT-000041", "30", "ASUS", "", "Vivobook", "", VIVO_CORE,
                        identity_kind="FAMILY_NAME", recorded_code="S3407VA-LY135WSM")


def queries(target: SearchTarget, retailer: str = "pcx") -> list[SearchQuery]:
    return search_queries(target.target_id, retailer, model=target.model,
                          part_number=target.part_number, brand=target.brand,
                          series=target.series, core=target.core, kind=target.kind,
                          recorded_code=target.recorded_code)


def hit(title: str, handle: str = "x") -> SearchHit:
    url = f"https://pcx.com.ph/products/{handle}"
    return SearchHit("pcx", url, title, url, 1, 1)


def rec(target: SearchTarget, *hits: SearchHit) -> SearchRecord:
    query = SearchQuery(target.target_id, "pcx", TIER_MODEL_CODE, target.recorded_code, True, "")
    return SearchRecord(query, 1, "https://pcx.com.ph/search", "RESULTS", hit_items=list(hits))


def test_a_recorded_code_is_the_first_verified_query_for_a_broad_display_name() -> None:
    first = queries(tuf())[0]
    assert (first.tier, first.text, first.verified_identifier) == (
        TIER_MODEL_CODE, "FA607NUQ-RL011W", True)
    assert "recorded" in first.basis
    assert queries(vivobook())[0].text == "S3407VA-LY135WSM"


def test_precise_queries_precede_broad_family_queries_and_nothing_is_repeated() -> None:
    plan = queries(tuf())
    tiers = [q.tier for q in plan]
    assert tiers.index(TIER_MODEL_CODE) < tiers.index(TIER_PRODUCT_NAME)
    # Spellings stay search text only; the full code is searched first and unchanged.
    assert TIER_MODEL_CODE_SPACED in tiers and TIER_CODE_SEGMENT in tiers
    keys = [" ".join(q.text.casefold().split()) for q in plan]
    assert len(keys) == len(set(keys))
    assert any(q.text == "ASUS TUF A16" for q in plan)  # The broad name still follows.


def test_a_target_whose_model_is_the_code_does_not_search_it_twice() -> None:
    target = SearchTarget("TGT-000082", "1", "ASUS", "Vivobook", "X1404VA-EB2264WSM", "",
                          {"model": "X1404VA-EB2264WSM"}, identity_kind="MODEL_CODE",
                          recorded_code="X1404VA-EB2264WSM")
    texts = [q.text for q in queries(target)]
    assert texts.count("X1404VA-EB2264WSM") == 1


def test_equivalent_queries_are_deduplicated_across_case_spacing_and_dash_style() -> None:
    plan = search_queries("T", "pcx", model="TUF A16", part_number="", brand="ASUS", series="",
                          kind="FAMILY_NAME", recorded_code="fa607nuq–rl011w")
    texts = [q.text for q in plan if q.tier == TIER_MODEL_CODE]
    assert len(texts) == 1


def test_a_family_name_without_a_recorded_code_keeps_its_name_queries_only() -> None:
    plan = search_queries("T", "pcx", model="IdeaPad", part_number="", brand="Lenovo",
                          series="", kind="FAMILY_NAME")
    assert TIER_MODEL_CODE not in {q.tier for q in plan}


def test_a_title_showing_the_recorded_code_is_a_plausible_collectable_candidate() -> None:
    target = tuf()
    # The name words are not contiguous ("TUF Gaming A16"): only the recorded code identifies it.
    found = rank_hit(target, hit("ASUS TUF Gaming A16 FA607NUQ-RL011W Ryzen 7 16GB 512GB RTX 4050"),
                     [])
    assert found.category == FAMILY_SPEC_CANDIDATE
    assert "recorded code" in found.code_evidence and is_plausible(found)


def test_a_title_showing_another_code_stays_rejected_for_a_recorded_code() -> None:
    target = tuf()
    for title in ("ASUS TUF Gaming A16 FA607NUQ-RL204WSM Ryzen 7 16GB 512GB",
                  "ASUS TUF Gaming A16 FA607NUG-RL011W Ryzen 7 16GB 512GB",
                  "ASUS TUF Gaming A16 FA607NUQ-RL011WSM Ryzen 7 16GB 512GB"):
        found = rank_hit(target, hit(title), [])
        assert found.category == DIFFERENT_MODEL, title
        assert not is_plausible(found)


def test_a_shortened_search_term_never_makes_another_full_code_plausible() -> None:
    # The segment query "RL011W" can return the sibling model; the full code must still show.
    target = tuf()
    sibling = hit("ASUS TUF Gaming A16 FA608UH-RL011W Ryzen 7 16GB 512GB", "sibling")
    assert plausible_candidate(target, [rec(target, sibling)]) == ""


def test_the_recorded_code_in_the_url_handle_alone_is_collectable_not_plausible() -> None:
    target = vivobook()
    found = rank_hit(target, hit("ASUS laptop", "asus-vivobook-14-s3407va-ly135wsm"), [])
    assert found.category == "CODE_IN_URL_ONLY" and not is_plausible(found)
