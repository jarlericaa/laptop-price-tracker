"""Offline replay of saved collections: synthetic fixture pages and throwaway stores only."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from scraping_fakes import no_network  # noqa: F401
from test_extraction import LF_TARGET, fixture_offer
from test_match_run import TARGET_ROWS, write_observations, write_rows
from test_target_master import target_values
from test_verified_top import load_script

from laptop_tracker.match_run import NumberedObservation
from laptop_tracker.matching import ExactMatcher, MatchStatus
from laptop_tracker.observation_store import ObservationStore, observation_id
from laptop_tracker.replay import (
    conflict_check,
    match_items,
    price_comparability,
    record_differences,
    reparse_saved_pages,
)

pytestmark = pytest.mark.usefixtures("no_network")
FIXTURES = Path(__file__).resolve().parent / "scraping_fixtures"


def saved(tmp_path: Path, name: str, fixture: str):
    observation = fixture_offer(name)
    html = tmp_path / "html"
    html.mkdir(exist_ok=True)
    body = (FIXTURES / fixture).read_bytes()
    assert hashlib.sha256(body).hexdigest() == observation.source.content_sha256
    (html / f"{observation.source.content_sha256}.html").write_bytes(body)
    return NumberedObservation(2, observation), html


def test_saved_html_reparses_with_the_original_retrieval_time(tmp_path: Path) -> None:
    item, html = saved(tmp_path, "lf_sale", "laptop_factory/lf_product_sale.html")
    [replayed], failures = reparse_saved_pages([item], html)
    assert failures == []
    assert replayed.observation.retrieved_at == item.observation.retrieved_at
    assert observation_id(replayed.observation) == observation_id(item.observation)
    assert record_differences(item.observation, replayed.observation) == []


def test_missing_or_altered_bodies_are_failures_not_guesses(tmp_path: Path) -> None:
    item, html = saved(tmp_path, "lf_sale", "laptop_factory/lf_product_sale.html")
    path = html / f"{item.observation.source.content_sha256}.html"
    path.write_bytes(path.read_bytes() + b" ")
    replayed, [failure] = reparse_saved_pages([item], html)
    assert (replayed, failure.code) == ([], "BODY_HASH_MISMATCH")
    path.unlink()
    assert reparse_saved_pages([item], html)[1][0].code == "BODY_MISSING"


def test_unchanged_matches_and_price_rule_for_laptop_factory(tmp_path: Path) -> None:
    item, html = saved(tmp_path, "lf_sale", "laptop_factory/lf_product_sale.html")
    matcher = ExactMatcher([LF_TARGET])
    [(_, before)] = match_items(matcher, [item])
    [(_, after)] = match_items(matcher, reparse_saved_pages([item], html)[0])
    assert before.status is after.status is MatchStatus.EXACT
    assert before.target_id == after.target_id == "TGT-000001"
    assert price_comparability(item.observation, before) == "COMPARABLE"


def test_conflict_check_uses_a_throwaway_copy(tmp_path: Path) -> None:
    item, _ = saved(tmp_path, "lf_sale", "laptop_factory/lf_product_sale.html")
    store_path = tmp_path / "store.sqlite3"
    with ObservationStore.open(store_path) as store:
        store.import_observations(
            [item.observation], source_label="t", imported_at=datetime.now(UTC)
        )
    work = tmp_path / "work"
    work.mkdir()
    assert conflict_check(store_path, [item.observation], work).startswith("ACCEPTED: 0 new, 1")
    # A stored record re-parsed by a newer adapter keeps its identity but not its content.
    pcc_item, _ = saved(tmp_path, "pcc_variants", "pcc/pcc_product_variants_sale.html")
    assert pcc_item.observation.details.scoped_evidence
    with ObservationStore.open(store_path) as store:
        store.import_observations(
            [pcc_item.observation], source_label="t", imported_at=datetime.now(UTC)
        )
    before = store_path.read_bytes()
    altered = replace(
        pcc_item.observation,
        details=replace(pcc_item.observation.details, scoped_evidence=()),
        source=replace(pcc_item.observation.source, adapter_version="pcc-1"),
    )
    assert observation_id(altered) == observation_id(pcc_item.observation)
    outcome = conflict_check(store_path, [altered], work)
    assert outcome.startswith("REFUSED")
    assert store_path.read_bytes() == before
    assert list(work.iterdir()) == []


def test_replay_command_writes_both_paths_and_never_imports(tmp_path: Path) -> None:
    _, html = saved(tmp_path, "lf_sale", "laptop_factory/lf_product_sale.html")
    observations = write_observations(tmp_path / "observations.csv", "lf_sale")
    target_master = write_rows(tmp_path / "target_master.csv", target_values(TARGET_ROWS[0]))
    out = tmp_path / "replay"
    args = ["--observations", str(observations), "--target-master", str(target_master),
            "--html-dir", str(html), "--output-dir", str(out)]
    assert load_script("replay_extraction").main(args) == 0
    summary = json.loads((out / "replay_summary.json").read_text(encoding="utf-8"))
    assert summary["observation_replay_statuses"] == {"EXACT": 1}
    assert summary["html_replay_statuses"] == {"EXACT": 1}
    assert summary["reparsed_records_imported"] is False
    assert (out / "html_replay" / "reparsed_observations_NOT_FOR_IMPORT.csv").is_file()
    assert load_script("replay_extraction").main(args) == 1  # Existing folder.
