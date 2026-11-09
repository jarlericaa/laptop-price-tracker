# Catalog binding audit and P1 publication diagnostics (2026-11-03)

Two offline deliverables prepared from the union run `data/output/union_2026-11-03/`. Nothing in
either one writes a Sheet, a catalog table, a mapping, an approval or a history file; no retailer
is contacted. Both scripts write only into their output folder under `data/output/` and refuse any
other location (and `config/`).

## 1. Catalog binding audit: the correction queue

`build_catalog_correction_queue.py` audits every mapped variation of the eligible pool (205 of
1,571) and every target any `SOURCE_MODEL_MAP` row points to (476 rows, 241 targets), and writes a
queue for the owner. Library: `src/laptop_tracker/catalog_corrections.py`.

```
python scripts/build_catalog_correction_queue.py            # writes catalog_corrections/ next to the run
python scripts/build_catalog_correction_queue.py --dry-run  # counts only, writes nothing
```

Inputs (read only): `shopee_variations/shopee_variation_ranking.csv`, and from `inputs/` the
validated `target_master`, `source_model_map` and `match_review` copies. Output, in
`<run>/catalog_corrections/`: `catalog_correction_queue.csv` (the working file), `.json` (the same
rows with the full evidence, the input SHA-256s and the field-by-field readings) and `.md`
(counts and the highest-priority rows).

### What is checked

| Class | Meaning |
|---|---|
| `BRAND_MISMATCH` | The parent title names one brand (`Hp Laptop`) and the target's brand, or the brand its model name states (`Dell Latitde 5300` with a blank brand), is another. |
| `VARIATION_CONTRADICTS_TARGET` | The variation's own text states a value the target records differently: the four compared fields of `variation_specs` (CPU series, screen, RAM, storage), read exactly as the ranking reads them. In addition an unlabeled `RAM/storage` pair after a processor token (`I3-7GEN/4GB/128GB`, `16+512G`) is read for RAM and storage; those readings are shown as `variation_pair` (the catalog's own capacity reviews read the same convention). A parent title is never read as a variation. |
| `SIBLING_FAN_IN` | Variations of one parent (`shop_id\|item_id`) are bound to targets of different brands, or to different models that neither the title nor the variations name. One `PARENT_LISTING` row per parent, and the class is noted on every sibling. A multi-model listing that names each model per variation (the ThinkPad X280/X390/X13 listings) is not flagged. |
| `TARGET_INTERNAL_CONFLICT` | The target contradicts itself: `cpu_model` against `cpu_series`, brand against the brand its model names, incomplete core identity, inactive. Uses `verified_top._target_issue` (its reason code is shown as "verified_top reason today") plus the same field checks without its `LISTING_ONLY` short circuit. One `TARGET` row per defective target. |
| `MODEL_NOT_STATED_BY_LISTING` | A specific target model (`HP Probook 450 G5`) that neither the title nor the variation name states. |
| `LISTING_ONLY_BINDING` | A variation bound to a `LISTING_ONLY` target (tracked as one listing). |

A binding can carry several classes; `finding_class` is the first in the order above and
`all_finding_classes` lists them all. Scopes: `VARIATION_BINDING` (one row per flagged mapped
variation), `PARENT_LISTING` (fan-in, one row per parent), `TARGET` (one row per defective target,
including mapped ids outside the pool: all 271 such ids were checked at target level, and no target
defect exists that is not also bound to a pool variation).

### Columns

`queue_id`, `scope`, `priority` (P1 when the variation's `eligible_rank` is within 100 or it is
selected, P2 within 300, else P3; parent and target rows use their best bound variation),
`finding_class`, `all_finding_classes`, `eligible_rank`, `ranking_gmv_usd` (summed for parent and
target rows), `selection_status`, `source_model_id`, `source_key`, `shop_name`, `listing_title`,
`variation_name`, `current_target_id`, `target_brand` (recorded, or inferred from the model name,
and the finding says which), `target_model`, `target_specs`, `conflicting_fields` (each field with
both values and the quoted text), `quoted_variation_text`, `listing_brand`, `sibling_summary`,
`existing_targets_with_stated_identity` (information only: same-brand configuration targets that
record every cpu/RAM/storage value the variation states; the variation names no model or screen, so
this is never a mapping), `binding_basis` (`CURRENT`, `STALE` or `UNRECORDED`: whether the target's
own notes name the variation it is bound to today, and which variation they name instead),
`target_notes_excerpt` (the two newest dated notes), `match_review` (status, date, whether its
recorded variation is the current one; or `NO_MATCH_REVIEW_ROW`), `recommended_owner_action`,
`uncertainty`, `applies_automatically` (always `FALSE`).

### Result on the real data

29 queue rows: 17 `VARIATION_BINDING`, 2 `PARENT_LISTING`, 10 `TARGET`. Priority: 15 P1, 13 P2, 1 P3.
Primary class of the variation rows: `BRAND_MISMATCH` 3, `VARIATION_CONTRADICTS_TARGET` 5,
`SIBLING_FAN_IN` 4, `LISTING_ONLY_BINDING` 4, `TARGET_INTERNAL_CONFLICT` 1. Counting every class a
variation carries: brand mismatch 3, contradiction 6, fan-in 9, target conflict 10, model not stated
2, listing-only 12. The 17 flagged variations carry USD 176,453 of ranking GMV.

All 17 are variations that the ranking already excludes (5 `MAPPED_TARGET_CPU_MODEL_CONFLICT`,
12 `MAPPED_TARGET_LISTING_ONLY`), and all but one belong to five FicoPcTradingCorp. refurbished
listings with generic titles (two "Hp Laptop", two "Fujitsu Laptop", one "Toshiba Laptop"); the
other is an ASUS variation bound to a `LISTING_ONLY` target. **None of the 188 `MAPPED_VERIFIED`
variations, and none of the selected Top 100, has a finding.** The Top 100 is therefore not
affected; for the excluded set the evidence suggests that many of these are wrong bindings rather
than right bindings that are merely blocked, which is the owner's call.

The case that was reported: `CC-VAR-245580060344` (HP "Intel i7/i5/i3 & Celeron" listing, variation
`5:I3-7GEN/4GB/128GB`, eligible rank 43, USD 23,805) is bound to TGT-000238, a Dell Latitude 5300
with Core i5 / 8GB / 256GB and `cpu_model` "Intel i7". Findings: brand HP vs Dell; CPU series
Core i3 vs Core i5; RAM 4 vs 8 and storage 128 vs 256 (from the unlabeled pair `4GB/128GB`); the
target's own CPU contradiction; the target was reviewed from `11:I5-10GEN/8G/256GB`, a different
variation of the same model id (the seller's variation changed after the review). The seven bound
variations of that listing resolve to Dell, HP and Toshiba targets (`CC-PAR-780290062-21327704634`).
`cpu_model` "Intel i7" on all ten defective targets is the first processor of the seller's
multi-option title, not evidence for any configuration. The queue does **not** propose clearing it: the action is to
review the binding and "not edit TARGET_MASTER row 238 to fit".

### What the owner must decide

1. For each `BRAND_MISMATCH`/`VARIATION_CONTRADICTS_TARGET` row: keep the binding, unmap the
   variation, or identify a correct target from the seller's own label. Nothing was changed.
2. For each `TARGET` row: which of the target's conflicting fields is right (the notes record only
   the product it was reviewed from). Correct TARGET_MASTER yourself; the queue never edits it.
3. For `LISTING_ONLY_BINDING`: whether a one-listing target is intended for one variation of a
   multi-configuration listing, or the variation needs its own `CONFIGURATION` target.
4. Whether the unlabeled-pair reading (`4GB/128GB`, `16+512G`) is accepted as evidence for the
   audit. It only adds fields to a finding and never selects or excludes a variation.
5. Whether the ranking itself should apply the pair reading and brand check (not done: those
   modules are outside this change).

Limits: brand detection uses a closed list of laptop brands and product lines (a title naming two
brands resolves to none); a target with no brand and no brand in its model is reported as unknown,
not as a mismatch; mapped ids outside the eligible pool have no listing title in the inputs, so only
their target-level checks apply.

## 2. Publication diagnostics (preview only)

`build_publication_diagnostics.py` prepares the complete audit content that the live P1 tabs
(headers only today) should hold, plus a publication plan. Library:
`src/laptop_tracker/publication_diagnostics.py`.

```
python scripts/build_publication_diagnostics.py             # writes <run>/diagnostics/
python scripts/build_publication_diagnostics.py --discovery-dir data/output/weekly_runs/<run>/discovery
python scripts/build_publication_diagnostics.py --dry-run
```

`--discovery-dir` defaults to the newest `weekly_runs/*/discovery` with a `search_jobs.csv`.
`--correction-queue` (default: the queue of section 1 when present) links mapped exclusions to
their queue rows. Output in `<run>/diagnostics/`:

| File | Rows | Content |
|---|---:|---|
| `eligibility_variations.csv` | 1,571 | Every eligible variation of the union pool: Phase 1 rank, shop, item, variation, `present_in_m_minus_1`/`present_in_mtd`, month statuses (`PRESENT` / `NO_RECORD_TREATED_AS_ZERO`), M-1/MTD/ranking GMV, price, price basis, period month and record date, selection status, exclusion reason, target and mapping status. |
| `eligibility_not_eligible_listings.csv` | 1,151 | The 1,000 review-pending and 151 excluded Phase 1 listings with `disposition`, the reason (the rule that matched is appended) and their GMV and price. Review rows carry no month flags (the Phase 1 review file has none). |
| `mapping_exclusions_diagnostic.csv` | 1,471 | Every eligible variation that is not selected: 1,366 `UNMAPPED`, 12 `MAPPED_TARGET_LISTING_ONLY`, 5 `MAPPED_TARGET_CPU_MODEL_CONFLICT`, 88 `QUALIFIED_BELOW_SELECTION_LIMIT`. Each has the ranking's own evidence, the variation's stated specifications and conflicts, what `MATCH_REVIEW` holds beside it (never used to select), whether it is a selection-audit representative, its correction-queue row and the suggested action. |
| `search_coverage.csv` | 200 | One row per target x retailer of the discovery run: terminal outcome, queries planned/run, pages sent/from cache/not sent, search results, candidates (rejected, not selected, collected, other, plausible), inspections and their outcomes, requests avoided by reuse, caps reached; every other column of `search_jobs.csv` is carried through unchanged. |
| `diagnostics_summary.json` | | Row counts, counts by selection status/reason, search coverage by outcome and retailer, run budget use (used vs limit), input SHA-256s and four self-checks (all pass). |
| `publication_plan.json`, `README.md` | | The intended tabs: the three native P1 tabs from the run's Phase 1 files (1,571 / 1,000 / 151 rows) and four new tabs (`P1_ELIGIBILITY_AUDIT`, `P1_NOT_ELIGIBLE_AUDIT`, `P1_MAPPING_EXCLUSIONS`, `P1_SEARCH_COVERAGE`). Every entry is `PREVIEW_ONLY_NOT_WRITTEN`. |

Search coverage reads outcomes dynamically. Any `search_jobs.csv` column whose name says outcome,
stop reason, coverage or terminal and whose values are short categories is reported and grouped
by whatever values it holds (`outcome_columns` per row, counts in the summary). The terminal outcome
is the first present of `terminal_outcome`, `final_outcome`, `discovery_terminal_outcome`,
`discovery_outcome`, else any other outcome column, else `stop_reason`, else `UNRECORDED`; the
column used is named in `terminal_outcome_column`. No value list is fixed, so the older vocabulary
(`QUERY_PLAN_EXHAUSTED`, `SUITABLE_OFFER_FOUND`, ...) and a newer one need no change here. On the
current run: 177 `QUERY_PLAN_EXHAUSTED`, 21 `SUITABLE_OFFER_FOUND`, 2
`PLAUSIBLE_CANDIDATE_NOT_COLLECTED`; 123 rows hit a cap (118 result cap, 20 candidate limit, 5
per-round candidate limit, counted per row); 348 requests avoided by reuse; run budget not reached
(311 of 6,000 requests). `requests_avoided_by_reuse` is search pages from cache plus inspections
served from saved evidence, and is blank when the run records neither.

### What the owner must decide

1. Approve publishing the three native tabs (replacing their headers-only content) and approve the
   four new tab names. The writer refuses tabs outside `sheets_writer.ALLOWED_OUTPUT_TABS`, which
   must be extended before any write; this change did not touch it.
2. Whether `mapping_exclusions_diagnostic.csv` (1,471 rows, a long text per row) should be one tab
   or be limited to the unmapped rows with a complete core identity in MATCH_REVIEW.
3. Re-run after the discovery run's terminal-outcome vocabulary is final; the file needs no change.

Tests: `tests/test_catalog_corrections.py`, `tests/test_publication_diagnostics.py` (synthetic
data; brand mismatch, sibling fan-in, contradiction, internal conflict, never auto-applied, inputs
never mutated, unknown outcome vocabulary, writes only into the output folder).

## 3. Before/after report (2026-11-04)

`build_before_after_report.py` compares two run folders from their own artifacts (read only; no
retailer, Sheet or history store). Library: `src/laptop_tracker/before_after.py`.

```
python scripts/build_before_after_report.py \
  --before-dir data/output/weekly_runs/20261103T023824Z \
  --after-dir  data/output/weekly_runs/<new run>          # writes <after>/before_after/
python scripts/build_before_after_report.py --before-dir ... --after-dir ... --dry-run   # print only
```

Reads `shopee_variations/shopee_variation_run_summary.json`, `variation_reports/variation_coverage.json`,
`discovery/discovery_summary.json` and `discovery/search_jobs.csv` of each folder (a missing file is
listed, not guessed) and writes `before_after_report.json/.md` (under `data/output/` only). Sections:
eligible, qualifying and selected variations per month and their union; exclusions by reason; selected
variations, distinct contexts and the shortfall; **matching coverage and alternative coverage as two
separate sections**; discovery jobs by terminal outcome; the six owner-named misses (PC Central
X1504VA and AG14-71M, PC Express MacBook Neo 256/512, DataBlitz MacBook Air M5 16/512 and 16/1TB)
with their outcome before and after; jobs ending `PAGE_FETCHED_IDENTITY_UNRESOLVED`; requests avoided
by reuse. A figure a run never wrote is `NOT_RECORDED`, never zero (the run before the union rule has
no month counts; a run before the 2026-11-03 vocabulary has `discovery_outcome` only, whose
`QUERY_PLAN_EXHAUSTED` maps to no class). "Recovered" means class `0_SUITABLE_OFFER` only: a confirmed
offer excluded by stock or terms, an unresolved page and a deferred fetch are reported under their own
status. Tests: `tests/test_before_after.py`.
