# Close-alternative retailer comparisons (2026-10-27)

**Intentional policy change, 2026-10-27:** a retailer offer with **up to two total established
differences across the five core dimensions plus GPU** may appear as a clearly labelled
alternative comparison. Product identities are preserved: nothing here assigns a target, remaps a
Shopee listing, merges target IDs, edits a catalog value or changes a stored match.

Everything below was built and checked offline. No live discovery run, Google Sheets write,
catalog, mapping or history change was made.

## 1. Policy

| Differences | Result |
|---|---|
| none established | not an alternative (the existing exact / listed-spec rules decide a `MATCH`) |
| RAM only | `ALT 1 · RAM` |
| RAM + storage | `ALT 2 · RAM, Storage` |
| GPU only | `ALT 1 · GPU` |
| CPU + GPU | `ALT 2 · CPU, GPU` |
| RAM + storage + GPU | excluded: three differences |

The six dimensions share one budget of two (`alternatives.MAX_DIFFERENCES`); GPU has no separate
allowance. Each dimension is compared once: several changed CPU attributes (series, exact
processor, brand, generation, cores) are one CPU difference, several changed GPU attributes
(model, brand, type, VRAM) are one GPU difference, and a changed CPU series together with a
changed exact processor is also one. Tags use the fixed order Model, Screen, CPU, RAM, Storage,
GPU.

### Four states per dimension

`AGREES`, `DIFFERS`, `UNKNOWN` and `CONFLICTING_EVIDENCE`, each stored with the target value, the
retailer value, both normalized values, the comparison basis, the field-level evidence references
and a sentence of explanation (`weekly_price_alternatives.csv` →
`comparison_detail_json`).

- A *difference* is a consistent disagreement between target and retailer. Target RAM 16GB and a
  retailer that consistently states 8GB: a RAM difference.
- A contradiction *inside* the retailer's evidence is not an ordinary difference: a title saying
  16GB over a selected-offer specification saying 8GB, two screen sizes, conflicting model codes
  (`MODEL_CLAIM_CONFLICT`), several GPUs not bound to the offer. The offer is a `RECHECK`.
  Existing evidence-scope rules (a confirmed selected variant, maximum/optional clauses) still
  resolve what they legitimately resolve before the comparison.
- A missing core value is never agreement. All five core dimensions must be established on both
  sides, at the approved granularity; otherwise the offer is a `RECHECK`
  (`REQUIRED_<DIMENSION>_NOT_ESTABLISHED`). Three or more established differences exclude the
  offer even when something else is unknown.
- Zero established differences never create an exact match.

### Dimension rules

| Dimension | Rule |
|---|---|
| Model | Same manufacturer code agrees; a different established code inside the family is one difference. A product-name target (MacBook Air) compares names. A **broad-family target** keeps the listed-spec treatment: compared at family level (`FAMILY_ONLY`), the specific model stays explicitly unknown (`SPECIFIC_MODEL_UNKNOWN`); a code the catalog records for the target is compared with the offer's. A missing code for a code target is unknown. |
| Screen | `core_value_key` equality: 15 vs 15.6 is one difference. Nothing is rounded, aliased or edited. |
| CPU | Series differs (`Core i5` / `Core 5` / `Core Ultra 5` / `Apple M5` / `Apple M5 Pro` stay distinct): one difference. Series equal and both exact processors established but different: one difference. Series equal and an exact processor missing on either side: `AGREES` on a visible limited basis `SERIES_ONLY` with the limitation `CPU_EXACT_PROCESSOR_UNKNOWN` — not exact equality. |
| RAM, storage | Installed capacity of the selected offer only. Maximums, upgrades, GPU memory, bundled external storage and unselected variants never reach the offer's values (the extractor's evidence-scope rules). Counted separately. |
| GPU | Both stated: different canonical GPU is one difference (RTX 5050 vs RTX 4050). Either side unstated: `UNKNOWN` with `GPU_UNKNOWN` — never agreement, never a difference, never copied from the other side. Several unbound GPUs on the page: `CONFLICTING_EVIDENCE` when the target records a GPU. |
| Other | Blocking secondary fields outside the six dimensions (resolution, panel type, refresh rate, RAM type, drive type/layout, recorded part number) keep their existing policy: a verified conflict is a `RECHECK` (`MATERIAL_CONFLICT_<FIELD>`). Warning-class fields (OS, colour, …) are carried as warnings. |

### Candidate scope: brand and recognized family

Only the same brand and a recognized product family are compared.
`config/product_family_registry.csv` (read by `product_families.py`) lists each reviewed family
with its approved naming variations (`TUF Gaming; TUF`, `Vivobook; Vivo Book`) and the families a
broad family `includes` (`Aspire` includes `Aspire Go` and `Aspire Lite`; the reverse is not
true). A text names a family only through whole-word names; the most specific wins; MacBook Air
and MacBook Pro are different families. An unlisted family is `FAMILY_NOT_RECOGNIZED`, never
inferred from a shared word. The same manufacturer model code always names the same product. The
registry was seeded from the catalog families and the existing product-name registry; **the owner
should review it** (it is a new controlled list, `reviewed_on` 2026-10-27).

## 2. Identity and association (nothing is overwritten)

Four concepts stay separate: the offer's stored product identity (`match_status`/`target_id`), its
exact target assignment if any, the target(s) that initiated discovery (`search_context`), and
the targets for which it qualifies as an alternative. An offer assigned to TGT-000002 can be an
alternative for TGT-000001. The alternative association carries the target, offer key,
observation ID, comparison basis, difference count, differing dimensions, target-versus-offer
values, unknown fields, limitations, evidence and `close-alternatives/2026-10-27`.

Alternatives are *not* a matching rule: `alternatives.py` is outside `RULE_MODULES`, the matching
context ID and `MATCHING_RULES_VERSION` are unchanged, and no stored match is rewritten or
re-recorded. TARGET_MASTER, SOURCE_MODEL_MAP, sales aggregation and the Top 100 ranking are not
read differently. Only observations of the report week are evaluated; earlier observations keep
their timestamps and are never priced again.

## 3. Reporting and offer selection (`price_tracking.py`)

- **Main display per retailer cell:** an eligible matching configuration (exact or listed-spec,
  unchanged); otherwise the best `ALT 1`; otherwise the best `ALT 2`; within the level the lowest
  eligible price under the existing tie-break. A cheaper `ALT 2` never displaces a matching
  offer. The cell's status is `SELECTED` (matching) or `ALTERNATIVE_SELECTED`;
  `<code>_Comparison_Tag` is `MATCH`, `ALT n · …` or `RECHECK`.
- **Matching minimums and counts are unchanged** (`Lowest_Price`, `Lowest_In_Stock_Price`,
  `Comparable_Retailer_Count`, `selected_retailer_prices`, series rows).
- **Alternative-only minimums** (`Lowest_Alternative_Price*`, `Lowest_Alternative_In_Stock_Price*`)
  are taken over the full eligible alternative pool of every retailer, including offers hidden by
  the main display and alternatives of retailers that also have a matching offer. They expose the
  winning offer's tag, difference count, differences, unknown fields and observation ID. Each
  retailer is counted once per category (`Alternative_Retailer_Count`,
  `Comparison_Retailer_Count`, `<code>_Alternative_Offers`); colour variants are not extra
  retailers. Out-of-stock offers cannot win an in-stock minimum. Stock, bundle, purchase-condition
  and freshness protections are the same price rules as for matches.
- **New report files:** `weekly_price_alternatives.csv` (one row per target–retailer-offer
  association, with price eligibility and whether it is shown) and
  `alternative_evaluations.csv` (every evaluation: alternatives, rechecks, exclusions, for audit).
  Columns are appended at the right of the comparison, selections and history files.
- **Review queue:** an unassigned offer that qualifies as an alternative is listed as
  `CLOSE_ALTERNATIVE ALT n · … for TGT-… (differences)` instead of an unresolved
  "different configuration" row; contradictions, missing evidence, exclusions and other genuine
  review cases are unchanged.
- `track_prices.py --no-alternatives` reproduces the matching-only report.

## 4. Discovery (before final matching)

`discover_products.py` no longer skips a candidate merely because its search *title* states
another configuration. `product_discovery.title_alternative_potential` counts established title
differences once per dimension (screen, CPU, RAM, storage, GPU, and a different model code of
the same brand and recognized family); a field the title does not state is unknown. 1–2
differences: retained and inspected within the existing limits (`COLLECT_CLOSE_ALTERNATIVE_CANDIDATE`
for family listings and same-family different-model results; `COLLECT_CHECK_CONFIGURATION` for
code listings as before). More than two: `SKIPPED_TOO_MANY_TITLE_DIFFERENCES` with the
dimensions named. Partial titles are inspected without assuming agreement. Candidates are
prioritized: matching configurations, then one-difference, then two-difference alternatives, then
relevant candidates with incomplete title evidence (`alternative_tier` in `candidates.csv`).
Multi-variant pages whose variants differ from the target only in RAM/storage collect one variant
per distinct (RAM, storage) configuration within the two-difference budget
(`COLLECT_VARIANT_ALTERNATIVE`, `--max-alternative-variants-per-candidate`, default 1; colours
are not extra alternatives, and position or price never selects a variant).
Accessory/product-type filters, request ceilings, shared retailer cooldowns, checkpointing and
response reuse are unchanged. The page's own evidence decides eligibility; a title never supplies
a price.

## 5. Sheets publication (preview only)

See the `pilot-sheets-7` section of [PRICE_TRACKING_OUTPUT_SCHEMA.md](PRICE_TRACKING_OUTPUT_SCHEMA.md)
and [WEEKLY_WORKFLOW.md](WEEKLY_WORKFLOW.md) (Step 8). Publisher `pilot-sheets-7` adds the
`PILOT_PRICE_ALTERNATIVES` tab, comparison-status colours on `PILOT_PRICE_OVERVIEW` and
`PILOT_PRICE_SUMMARY` (light green MATCH, light yellow ALT 1, light orange ALT 2, light gray
RECHECK) with a text-label column and legend so colour is never the only signal, cell notes with
the matching basis, actual configuration, differences, unknowns, stock and the link to the
detailed record, and new columns strictly at the right of the comparison tab. **No write to
Google Sheets was made.**

## 6. Reproduction (all offline)

Inputs are the saved 2026-10-26 weekly run (`RUN=data/output/weekly_runs/20261026T025827Z`) and
disposable copies of the history store; the production store is never opened for writing (its
SHA-256 was identical before and after, `3fa37646…`).

```bash
RUN=data/output/weekly_runs/20261026T025827Z
OUT=data/output/alternatives_2026-10-27
mkdir -p $OUT/disposable
for variant in before after; do
  cp data/history/price_history.sqlite3 $OUT/disposable/store_$variant.sqlite3
  extra=""; [ $variant = before ] && extra="--no-alternatives"
  python scripts/track_prices.py --store $OUT/disposable/store_$variant.sqlite3     --target-master $RUN/inputs/target_master_validated.csv     --source-model-map $RUN/inputs/source_model_map_validated.csv     --match-review $RUN/inputs/match_review_validated.csv     --selected-list $RUN/verified_top_100/top_100_verified_configurations.csv     --contributions $RUN/verified_top_100/verified_configuration_contributions.csv     --sales-ranking $RUN/inputs/eligible_model_ranking.csv     --week-start 2026-10-26 --as-of 2026-10-26T06:33:50.494649+00:00     --discovery-dir $RUN/discovery     --search-context $RUN/discovery/collection/match_results.csv     --output-dir $OUT/replay_$variant $extra       # no --observations: nothing is imported
done
python scripts/compare_alternative_replay.py --before $OUT/replay_before --after $OUT/replay_after   --output-dir $OUT/comparison --discovery-summary $OUT/discovery_reassessment_summary.json
python scripts/audit_alternatives.py --report-dir $OUT/replay_after   --store $OUT/disposable/store_after.sqlite3   --target-master $RUN/inputs/target_master_validated.csv --output-dir $OUT/audit
python -m pytest -q tests/test_alternatives.py tests/test_alternative_reports.py   tests/test_alternative_discovery.py tests/test_alternative_publication.py
```

**Publication preview (no Google Sheets access).** The plan is produced by the normal command, which
reads the destination tabs with the read-only scope:

```bash
python scripts/publish_prices.py --preview --report-dir $OUT/replay_after   --pilot-scope $RUN/scope_top_100.csv --collection-dir $RUN/discovery/collection   --selected-list $RUN/verified_top_100/top_100_verified_configurations.csv   --output-root $OUT/publication_preview
```

The preview in `$OUT/publication_preview/` was instead produced offline: the same
`publish_prices.main([... "--preview" ...], spreadsheet_factory=<in-process FakeSpreadsheet from
tests/test_sheet_publication.py>)`, and a dry-run `--write-sheets` against that fake (SUCCESS, 0
readback issues). Read `plan_<tab>.csv` and `plan_<tab>_notes.csv` there. Publishing for real
(`--write-sheets`) is a separate, user-approved step.

The discovery reassessment (`discovery_reassessment.csv`) re-reads the saved
`$RUN/discovery/candidates.csv` with `product_discovery.title_alternative_potential_from_row`;
it simulates the title gate only.

## 7. Replay of the 2026-10-26 run

The 2026-10-26 run was throttled by two retailers (6 comparable prices), so the absolute numbers
are small; the rules, not the volume, are what was checked.

| | Before (matching only) | After (close alternatives) |
|---|---|---|
| Matching cells (MATCH) | 6 (DataBlitz 2, PC Express 4) | 6, **identical** (price, status, link, stock, basis) |
| Matching minimums, in-stock minimums, matching counts | — | **identical** for all 86 targets |
| Alternative cells shown | 0 | 13 (`ALT 1` 5, `ALT 2` 8; all PC Express) |
| Targets with a price of either kind | 6 | 19 (6 matching + 13 alternative-only) |
| Targets with an eligible alternative anywhere | — | 18 |
| Alternative associations | — | 32 (13 shown; 7 at a retailer that also has a matching offer) |
| Blank cells resolved by an alternative | — | 13: 8 `CANDIDATE_NOT_CONFIRMED`, 3 `NO_RELEVANT_CANDIDATE`, 2 `CANDIDATE_NOT_FETCHED` |
| Targets whose lowest alternative undercuts the matching minimum | — | 4 |
| Evaluations still a `RECHECK` | — | 130 (missing evidence: CPU 123, RAM 121, screen 119, storage 119, model 49; contradictory model identifiers 5; display-type conflict 2) |
| Excluded for more than two differences | — | 42 (27 with three, 15 with four) |
| Excluded for brand or family | — | 392 (369 another family, 21 family not in the registry, 2 brand not stated) |

Associations by tag: `ALT 1 · Storage` 6, `ALT 2 · Screen, CPU` 8, `ALT 2 · Screen, Storage` 4,
`ALT 2 · CPU, Storage` 4, `ALT 1 · Screen` 2, `ALT 1 · CPU` 2, `ALT 1 · Model` 2,
`ALT 2 · Model, Screen` 2, `ALT 1 · RAM` 1, `ALT 2 · CPU, RAM` 1.

**Audit.** `audit_alternatives.py` re-extracted all 32 alternatives from their saved pages
independently of the evaluator: **32 PASS, 0 FAIL** (retailer values, catalog values, differing
dimensions and tag, price, stock, observation identity and time inside the week).

**Specific cases.** The two MacBook Neo targets (256GB / 512GB) keep their distinct exact
identities and are each a storage alternative for the other (`ALT 1 · Storage`; hidden behind the
matching DataBlitz offers). Pages that contradict themselves stay rechecks: 24 saved observations
carry `MODEL_CLAIM_CONFLICT` (the PC Express TUF case), and the MacBook Air pages that state 13 in
the title and 13.6 in the specification are `CONFLICTING_EVIDENCE` for screen, never a screen
difference. No new exact match was created and no stored match changed.

**Discovery.** Reassessing the 4,881 candidates of the run's `candidates.csv` (title gate only):
**521 candidates are newly eligible for inspection** (292 one-difference, 229 two-difference;
PC Express 308, Octagon 82, Laptop Factory 57, DataBlitz 56, PC Central 18; 194 family listings
and 327 same-family different-model results), 148 relevant candidates with more than two title
differences are skipped with a reason, and 64 matching-configuration and 91 incomplete-title
candidates keep their tiers. The per-target candidate limit still bounds how many are fetched.

## 8. Limitations

- Alternatives are evaluated for the report week only, against the selected configurations; the
  price series and `Lowest_Price` history are matching-only.
- `OFFERS_DISAGREE_*` warnings compare matching prices only.
- The family registry is conservative: a target whose family is not listed is never compared.
- A broad-family target accepts any offer naming its family (as in the listed-spec policy), so
  one retailer model can be an alternative for several family targets.
- Title-supplied values (`TITLE_SUPPLIED_*`, an existing evidence rule) can establish a
  dimension; `audit_alternatives.py` reports which dimensions of each alternative are
  title-supplied and which come from the page's specification.
- Live behaviour of the new discovery gate under the per-target candidate limit has not been
  measured; the offline reassessment only simulates the title gate.
