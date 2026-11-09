"""Catalog binding audit and correction queue (2026-11-03). Synthetic rows only.

A binding whose listing, variation or target disagrees becomes a queue row for the owner; nothing
is applied, no input row is changed, and a target is never edited to fit a variation.
"""

from __future__ import annotations

import copy
import csv
import json
import sys
from pathlib import Path

import pytest

from laptop_tracker import catalog_corrections as cc

HP_TITLE = "Hp Laptop | Intel i7/i5/i3 & Celeron 4GB-16GB RAM 128GB-512GB SSD"


def target(target_id: str, **values: str) -> dict[str, str]:
    base = {
        "target_id": target_id,
        "status": "ACTIVE",
        "brand": "HP",
        "model": "",
        "screen_size_in": "15.6",
        "cpu_series": "Core i5",
        "ram_capacity_gb": "8",
        "storage_capacity_gb": "256",
        "cpu_model": "",
        "tracking_mode": "CONFIGURATION",
        "notes": "",
        "variant_notes": "",
    }
    return base | values


def variation(
    model_id: str, name: str, target_id: str, *, item: str = HP_TITLE, gmv: str = "1000", rank="5"
) -> dict[str, str]:
    return {
        "eligible_rank": rank,
        "source_key": f"1|2|{model_id}",
        "shop_id": "1",
        "item_id": "2",
        "shop_name": "Shop",
        "source_model_id": model_id,
        "item_name": item,
        "shopee_model_name": name,
        "ranking_gmv_usd": gmv,
        "target_id": target_id,
        "selection_status": "QUALIFIED_NOT_SELECTED",
    }


def links(*pairs: tuple[str, str]) -> list[dict[str, str]]:
    return [{"source_model_id": m, "target_id": t} for m, t in pairs]


def hp_dell_catalog():
    """The known case: an HP i3/4GB/128GB variation bound to a Dell Latitude i5/8GB/256GB target."""
    dell = target(
        "TGT-000238",
        brand="",
        model="Dell Latitde 5300",
        screen_size_in="13.3",
        cpu_model="Intel i7",
        variant_notes="11:I5-10GEN/8G/256GB\nParent item_name: " + HP_TITLE,
        notes="2026-10-08: UNRESOLVED; no promotion.\n2026-10-08: NEW_CONFIGURATION_PROMOTED.",
    )
    ranked = [variation("245580060344", "5:I3-7GEN/4GB/128GB", "TGT-000238", rank="43")]
    return ranked, [dell], links(("245580060344", "TGT-000238"))


def queue_of(ranked, targets, maps, reviews=()):
    return cc.build_correction_queue(ranked, targets, maps, reviews)


def row(queue: cc.CorrectionQueue, queue_id: str) -> dict[str, str]:
    return next(r for r in queue.rows if r["queue_id"] == queue_id)


def test_hp_listing_bound_to_a_dell_target_is_a_brand_mismatch_that_contradicts_the_variation():
    queue = queue_of(*hp_dell_catalog())

    found = row(queue, "CC-VAR-245580060344")
    assert found["finding_class"] == cc.BRAND_MISMATCH
    assert set(found["all_finding_classes"].split(";")) >= {
        cc.BRAND_MISMATCH,
        cc.VARIATION_CONTRADICTS_TARGET,
        cc.TARGET_INTERNAL_CONFLICT,
    }
    text = found["conflicting_fields"]
    assert "'HP'" in text and "'Dell'" in text  # both brands
    assert "cpu_series: variation states 'Core i3'" in text and "'Core i5'" in text
    assert "ram_capacity_gb" in text and "storage_capacity_gb" in text
    assert '"4GB/128GB"' in found["quoted_variation_text"]
    assert (
        found["current_target_id"] == "TGT-000238" and found["target_model"] == "Dell Latitde 5300"
    )
    assert found["priority"] == "P1" and found["applies_automatically"] == "FALSE"
    action = found["recommended_owner_action"]
    assert "Do not edit TARGET_MASTER row TGT-000238" in action
    assert "unmap" in action
    # The target was reviewed from another variation of this model id.
    assert (
        found["binding_basis"].startswith("STALE")
        and "11:I5-10GEN/8G/256GB" in found["binding_basis"]
    )
    assert "UNRESOLVED" in found["target_notes_excerpt"]


def test_clearing_one_field_is_never_the_proposal():
    queue = queue_of(*hp_dell_catalog())

    for queued in queue.rows:
        action = queued["recommended_owner_action"]
        assert (
            "clearing one field does not settle" in action
            or "clearing one field is not" in action
            or (queued["scope"] == cc.SCOPE_PARENT)
        )
        assert "blank" not in action and "set cpu_model" not in action
        if queued["scope"] != cc.SCOPE_PARENT:
            assert "Do not edit TARGET_MASTER row TGT-000238" in action or "yourself" in action


def test_a_variation_that_contradicts_a_same_brand_target_is_not_a_brand_mismatch():
    ranked = [variation("10", "7:I3-6GEN/4G/128GB", "TGT-1")]
    targets = [target("TGT-1", model="HP Probook 450 G5")]

    found = row(queue_of(ranked, targets, links(("10", "TGT-1"))), "CC-VAR-10")

    assert found["finding_class"] == cc.VARIATION_CONTRADICTS_TARGET
    assert cc.BRAND_MISMATCH not in found["all_finding_classes"]
    assert (
        "No existing configuration target with the stated identity"
        in found["recommended_owner_action"]
    )


def test_existing_same_brand_targets_with_the_stated_identity_are_listed_not_mapped():
    ranked = [variation("10", "7:I3-6GEN/4G/128GB", "TGT-1")]
    targets = [
        target("TGT-1", model="HP Probook 450 G5"),
        target(
            "TGT-2",
            model="HP 250 G7",
            cpu_series="Core i3",
            ram_capacity_gb="4",
            storage_capacity_gb="128",
        ),
        target(
            "TGT-3",
            brand="Dell",
            model="Dell 3550",
            cpu_series="Core i3",
            ram_capacity_gb="4",
            storage_capacity_gb="128",
        ),
    ]

    found = row(queue_of(ranked, targets, links(("10", "TGT-1"))), "CC-VAR-10")

    assert found["existing_targets_with_stated_identity"] == "TGT-2"
    assert found["applies_automatically"] == "FALSE"


def test_a_variation_that_matches_its_target_is_not_queued():
    ranked = [variation("10", "5:I5-7GEN/8GB/256GB", "TGT-1")]
    targets = [target("TGT-1", model="HP Probook 450 G5")]

    queue = queue_of(ranked, targets, links(("10", "TGT-1")))

    # The generic title never names ProBook, so only that class may appear, not a contradiction.
    assert [r["queue_id"] for r in queue.rows] == ["CC-VAR-10"]
    assert queue.rows[0]["finding_class"] == cc.MODEL_NOT_STATED


def test_sibling_variations_bound_to_different_brands_fan_in_once_per_parent():
    ranked = [
        variation("10", "5:I5-7GEN/8GB/256GB", "TGT-1", gmv="3000"),
        variation("11", "6:I5-7GEN/8GB/256GB", "TGT-2", gmv="2000"),
        variation("12", "7:I5-7GEN/8GB/256GB", "TGT-3", gmv="1000"),
    ]
    targets = [
        target("TGT-1", model="HP Probook 450 G5"),
        target("TGT-2", brand="Dell", model="Dell 3550"),
        target("TGT-3", brand="Toshiba", model="Toshiba Dynabook S73"),
    ]

    queue = queue_of(ranked, targets, links(("10", "TGT-1"), ("11", "TGT-2"), ("12", "TGT-3")))

    parent = row(queue, "CC-PAR-1-2")
    assert parent["scope"] == cc.SCOPE_PARENT and parent["finding_class"] == cc.SIBLING_FAN_IN
    assert parent["ranking_gmv_usd"] == "6000.00"
    assert "TGT-2" in parent["sibling_summary"] and "Dell 3550" in parent["sibling_summary"]
    for model_id in ("10", "11", "12"):
        assert cc.SIBLING_FAN_IN in row(queue, f"CC-VAR-{model_id}")["all_finding_classes"].split(
            ";"
        )
    assert row(queue, "CC-VAR-11")["finding_class"] == cc.BRAND_MISMATCH


def test_a_multi_model_listing_that_names_each_model_is_not_a_fan_in():
    title = "Lenovo ThinkPad X13/X390/X280 laptop"
    ranked = [
        variation("10", "X280-I5 8TH GEN,8G-256GB", "TGT-1", item=title),
        variation("11", "X390-I5 8TH GEN,8G-256GB", "TGT-2", item=title),
    ]
    targets = [
        target("TGT-1", brand="Lenovo", model="ThinkPad X280", screen_size_in="12.5"),
        target("TGT-2", brand="Lenovo", model="ThinkPad X390", screen_size_in="13.3"),
    ]

    queue = queue_of(ranked, targets, links(("10", "TGT-1"), ("11", "TGT-2")))

    assert queue.rows == []


def test_a_target_that_contradicts_itself_is_queued_even_outside_the_pool():
    targets = [target("TGT-9", model="HP 250 G7", cpu_model="Intel i7")]

    queue = queue_of([], targets, links(("999", "TGT-9")))

    found = row(queue, "CC-TGT-TGT-9")
    assert (
        found["scope"] == cc.SCOPE_TARGET and found["finding_class"] == cc.TARGET_INTERNAL_CONFLICT
    )
    assert (
        "cpu_model 'Intel i7' indicates Core i7, but cpu_series is 'Core i5'"
        in found["conflicting_fields"]
    )
    assert found["source_model_id"] == "999"
    assert "TARGET_CPU_MODEL_CONFLICT" in found["recommended_owner_action"]
    assert queue.summary["mapped_ids_outside_the_pool"] == 1


def test_a_listing_only_target_still_gets_its_field_checks_and_is_flagged_for_its_binding():
    ranked = [variation("10", "5:I5-7GEN/8GB/256GB", "TGT-1")]
    targets = [target("TGT-1", brand="HP", tracking_mode="LISTING_ONLY", cpu_model="Intel i7")]

    queue = queue_of(ranked, targets, links(("10", "TGT-1")))

    found = row(queue, "CC-VAR-10")
    classes = found["all_finding_classes"].split(";")
    assert cc.LISTING_ONLY_BINDING in classes and cc.TARGET_INTERNAL_CONFLICT in classes
    assert row(queue, "CC-TGT-TGT-1")["finding_class"] == cc.TARGET_INTERNAL_CONFLICT


def test_a_brand_blank_target_that_names_no_brand_is_unknown_not_a_mismatch():
    ranked = [variation("10", "5:I5-7GEN/8GB/256GB", "TGT-1", item="Hp Laptop 15.6")]
    targets = [target("TGT-1", brand="", model="Laptop")]

    queue = queue_of(ranked, targets, links(("10", "TGT-1")))

    assert queue.rows == []


def test_every_row_is_marked_not_applied_and_the_inputs_are_never_changed():
    ranked, targets, maps = hp_dell_catalog()
    reviews = [{"source_model_id": "245580060344", "candidate_name": "x", "status": "INCOMPLETE"}]
    before = copy.deepcopy((ranked, targets, maps, reviews))

    queue = queue_of(ranked, targets, maps, reviews)

    assert (ranked, targets, maps, reviews) == before
    assert queue.rows and all(r["applies_automatically"] == "FALSE" for r in queue.rows)
    assert set(queue.rows[0]) == set(cc.QUEUE_COLUMNS)
    assert queue.summary["applies_automatically"] == "FALSE for every row"


def test_priority_follows_the_variations_ranking():
    ranked = [
        variation("10", "7:I3-6GEN/4G/128GB", "TGT-1", rank="5"),
        variation("11", "7:I3-6GEN/4G/128GB", "TGT-1", rank="150", item="Hp Laptop 15.6"),
        variation("12", "7:I3-6GEN/4G/128GB", "TGT-1", rank="900", item="Hp Laptop 15.6"),
    ]
    ranked[1]["item_id"], ranked[2]["item_id"] = "3", "4"
    targets = [target("TGT-1", model="HP Probook 450 G5")]

    queue = queue_of(ranked, targets, links(("10", "TGT-1"), ("11", "TGT-1"), ("12", "TGT-1")))

    assert [row(queue, f"CC-VAR-{m}")["priority"] for m in ("10", "11", "12")] == ["P1", "P2", "P3"]
    assert [r["queue_id"] for r in queue.rows][:3] == ["CC-VAR-10", "CC-VAR-11", "CC-VAR-12"]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("5:I3-7GEN/4GB/128GB", ("4", "128")),
        ("9:INTEL-11GEN/4/128G", ("4", "128")),
        ("11:I5-10GEN/8G/256GB", ("8", "256")),
        ("Silver,16+512G", ("16", "512")),
        ("X13-I7 10TH GEN,16G-512GB", ("16", "512")),
        ("Midnight,16GB/1TB", ("16", "1000")),
        ("i5-10 12345", None),
        ("Black", None),
        ("14:i5-8GEN", None),
        ("8GB/256GB or 16GB/512GB", None),  # two candidate pairs: left unknown
    ],
)
def test_an_unlabeled_ram_storage_pair_is_read_only_when_it_is_the_one_plausible_pair(
    name, expected
):
    pair = cc._pair_capacities(name)

    assert (pair[0], pair[1]) == expected if expected else pair is None


def test_a_labeled_statement_wins_over_the_pair_reading():
    readings = cc.read_binding_specs(HP_TITLE, "I5 8GB RAM 256GB SSD 4/128")

    assert (
        readings["ram_capacity_gb"].value == "8"
        and readings["ram_capacity_gb"].source == "VARIATION"
    )
    assert readings["storage_capacity_gb"].value == "256"


def test_brand_detection_needs_one_brand():
    assert cc.listing_brand("Hp Laptop | Intel i7") == ("HP", "Hp")
    assert cc.listing_brand("Fujitsu Laptop")[0] == "Fujitsu"
    assert cc.listing_brand("Lenovo ThinkPad X13")[0] == "Lenovo"
    assert cc.listing_brand("HP charger for Dell laptops") == ("", "")
    assert cc.target_brand({"brand": "", "model": "Dell Latitde 5300"}) == (
        "Dell",
        "INFERRED_FROM_MODEL",
    )
    assert cc.target_brand({"brand": "HP", "model": "x"}) == ("HP", "RECORDED")


def test_binding_basis_reads_the_variation_the_target_was_reviewed_from():
    reviewed = {"variant_notes": "11:I5-10GEN/8G/256GB\nVariation model_name: 11:I5-10GEN/8G/256GB"}

    assert cc.binding_basis(reviewed, "11:I5-10GEN/8G/256GB")[0] == "CURRENT"
    state, recorded = cc.binding_basis(reviewed, "5:I3-7GEN/4GB/128GB")
    assert state == "STALE" and recorded == "11:I5-10GEN/8G/256GB"
    assert cc.binding_basis({"notes": "nothing here"}, "x")[0] == "UNRECORDED"


def test_outputs_are_written_and_carry_the_evidence(tmp_path: Path):
    queue = queue_of(*hp_dell_catalog())

    cc.write_queue_outputs(tmp_path, queue, {"target_master": "tm.csv"}, {"target_master": "abc"})

    with (tmp_path / "catalog_correction_queue.csv").open(newline="", encoding="utf-8") as handle:
        read = list(csv.DictReader(handle))
    assert [r["queue_id"] for r in read] == [r["queue_id"] for r in queue.rows]
    document = json.loads((tmp_path / "catalog_correction_queue.json").read_text(encoding="utf-8"))
    assert document["input_sha256"] == {"target_master": "abc"}
    detail = next(r for r in document["rows"] if r["queue_id"] == "CC-VAR-245580060344")
    assert detail["evidence"]["field_conflicts"][0]["field"] == "cpu_series"
    markdown = (tmp_path / "catalog_correction_queue.md").read_text(encoding="utf-8")
    assert "applies_automatically` is FALSE" in markdown and "CC-VAR-245580060344" in markdown


def test_the_script_refuses_an_output_folder_outside_data_output(tmp_path: Path, capsys):
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "scripts"))
    try:
        import build_catalog_correction_queue as script
    finally:
        sys.path.remove(str(root / "scripts"))

    status = script.main(["--run-dir", str(tmp_path), "--output-dir", str(tmp_path / "out")])

    assert status == 1 and not (tmp_path / "out").exists()
    assert "Output must be under" in capsys.readouterr().err


def test_the_script_writes_the_queue_only_into_its_output_dir_and_never_touches_the_inputs(
    monkeypatch, tmp_path: Path
):
    import hashlib

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "scripts"))
    try:
        import build_catalog_correction_queue as script
    finally:
        sys.path.remove(str(root / "scripts"))
    monkeypatch.setattr(script, "ROOT", tmp_path)
    run = tmp_path / "data" / "output" / "run"
    ranked, targets, maps = hp_dell_catalog()

    def write(path: Path, rows: list[dict[str, str]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        columns = list(dict.fromkeys(k for r in rows for k in r))
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)

    write(run / "shopee_variations" / "shopee_variation_ranking.csv", ranked)
    write(run / "inputs" / "target_master_validated.csv", targets)
    write(run / "inputs" / "source_model_map_validated.csv", maps)
    write(run / "inputs" / "match_review_validated.csv", [{"source_model_id": "1", "status": "X"}])

    def digests() -> dict[str, str]:
        return {
            str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(run.rglob("*"))
            if p.is_file()
        }

    before = digests()
    status = script.main(["--run-dir", str(run), "--dry-run"])
    assert status == 0 and digests() == before

    status = script.main(["--run-dir", str(run)])

    after = digests()
    assert status == 0
    assert {k: v for k, v in after.items() if k in before} == before
    created = {Path(k).name for k in set(after) - set(before)}
    assert created == {
        "catalog_correction_queue.csv",
        "catalog_correction_queue.json",
        "catalog_correction_queue.md",
    }
