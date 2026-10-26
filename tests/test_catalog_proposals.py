"""Catalog correction proposals (2026-10-22): evidence-backed, read-only, never applied."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest

from laptop_tracker import catalog_proposals as cp
from laptop_tracker.catalog_proposals import (
    CatalogContext,
    OfferEvidence,
    OutputLocationError,
    Statement,
    build_proposals,
)

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "data" / "output" / "top100_rerun_20261022T083904Z"


def target(target_id: str, **values: str) -> dict[str, str]:
    row = {
        "target_id": target_id, "status": "ACTIVE", "tracking_mode": "CONFIGURATION",
        "brand": "HP", "model": "HP Laptop 15", "series": "", "screen_size_in": "15",
        "cpu_brand": "AMD", "cpu_series": "Ryzen 3", "cpu_model": "", "ram_capacity_gb": "8",
        "ram_type": "", "storage_capacity_gb": "512", "gpu_type": "", "gpu_brand": "",
        "gpu_model": "", "vram": "", "manufacturer_model_code": "",
        "manufacturer_part_number": "", "variant_notes": "", "notes": "",
    }
    return row | values


def offer(url: str, title: str, *, screen: str = "15.6", cpu: str = "Ryzen 3", ram: str = "8",
          storage: str = "512", model: str = "", specs=(), statements=None, status: str = "",
          target_id: str = "", candidates: tuple[str, ...] = ()) -> OfferEvidence:
    return OfferEvidence(
        retailer_id="laptop_factory", offer_url=url, retrieved_at="2026-10-22T09:00:00Z",
        title=title, observation_id="obs-" + url[-6:], source="synthetic", match_status=status,
        target_id=target_id, candidate_target_ids=candidates, offer_model=model,
        offer_values={"screen_size_in": screen, "cpu_series": cpu, "ram_capacity_gb": ram,
                      "storage_capacity_gb": storage},
        statements=statements if statements is not None else {"screen_size_in": [
            Statement("screen_size_in", screen, "Display Size", f"{screen}″ diagonal, FHD",
                      "PRODUCT_SPECIFICATION")]},
        specs=list(specs))


def context(targets, **tables) -> CatalogContext:
    return CatalogContext(targets=targets, **tables)


def only(proposals, ptype, field=None):
    return [p for p in proposals if p.proposal_type == ptype
            and (field is None or p.field == field)]


# 1. Family target next to a specific target ---------------------------------------------------


def test_family_and_specific_target_with_shared_offer_is_one_unapplied_proposal():
    family = target("TGT-000013", model="HP Laptop 14", screen_size_in="14", cpu_series="Core i3",
                    variant_notes="HP 14-ep0445TU\nParent item_name: HP Laptop 14-ep0445TU")
    specific = target("TGT-000233", model="14-ep0445TU", screen_size_in="14",
                      cpu_series="Core i3", manufacturer_model_code="14-ep0445TU",
                      variant_notes="HP Laptop | 14-ep0445TU | i3-1315U")
    page = offer("https://lf.example/product/hp-laptop-14-ep0445tu/", "HP Laptop 14-ep0445TU",
                 screen="14", cpu="Core i3", status="EXACT", target_id="TGT-000233",
                 candidates=("TGT-000013", "TGT-000233"))
    ctx = context([family, specific], source_model_map=[
        {"source_model_id": "111", "target_id": "TGT-000013"},
        {"source_model_id": "222", "target_id": "TGT-000233"}],
        selections=[{"target_id": "TGT-000013", "offer_url": page.offer_url, "price": "31999"},
                    {"target_id": "TGT-000233", "offer_url": page.offer_url, "price": "31999"}])
    before = json.dumps([family, specific], sort_keys=True)
    found = only(build_proposals(ctx, [page]), cp.TYPE_FAMILY_VS_SPECIFIC)
    assert len(found) == 1
    proposal = found[0]
    assert proposal.target_ids == ("TGT-000013", "TGT-000233")
    assert proposal.confidence == cp.HIGH
    assert "price both targets" in proposal.uncertainty and page.offer_url in proposal.uncertainty
    assert "no value proposed" in proposal.proposed_value
    assert "(a) retire" in proposal.proposed_value and "(b) keep" in proposal.proposed_value
    assert proposal.row()["applies_automatically"] == "FALSE"
    assert proposal.source_model_ids == ("111", "222")
    assert "nothing is merged" in proposal.reviewer_checks
    assert json.dumps([family, specific], sort_keys=True) == before  # inputs untouched


def test_different_configurations_or_unlinked_targets_are_not_proposed_as_duplicates():
    family = target("TGT-A", model="HP Laptop 14", screen_size_in="14", cpu_series="Core i3")
    other_ram = target("TGT-B", model="14-ep0445TU", screen_size_in="14", cpu_series="Core i3",
                       ram_capacity_gb="16", variant_notes="HP Laptop | 14-ep0445TU")
    unlinked = target("TGT-C", model="14-xx0001TU", screen_size_in="14", cpu_series="Core i3",
                      variant_notes="a different story")
    assert only(build_proposals(context([family, other_ram, unlinked]), []),
                cp.TYPE_FAMILY_VS_SPECIFIC) == []


# 2. Screen class versus diagonal --------------------------------------------------------------


def test_hp_15_with_identity_established_15_6_page_gets_a_per_target_proposal():
    hp15 = target("TGT-000016")
    page = offer("https://lf.example/product/hp-laptop-15-fc0933au/", "HP Laptop 15-fc0933AU",
                 model="15-fc0933AU")
    ctx = context([hp15], source_model_map=[{"source_model_id": "9", "target_id": "TGT-000016"}])
    (proposal,) = only(build_proposals(ctx, [page]), cp.TYPE_SCREEN)
    assert (proposal.old_value, proposal.proposed_value) == ("15", "15.6")
    assert proposal.evidence[0]["quoted_text"].startswith("15.6″ diagonal")
    assert proposal.evidence[0]["scope"] == "PRODUCT_SPECIFICATION"
    assert proposal.evidence[0]["model_code"] == "15-fc0933AU"
    assert "Do not apply a global" in proposal.reviewer_checks
    assert proposal.source_model_ids == ("9",)


@pytest.mark.parametrize("changed", [
    {"ram": "16"}, {"storage": "256"}, {"cpu": "Ryzen 5"},          # another configuration
])
def test_screen_proposal_needs_the_other_three_core_values_to_agree(changed):
    page = offer("https://lf.example/product/hp-laptop-15-fc0933au/", "HP Laptop 15-fc0933AU",
                 model="15-fc0933AU", **changed)
    assert only(build_proposals(context([target("TGT-1")]), [page]), cp.TYPE_SCREEN) == []


def test_screen_proposal_needs_the_page_to_name_the_family_and_the_brand():
    victus = offer("https://lf.example/product/hp-victus-15-fa1/", "HP Victus Gaming Laptop 15-fa1",
                   model="15-fa1")
    other_brand = offer("https://lf.example/product/acer-15/", "Acer Aspire 15 Laptop")
    assert only(build_proposals(context([target("TGT-1")]), [victus, other_brand]),
                cp.TYPE_SCREEN) == []


def test_unexplained_fractional_difference_is_review_only_with_no_value():
    hp14 = target("TGT-14", model="HP Laptop 14", screen_size_in="14")
    page = offer("https://lf.example/product/hp-laptop-14-xx/", "HP Laptop 14-xx0001TU",
                 screen="14.5", model="14-xx0001TU")
    (proposal,) = only(build_proposals(context([hp14]), [page]), cp.TYPE_SCREEN)
    assert proposal.proposed_value == "" and proposal.confidence == cp.REVIEW_ONLY
    assert "unexplained" in proposal.field and "no value is proposed" in proposal.uncertainty


def test_other_brands_are_not_screen_proposals_unless_requested():
    asus = target("TGT-A", brand="ASUS", model="Vivobook 15")
    page = offer("https://lf.example/product/asus-vivobook-15/", "ASUS Vivobook 15 X1504",
                 model="X1504VA")
    assert only(build_proposals(context([asus]), [page]), cp.TYPE_SCREEN) == []
    wide = only(build_proposals(context([asus]), [page], screen_brands=["*"]), cp.TYPE_SCREEN)
    assert len(wide) == 1 and wide[0].proposed_value == "15.6"


def test_conflicting_diagonals_leave_the_proposed_value_blank():
    hp15 = target("TGT-15")
    pages = [offer("https://lf.example/product/hp-laptop-15-a1/", "HP Laptop 15-a1", model="15-a1"),
             offer("https://lf.example/product/hp-laptop-15-b2/", "HP Laptop 15-b2", model="15-b2",
                   screen="15.5")]
    proposals = only(build_proposals(context([hp15]), pages), cp.TYPE_SCREEN)
    assert {p.field: p.proposed_value for p in proposals} == {
        "screen_size_in": "15.6", "screen_size_in (unexplained difference)": ""}


# 3. MSI memory fields -------------------------------------------------------------------------


def msi_target(**values):
    return target("TGT-000021", brand="MSI", model="B12UC-1432PH", screen_size_in="15",
                  cpu_series="Core i7", cpu_brand="Intel", ram_capacity_gb="16",
                  ram_type="GDDR6", vram="16GB", gpu_model="RTX 3050",
                  manufacturer_model_code="B12UC-1432PH", **values)


def msi_page(url="https://lf.example/product/msi-thin-15-b12uc-1432ph/"):
    return offer(url, "MSI Gaming Thin 15 B12UC-1432PH", cpu="Core i7", ram="16",
                 model="B12UC-1432PH", status="EXACT", target_id="TGT-000021", specs=[
                     ("RAM", "16GB 3200Mhz DDR4"),
                     ("Graphics", "NVIDIA® GeForce RTX 3050 Laptop GPU 4GB GDDR6")])


def test_msi_graphics_memory_in_system_fields_gets_field_level_proposals():
    proposals = only(build_proposals(context([msi_target()]), [msi_page()]), cp.TYPE_MSI)
    by_field = {p.field: p for p in proposals}
    assert (by_field["ram_type"].old_value, by_field["ram_type"].proposed_value) == (
        "GDDR6", "DDR4")
    assert (by_field["vram"].old_value, by_field["vram"].proposed_value) == ("16GB", "4GB")
    assert (by_field["screen_size_in"].old_value, by_field["screen_size_in"].proposed_value) == (
        "15", "15.6")
    assert "equals the system RAM" in by_field["vram"].uncertainty
    assert "ram_capacity_gb (a core identity field) unchanged" in by_field["vram"].reviewer_checks
    assert by_field["vram"].evidence[0]["quoted_text"].endswith("4GB GDDR6")


def test_msi_without_page_evidence_proposes_blank_values_not_guesses():
    proposals = only(build_proposals(context([msi_target()]), []), cp.TYPE_MSI)
    by_field = {p.field: p for p in proposals}
    assert by_field["ram_type"].proposed_value == "" and by_field["vram"].proposed_value == ""
    assert {p.confidence for p in proposals} == {cp.REVIEW_ONLY}


def test_a_clean_msi_target_has_no_proposal():
    clean = msi_target() | {"ram_type": "DDR4", "vram": "4GB", "screen_size_in": "15.6"}
    assert only(build_proposals(context([clean]), [msi_page()]), cp.TYPE_MSI) == []


# 4. Broad display name with a recorded manufacturer code --------------------------------------


def broad_context(**tables):
    row = target("TGT-000023", brand="ASUS", model="TUF A16", screen_size_in="16",
                 cpu_series="Ryzen 7", ram_capacity_gb="16", storage_capacity_gb="512",
                 manufacturer_model_code="FA607NUQ-RL011W", gpu_brand="AMD",
                 gpu_model="RTX 4050", notes=(
                     'SECONDARY_SPEC_CONFLICT manufacturer_model_code: [{"value":'
                     '"FA607NUQ-RL011W","targetIds":["TGT-000023"]},{"value":"FA608UH-RV020W",'
                     '"targetIds":["TGT-000191"]}]. Canonical nonblank value retained.'))
    contributions = [
        {"target_id": "TGT-000023", "model_id": "1", "contribution_status": "CONTRIBUTING",
         "item_name": "Asus TUF A16 FA607NUQ-RL011W 16\" Ryzen 7 16GB 512GB RTX 4050",
         "shopee_model_name": ""},
        {"target_id": "TGT-000023", "model_id": "2", "contribution_status": "EXCLUDED",
         "item_name": "Asus TUF A16 FA608UH-RV020W", "shopee_model_name": ""},
    ]
    return context([row], selected=[{"target_id": "TGT-000023", "rank": "21"}],
                   contributions=contributions, **tables)


def test_broad_name_with_recorded_code_proposes_the_code_as_model_and_keeps_the_name():
    ctx = broad_context(source_model_map=[{"source_model_id": "1", "target_id": "TGT-000023"}])
    (proposal,) = only(build_proposals(ctx, []), cp.TYPE_BROAD_NAME)
    assert (proposal.old_value, proposal.proposed_value) == ("TUF A16", "FA607NUQ-RL011W")
    assert proposal.companion_changes == "series: '' -> 'TUF A16'"
    assert proposal.confidence == cp.MEDIUM and proposal.in_top_100
    assert "FA608UH-RV020W" in proposal.uncertainty          # consolidated other code
    assert "FA608UH-RV020W" in proposal.reviewer_checks
    assert "five-field key changes" in proposal.identity_key_impact
    assert "No collected retailer page" in proposal.uncertainty


def test_broad_name_proposal_notes_an_existing_target_with_the_new_key():
    ctx = broad_context()
    existing = target("TGT-X", brand="ASUS", model="FA607NUQ-RL011W", screen_size_in="16",
                      cpu_series="Ryzen 7", ram_capacity_gb="16", storage_capacity_gb="512")
    ctx = context([*ctx.targets, existing], selected=ctx.selected, contributions=ctx.contributions)
    (proposal,) = only(build_proposals(ctx, []), cp.TYPE_BROAD_NAME)
    assert "already held by TGT-X" in proposal.identity_key_impact


def test_model_name_recorded_as_a_code_is_review_only_and_never_guessed():
    row = target("TGT-58", brand="Lenovo", model="ThinkPad", screen_size_in="13.3",
                 manufacturer_model_code="ThinkPad X390")
    (proposal,) = only(build_proposals(context([row]), []), cp.TYPE_BROAD_NAME)
    assert proposal.proposed_value == "" and proposal.confidence == cp.REVIEW_ONLY
    assert "No code is guessed from a name" in proposal.uncertainty


def test_targets_already_identified_by_code_or_registered_product_name_are_not_proposed():
    coded = target("TGT-1", brand="ASUS", model="FA607NUQ-RL011W",
                   manufacturer_model_code="FA607NUQ-RL011W")
    neo = target("TGT-2", brand="Apple", model="MacBook Neo", manufacturer_model_code="")
    assert only(build_proposals(context([coded, neo]), []), cp.TYPE_BROAD_NAME) == []


# 5. GPU fields --------------------------------------------------------------------------------


def test_gpu_brand_copied_from_the_cpu_brand_is_proposed_with_the_model_vendor():
    row = target("TGT-000023", brand="ASUS", model="FA607NUQ-RL011W", cpu_brand="AMD",
                 gpu_brand="AMD", gpu_model="RTX 4050")
    (proposal,) = only(build_proposals(context([row]), []), cp.TYPE_GPU)
    assert (proposal.field, proposal.old_value, proposal.proposed_value) == (
        "gpu_brand", "AMD", "NVIDIA")
    assert proposal.confidence == cp.HIGH and "likely copied" in proposal.uncertainty


def test_consolidated_different_gpus_are_flagged_but_spelling_variants_are_not():
    def note(*values):
        items = ",".join(f'{{"value":"{v}","targetIds":["T{i}"]}}' for i, v in enumerate(values))
        return f"SECONDARY_SPEC_CONFLICT gpu_model: [{items}]. kept"
    real = target("TGT-A", brand="ASUS", model="FA607NUQ-RL011W", gpu_model="RTX 4050",
                  notes=note("RTX 4050", "RTX 5050"))
    spelling = target("TGT-B", brand="MSI", model="B13UC-3474PH", gpu_model="RTX 3050",
                      notes=note("RTX3050", "RTX 3050"))
    proposals = only(build_proposals(context([real, spelling]), []), cp.TYPE_GPU)
    assert [p.target_ids for p in proposals] == [("TGT-A",)]
    assert proposals[0].confidence == cp.REVIEW_ONLY and proposals[0].proposed_value == ""


def test_discrete_catalog_gpu_against_integrated_only_pages_is_review_only():
    row = target("TGT-G", brand="ASUS", model="FA506NCQ-HN012W", cpu_brand="AMD",
                 gpu_brand="NVIDIA", gpu_model="RTX 3050",
                 manufacturer_model_code="FA506NCQ-HN012W")
    page = offer("https://lf.example/product/x/", "ASUS TUF FA506NCQ-HN012W", status="EXACT",
                 target_id="TGT-G", model="FA506NCQ-HN012W",
                 specs=[("Graphics", "AMD Radeon Graphics (integrated)")])
    (proposal,) = only(build_proposals(context([row]), [page]), cp.TYPE_GPU)
    assert proposal.proposed_value == "" and proposal.confidence == cp.REVIEW_ONLY
    assert "integrated graphics only" in proposal.uncertainty


def test_hybrid_graphics_pages_do_not_contradict_a_discrete_catalog_gpu():
    row = target("TGT-G", brand="HP", model="15-fa2401TX", cpu_brand="Intel", gpu_brand="NVIDIA",
                 gpu_model="RTX 5050", manufacturer_model_code="15-fa2401TX")
    page = offer("https://lf.example/product/x/", "HP Victus 15-fa2401TX", status="EXACT",
                 target_id="TGT-G", model="15-fa2401TX",
                 specs=[("Graphics", ""), ("Intel UHD Graphics", ""), ("NVIDIA GeForce RTX 5050", ""),
                        ("Display", "")])
    assert only(build_proposals(context([row]), [page]), cp.TYPE_GPU) == []


# Evidence loading, outputs, and safety --------------------------------------------------------


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_load_offer_evidence_joins_observations_results_and_extraction_rows(tmp_path):
    url = "https://lf.example/product/hp-laptop-15-fc0933au/"
    observation = {"retailer_id": "laptop_factory", "offer_url": url,
                   "retrieved_at": "2026-10-22T09:00:00Z", "offer_index": "0",
                   "raw_title": "HP Laptop 15-fc0933AU", "availability": "IN_STOCK",
                   "price": "32999.00",
                   "raw_specifications": json.dumps([["Display Size", "15.6″ diagonal"]])}
    result = {"offer_url": url, "retrieved_at": "2026-10-22T09:00:00Z", "offer_index": "0",
              "observation_id": "OBS1", "match_status": "UNMATCHED", "target_id": "",
              "candidate_target_ids": "TGT-1;TGT-2", "expected_target_id": "TGT-1",
              "offer_model": "15-fc0933AU", "offer_screen_size_in": "15.6",
              "offer_cpu_series": "Ryzen 3", "offer_ram_capacity_gb": "8",
              "offer_storage_capacity_gb": "512"}
    evidence = [{"observation_id": "OBS1", "stage": "EXTRACTION", "field": "screen_size_in",
                 "offer_value": "15.6", "source_label": "Display Size",
                 "source_text": "15.6″ diagonal", "evidence_scope": "PRODUCT_SPECIFICATION"},
                {"observation_id": "OBS1", "stage": "MATCH", "field": "screen_size_in",
                 "offer_value": "15.6", "source_label": "", "source_text": "",
                 "evidence_scope": ""}]
    for name, rows in (("observations.csv", [observation]), ("match_results.csv", [result]),
                       ("match_evidence.csv", evidence)):
        write_csv(tmp_path / name, rows)
    (loaded,) = cp.load_offer_evidence([tmp_path])
    assert loaded.candidate_target_ids == ("TGT-1", "TGT-2")
    assert loaded.quote("screen_size_in").text == "15.6″ diagonal"      # extraction row only
    assert len(loaded.statement_list("screen_size_in")) == 1
    assert loaded.spec_values("display") == [("Display Size", "15.6″ diagonal")]
    with pytest.raises(FileNotFoundError):
        cp.load_offer_evidence([tmp_path / "missing"])


def test_a_flattened_label_row_takes_the_next_row_as_its_value():
    page = offer("https://pcc.example/p", "x", specs=[("Graphics", ""), ("NVIDIA RTX 3050", "")])
    assert page.spec_values("graphics") == [("Graphics", "NVIDIA RTX 3050")]


def test_output_dir_must_be_under_data_output_and_never_config(tmp_path):
    (tmp_path / "data" / "output").mkdir(parents=True)
    ok = cp.check_output_dir(tmp_path / "data" / "output" / "catalog_proposals", tmp_path)
    assert ok.name == "catalog_proposals"
    for bad in (tmp_path / "config", tmp_path / "config" / "x", tmp_path / "docs",
                tmp_path / "data" / "history", tmp_path / "data" / "output" / ".." / "history",
                tmp_path.parent):
        with pytest.raises(OutputLocationError):
            cp.check_output_dir(bad, tmp_path)


def test_write_outputs_files_and_counts(tmp_path):
    ctx = broad_context()
    proposals = build_proposals(ctx, [])
    summary = cp.write_outputs(tmp_path / "out", proposals, {"target_master": "x.csv"}, {})
    assert summary["proposals"] == len(proposals) and summary["applied_automatically"] == 0
    rows = list(csv.DictReader((tmp_path / "out" / "catalog_proposals.csv").open(
        encoding="utf-8")))
    assert tuple(rows[0]) == cp.PROPOSAL_COLUMNS
    assert {r["applies_automatically"] for r in rows} == {"FALSE"}
    document = json.loads((tmp_path / "out" / "catalog_proposals.json").read_text("utf-8"))
    assert document["proposals"][0]["target_rows"]["TGT-000023"]["model"] == "TUF A16"
    readme = (tmp_path / "out" / "REVIEW_README.md").read_text("utf-8")
    assert "Nothing was applied" in readme and "TGT-000023" in readme


def test_proposal_ids_are_unique_and_deterministic():
    ctx = broad_context()
    first = [p.proposal_id for p in build_proposals(ctx, [])]
    assert first == [p.proposal_id for p in build_proposals(ctx, [])]
    assert len(first) == len(set(first))


# Real snapshot (skipped when the run folder is absent) -----------------------------------------


@pytest.mark.skipif(not (RUN / "inputs" / "target_master_validated.csv").is_file(),
                    reason="the 2026-10-22 run folder is not in this checkout")
def test_real_snapshot_proposals_and_inputs_are_untouched():
    master = RUN / "inputs" / "target_master_validated.csv"
    digest = hashlib.sha256(master.read_bytes()).hexdigest()
    ctx = cp.load_context(master, run_dir=RUN)
    folders = [p for p in sorted((ROOT / "data" / "output").glob(
        "top100_*/discovery/collection")) if (p / "observations.csv").is_file()]
    proposals = build_proposals(ctx, cp.load_offer_evidence(folders))
    ids = {p.proposal_id for p in proposals}
    assert "CP-FAM-TGT-000013+TGT-000233-target_identity" in ids
    names = {p.target_ids[0]: p for p in only(proposals, cp.TYPE_BROAD_NAME)}
    assert names["TGT-000023"].proposed_value == "FA607NUQ-RL011W"
    assert names["TGT-000041"].proposed_value == "S3407VA-LY135WSM"
    msi = {p.field: p for p in only(proposals, cp.TYPE_MSI) if p.target_ids == ("TGT-000021",)}
    assert {"ram_type", "vram", "screen_size_in"} <= set(msi)
    assert msi["vram"].proposed_value == "4GB" and msi["ram_type"].proposed_value == "DDR4"
    gpu = {(p.target_ids[0], p.field) for p in only(proposals, cp.TYPE_GPU)}
    assert ("TGT-000023", "gpu_brand") in gpu
    assert all(p.row()["applies_automatically"] == "FALSE" for p in proposals)
    assert not any(p.proposed_value == "15.6" and p.old_value != "15" for p in proposals)
    assert hashlib.sha256(master.read_bytes()).hexdigest() == digest
