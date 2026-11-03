"""Close-alternative evaluation (2026-10-27): at most two established differences across the
five core dimensions plus GPU, with differences kept apart from unknowns and contradictions."""

from __future__ import annotations

import pytest

from laptop_tracker.alternatives import (
    AGREES,
    ALTERNATIVE,
    CONFLICTING,
    DIFFERS,
    EXCLUDED_DIFFERENCES,
    EXCLUDED_FAMILY,
    NO_DIFFERENCE,
    RECHECK,
    UNKNOWN,
    AlternativeEvaluator,
    OfferEvidence,
    alternative_tag,
)
from laptop_tracker.product_families import FamilyRegistry, FamilyRegistryError

TITLE = "MSI Thin 15 B13UC-3474PH 15.6 Core i5 RTX 3050 Gaming Laptop"


def target(**overrides) -> dict[str, str]:
    row = {
        "target_id": "TGT-000258", "status": "ACTIVE", "brand": "MSI", "series": "Thin 15",
        "model": "B13UC-3474PH", "screen_size_in": "15.6", "cpu_series": "Core i5",
        "cpu_model": "Core i5-13420H", "ram_capacity_gb": "16", "storage_capacity_gb": "512",
        "gpu_model": "RTX 3050", "tracking_mode": "CONFIGURATION",
    }
    return row | overrides


def offer(title: str = TITLE, ambiguous=(), codes=(), **values: str) -> OfferEvidence:
    base = {
        "brand": "MSI", "model": "B13UC-3474PH", "screen_size_in": "15.6", "cpu_series": "Core i5",
        "cpu_model": "i5-13420H", "ram_capacity_gb": "16", "storage_capacity_gb": "512",
        "gpu_model": "RTX 3050",
    }
    merged = {k: v for k, v in (base | values).items() if v != ""}
    return OfferEvidence(merged, title, tuple(codes), frozenset(ambiguous))


def evaluate(evidence: OfferEvidence, row: dict[str, str] | None = None):
    row = row or target()
    return AlternativeEvaluator([row]).evaluate(row, evidence)


def test_zero_differences_is_not_an_alternative_and_not_a_match() -> None:
    result = evaluate(offer())
    assert result.status == NO_DIFFERENCE and result.tag == "" and result.difference_count == 0
    assert not result.is_alternative  # exact and listed-spec eligibility are decided elsewhere


def test_one_ram_difference_is_alt_1() -> None:
    result = evaluate(offer(ram_capacity_gb="8"))
    assert result.status == ALTERNATIVE and result.difference_count == 1
    assert result.tag == "ALT 1 · RAM" and result.differing == ("ram",)
    assert result.differences_text == "RAM: target 16GB → retailer 8GB."


def test_ram_and_storage_differences_are_alt_2_in_stable_order() -> None:
    result = evaluate(offer(ram_capacity_gb="8", storage_capacity_gb="256"))
    assert result.tag == "ALT 2 · RAM, Storage"
    assert result.differences_text == (
        "RAM: target 16GB → retailer 8GB; storage: target 512GB → retailer 256GB.")


def test_gpu_alone_is_alt_1_and_rtx_5050_versus_4050_counts_once() -> None:
    result = evaluate(offer(gpu_model="RTX 4050"), target(gpu_model="RTX 5050"))
    assert result.tag == "ALT 1 · GPU"
    assert result.differences_text == "GPU: target RTX 5050 → retailer RTX 4050."


def test_cpu_and_gpu_are_alt_2() -> None:
    result = evaluate(offer(cpu_series="Core i7", cpu_model="i7-13620H", gpu_model="RTX 4050"))
    assert result.tag == "ALT 2 · CPU, GPU"
    assert alternative_tag(2, ["gpu", "cpu"]) == "ALT 2 · CPU, GPU"


def test_ram_storage_and_gpu_are_three_differences_and_excluded() -> None:
    result = evaluate(offer(ram_capacity_gb="8", storage_capacity_gb="256", gpu_model="RTX 4050"))
    assert result.status == EXCLUDED_DIFFERENCES and result.difference_count == 3
    assert not result.is_alternative


def test_three_core_differences_are_excluded() -> None:
    result = evaluate(offer(screen_size_in="14", ram_capacity_gb="8", storage_capacity_gb="256"))
    assert result.status == EXCLUDED_DIFFERENCES


def test_screen_size_is_one_difference_without_rounding() -> None:
    result = evaluate(offer(screen_size_in="15"), target(screen_size_in="15.6"))
    assert result.tag == "ALT 1 · Screen"
    assert result.differences_text == "screen: target 15.6in → retailer 15in."


def test_different_exact_cpus_in_one_series_count_once() -> None:
    result = evaluate(offer(cpu_model="i5-12450H"))
    assert result.differing == ("cpu",) and result.tag == "ALT 1 · CPU"
    assert result.dimension("cpu").basis == "EXACT_PROCESSOR"


def test_series_and_exact_processor_changing_together_is_one_cpu_difference() -> None:
    result = evaluate(offer(cpu_series="Core i7", cpu_model="i7-13620H"))
    assert result.difference_count == 1 and result.differing == ("cpu",)


def test_core_i5_core_5_and_core_ultra_5_stay_distinct() -> None:
    for series in ("Core 5", "Core Ultra 5"):
        result = evaluate(offer(cpu_series=series, cpu_model=""))
        assert result.differing == ("cpu",)
    pro = evaluate(offer("Apple MacBook Pro 14 M5 Pro chip", cpu_series="Apple M5 Pro",
                         cpu_model="", brand="Apple", model="MacBook Pro", screen_size_in="14",
                         gpu_model=""),
                   target(brand="Apple", model="MacBook Pro", series="", screen_size_in="14",
                          cpu_series="Apple M5", cpu_model="M5", gpu_model=""))
    assert pro.differing == ("cpu",) and pro.tag == "ALT 1 · CPU"  # the Pro modifier is kept


def test_missing_exact_cpu_is_a_visible_limited_basis_not_equality() -> None:
    result = evaluate(offer(cpu_model="", ram_capacity_gb="8"))
    cpu = result.dimension("cpu")
    assert cpu.state == AGREES and cpu.basis == "SERIES_ONLY"
    assert "CPU_EXACT_PROCESSOR_UNKNOWN" in result.limitations
    assert result.comparison_basis == "CPU SERIES_ONLY"


@pytest.mark.parametrize("missing", ["ram_capacity_gb", "storage_capacity_gb", "screen_size_in",
                                     "cpu_series", "model"])
def test_a_missing_core_value_is_never_agreement(missing: str) -> None:
    result = evaluate(offer(**{missing: ""}, gpu_model="RTX 4050"))
    assert result.status == RECHECK and any(r.startswith("REQUIRED_") for r in result.reasons)


def test_unknown_gpu_stays_unknown_and_is_not_a_difference() -> None:
    result = evaluate(offer(gpu_model="", ram_capacity_gb="8"))
    assert result.dimension("gpu").state == UNKNOWN
    assert result.status == ALTERNATIVE and result.difference_count == 1
    assert "GPU_UNKNOWN" in result.limitations
    assert result.unknown == ("gpu",)
    unspecified = evaluate(offer(ram_capacity_gb="8"), target(gpu_model=""))
    assert unspecified.dimension("gpu").state == UNKNOWN and unspecified.difference_count == 1


def test_a_target_without_gpu_never_gains_one_from_the_offer() -> None:
    result = evaluate(offer(gpu_model="RTX 4050"), target(gpu_model=""))
    assert result.dimension("gpu").state == UNKNOWN and result.differing == ()


def test_several_unbound_gpus_are_conflicting_evidence_not_a_chosen_gpu() -> None:
    result = evaluate(offer(gpu_model="", ambiguous=["gpu_model"]))
    assert result.dimension("gpu").state == CONFLICTING and result.status == RECHECK


def test_an_internal_contradiction_is_not_an_ordinary_difference() -> None:
    contradictory = evaluate(offer(ram_capacity_gb="", ambiguous=["ram_capacity_gb"],
                                   codes=["TITLE_SPEC_CONFLICT_RAM_CAPACITY_GB"]))
    assert contradictory.dimension("ram").state == CONFLICTING
    assert contradictory.status == RECHECK and contradictory.difference_count == 0
    consistent = evaluate(offer(ram_capacity_gb="8"), target(ram_capacity_gb="16"))
    assert consistent.dimension("ram").state == DIFFERS and consistent.status == ALTERNATIVE


def test_contradictory_screen_statements_remain_a_recheck() -> None:
    result = evaluate(offer(screen_size_in="", ambiguous=["screen_size_in"]))
    assert result.dimension("screen").state == CONFLICTING and result.status == RECHECK


def test_contradictory_model_identifiers_remain_a_recheck() -> None:
    result = evaluate(offer(codes=["MODEL_CLAIM_CONFLICT"]))
    assert result.dimension("model").state == CONFLICTING and result.status == RECHECK


def test_two_differences_plus_a_conflict_is_not_confirmed() -> None:
    result = evaluate(offer(ram_capacity_gb="8", storage_capacity_gb="256", screen_size_in="",
                            ambiguous=["screen_size_in"]))
    assert result.status == RECHECK  # never a silent ALT 2


def test_a_different_brand_is_excluded() -> None:
    result = evaluate(offer(title="ASUS Vivobook S3607VA 15.6 Core i5", brand="ASUS"))
    assert result.status == EXCLUDED_FAMILY and result.reasons == ("BRAND_DIFFERS",)


def test_an_unrelated_family_of_the_same_brand_is_excluded() -> None:
    result = evaluate(offer(title="MSI Cyborg 15 A12UCX-239PH Core i5 RTX 3050",
                            model="A12UCX-239PH"))
    assert result.status == EXCLUDED_FAMILY and result.reasons == ("FAMILY_DIFFERS",)


def test_macbook_air_and_macbook_pro_are_never_one_family() -> None:
    air = {"target_id": "TGT-000001", "status": "ACTIVE", "brand": "Apple", "model": "MacBook Air",
           "screen_size_in": "13", "cpu_series": "Apple M5", "cpu_model": "M5",
           "ram_capacity_gb": "16", "storage_capacity_gb": "512", "tracking_mode": "CONFIGURATION"}
    pro = OfferEvidence(
        {"brand": "Apple", "model": "MacBook Pro", "screen_size_in": "13", "cpu_series": "Apple M5",
         "ram_capacity_gb": "16", "storage_capacity_gb": "512"}, "Apple MacBook Pro 13 M5 chip")
    assert evaluate(pro, air).status == EXCLUDED_FAMILY
    same = OfferEvidence(
        {"brand": "Apple", "model": "MacBook Air", "screen_size_in": "15", "cpu_series": "Apple M5",
         "ram_capacity_gb": "16", "storage_capacity_gb": "512"}, "Apple MacBook Air 15 M5 chip")
    result = evaluate(same, air)
    assert result.tag == "ALT 1 · Screen" and result.dimension("gpu").state == UNKNOWN


def test_a_vague_shared_word_is_not_a_family() -> None:
    # "Thin" in "Thin and Light" names no family; only the reviewed names (Thin 15) do.
    row = target(model="Thin 15", series="")
    result = evaluate(offer(title="MSI Modern 14 Thin and Light laptop", model="C13M-458PH"), row)
    assert result.status == EXCLUDED_FAMILY and result.reasons == ("FAMILY_NOT_STATED",)


def test_a_family_not_in_the_registry_is_not_inferred_from_a_shared_word() -> None:
    row = target(series="Obscure", model="OB1-22")
    result = evaluate(offer(title="MSI Obscure Thin light laptop", model="XB-77"), row)
    assert result.status == EXCLUDED_FAMILY and result.reasons == ("FAMILY_NOT_RECOGNIZED",)


def test_different_model_codes_inside_the_family_are_one_model_difference() -> None:
    result = evaluate(offer(model="B12UC-1435PH", title="MSI Thin 15 B12UC-1435PH i5 RTX 3050"))
    assert result.differing == ("model",) and result.tag == "ALT 1 · Model"
    assert result.differences_text == "model: target B13UC-3474PH → retailer B12UC-1435PH."


def test_model_cpu_and_gpu_changes_are_counted_independently() -> None:
    result = evaluate(offer(model="B12UC-1435PH", cpu_model="i5-12450H", gpu_model="RTX 4050",
                            title="MSI Thin 15 B12UC-1435PH"))
    assert result.status == EXCLUDED_DIFFERENCES and result.differing == ("model", "cpu", "gpu")
    two = evaluate(offer(model="B12UC-1435PH", cpu_model="i5-12450H", gpu_model="RTX 3050",
                         ram_capacity_gb="16", title="MSI Thin 15 B12UC-1435PH"))
    assert two.tag == "ALT 2 · Model, CPU"  # CPU series and exact processor are one dimension


def test_same_model_code_with_consistently_different_ram_is_an_alternative() -> None:
    result = evaluate(offer(ram_capacity_gb="8", title="MSI Thin 15 B13UC-3474PH 8GB"))
    assert result.tag == "ALT 1 · RAM" and result.family_basis == "SAME_MODEL_CODE"
    assert result.dimension("model").state == AGREES


def test_a_missing_specific_code_stays_unknown_for_a_code_target() -> None:
    result = evaluate(offer(model=""))
    assert result.dimension("model").state == UNKNOWN and result.status == RECHECK


def test_a_broad_family_target_keeps_the_specific_model_unknown() -> None:
    row = target(model="Thin 15", series="", cpu_model="", gpu_model="RTX 3050")
    result = evaluate(offer(model="B13UC-3474PH", ram_capacity_gb="8"), row)
    model = result.dimension("model")
    assert model.state == AGREES and model.basis == "FAMILY_ONLY"
    assert "SPECIFIC_MODEL_UNKNOWN" in result.limitations and result.tag == "ALT 1 · RAM"


def test_a_recorded_code_that_differs_is_a_model_difference_for_a_family_target() -> None:
    row = target(model="Thin 15", series="", manufacturer_model_code="B13UC-3474PH")
    result = evaluate(offer(model="B12UC-1435PH", title="MSI Thin 15 B12UC-1435PH"), row)
    assert result.differing == ("model",)


def test_material_conflicts_outside_the_dimensions_stay_blocking() -> None:
    row = target(resolution="1920x1080")
    result = evaluate(offer(resolution="2560x1440", ram_capacity_gb="8"), row)
    assert result.status == RECHECK and result.material_conflicts == ("resolution",)
    assert result.reasons == ("MATERIAL_CONFLICT_RESOLUTION",)


def test_warning_class_differences_are_carried_not_blocking() -> None:
    row = target(os="Windows 11 Home")
    result = evaluate(offer(os="FreeDOS", ram_capacity_gb="8"), row)
    assert result.status == ALTERNATIVE and "SECONDARY_WARNING_OS" in result.warnings


def test_evaluate_offer_scopes_candidates_by_brand_and_keeps_lineage_visible() -> None:
    asus = target(target_id="TGT-000031", brand="ASUS", series="Vivobook", model="S3607VA-RP181WSM")
    evaluator = AlternativeEvaluator([target(), asus])
    evidence = offer(ram_capacity_gb="8")
    assert [e.target_id for e in evaluator.evaluate_offer(evidence)] == ["TGT-000258"]
    with_lineage = evaluator.evaluate_offer(evidence, always=["TGT-000031"])
    assert {e.target_id: e.status for e in with_lineage} == {
        "TGT-000258": ALTERNATIVE, "TGT-000031": EXCLUDED_FAMILY}
    assert evaluator.evaluate_offer(evidence, skip=["TGT-000258"]) == []


def test_the_family_registry_rejects_inconsistent_rows(tmp_path) -> None:
    path = tmp_path / "families.csv"
    path.write_text("brand,family,names,includes,evidence,reviewed_on\n"
                    "Acer,Aspire,Aspire,Aspire Lite,e,2026-10-27\n")
    with pytest.raises(FamilyRegistryError):
        FamilyRegistry.read(path)
