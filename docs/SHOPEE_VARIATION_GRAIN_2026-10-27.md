# Top 100 eligible mapped Shopee variations (schema `shopee-variation-sheets-1`, 2026-10-27)

> **Updated 2026-11-03 (owner decision).** The Top 100 now holds only eligible variations with a
> verified mapping (`MAPPED_VERIFIED`), the filter runs before the 100-row limit, and unmapped or
> untrusted variations are no longer listed. The Selection, mapping and unmapped-row bullets below
> describe the 2026-10-27 behaviour and are superseded by
> [MAPPED_TOP_100_2026-11-03.md](MAPPED_TOP_100_2026-11-03.md); grain, prices, contexts, reports
> and the publisher are unchanged.

Owner decision 2026-10-27: the working list, the price reports and the Sheets publication move from
**configuration grain** (one row per catalog target) to **Shopee variation grain** (one row per
eligible Shopee seller variation). This is implemented offline; nothing was published, committed,
or written to Google Sheets, and no live discovery was run.

## Contract

- **Row.** One eligible Shopee seller variation, identified by the Phase 1 `source_key`
  (`shop_id|item_id|model_id`, all exact text) and its `shop_id`, `item_id`, `source_model_id`.
  `comparison_row_id` is `SV-<shop_id>-<item_id>-<model_id>`. `target_id` is never a row key.
- **Source-ID uniqueness.** Phase 1 quarantines every model ID tied to several shop/item
  identities (`find_model_identity_conflicts`), so `model_id` is unique in the eligible ranking.
  The variation grain additionally requires a unique `source_key` equal to
  `shop_id|item_id|model_id` (`build_variation_snapshot` refuses anything else). A model ID that
  ever appeared under two sources is never mapped by model ID alone (`SOURCE_ID_NOT_UNIQUE`).
- **Metric.** Each variation's own `ranking_gmv_usd` = M-1 full calendar month + MTD as
  extracted (the Phase 1 metric, never MTD-only, never summed across overlapping cumulative
  snapshots, never summed across variations). Order: ranking GMV, MTD GMV, then `model_id` (text
  order), the Phase 1 tie-break. `rank_metric` / `rank_metric_scope` record it on every row.
- **Selection (superseded 2026-11-03: the pool is the eligible variations with a verified
  mapping, filtered before the limit).** The first 100 eligible variations (`eligibility_status = ELIGIBLE`,
  `present_in_mtd`). Exactly 100 distinct rows when at least 100 qualify; otherwise the true
  shortfall and its reason, never padded or duplicated. Eligibility, date interpretation,
  quarantine and duplicate decisions are Phase 1's, inherited through the eligible ranking.
- **A mapping is enrichment, not a gate (superseded 2026-11-03: a verified mapping is now the
  entry condition for the Top 100; the exclusion logic below is unchanged, but excluded rows
  are left out and lower qualifying rows take their places).** `TARGET_MASTER` / `SOURCE_MODEL_MAP` only decide whether
  a row has a *trusted comparison context* (an ACTIVE, complete CONFIGURATION target whose
  `cpu_model` agrees with its `cpu_series`, with no open target-level conflict). Unmapped, inactive,
  listing-only, incomplete or conflicted mappings keep the variation ranked at its position with
  `comparison_status = CONFIGURATION_REVIEW_PENDING` and no specs shown. An open source-level
  register conflict keeps the listing ranked and sets `SOURCE_CONFLICT_COMPARISON_BLOCKED`: it
  blocks only the retailer comparison. Excluded top rows are never replaced by lower mapped rows.
  `MATCH_REVIEW` is not read as a gate.
- **Own Shopee price.** Each row shows its own variation's snapshot price from the sales extract
  (`shopee_current_price_php`, `PRICE_OK`), as of the report cutoff: an extract dated after the
  cutoff gives no price (`SNAPSHOT_AFTER_CUTOFF`); an extract more than 7 days before the cutoff is
  shown labelled `SNAPSHOT_PRICE_STALE`; missing and conflicting prices stay explicit
  (`PRICE_MISSING`, `PRICE_CONFLICT`) and never drop the row. There is no fallback to another
  seller, the cheapest mapped seller, an average, a parent price, or the configuration-level
  snapshot. Only the extract date is known, so no per-price time is shown.
- **Contexts after selection.** The distinct trusted targets of the 100 rows are the comparison
  contexts. The retailer layer (discovery, collection, matching, `track_prices.py`) is keyed by
  context, so many variations share one context and its observations: a context is searched,
  fetched and imported once. `variation_contexts.csv` also carries an identity fingerprint over the
  five core fields plus known secondary hardware (blank = unknown, equal only to blank); the
  catalog already refuses two targets with one core identity, so contexts are targets.
- **Unmapped rows (superseded 2026-11-03: they are excluded, listed in `mapping_exclusions.csv`).**
  They remained present with blank retailer prices and an explanation.
  *Not implemented:* an evidence-backed row context for an unmapped row (independently extracted
  source evidence under the existing rules). The repository has no source-listing evidence
  extractor to reuse safely, so such rows stay `CONFIGURATION_REVIEW_PENDING` until the catalog
  maps them; nothing is mapped, promoted or approved automatically.
- **Retailer cells, alternatives, minima.** A row's retailer cells are exactly its context's
  reviewed cells: matching offer first, otherwise `ALT 1`, then `ALT 2` (lowest price in a level),
  matching and alternative minima separate with separate in-stock minima (the alternative minima
  over the full eligible pool), out-of-stock and purchase-condition rules unchanged. Nothing is
  rematched, repriced or replayed.
- **Associations.** A row-to-observation association has its own ID (`association_id`); the
  observation itself is stored once. Coverage counts variation rows, contexts and unique
  observations separately (`variation_coverage.json`): a reused observation populating several
  rows is one observation.
- **Legacy outputs.** Historical configuration reports, rankings, dates, observation IDs and
  publication logs are untouched and keep their grain (`TARGET_CONFIGURATION`). Target ranks are
  never republished as variation ranks (a context's `context_order` is only a presentation order).

## Commands

All commands run in the repo's WSL environment (`source .venv/bin/activate`). Preview is the
default stopping point; `--write-sheets` is a separate, explicit, approved step.

```bash
weekly_run="data/output/weekly_runs/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$weekly_run/inputs"

# 1. Refresh source inputs (read-only Sheets access; no Sheets writes). The sales refresh now also
#    writes the local sidecar source_shop_names.csv (seller names).
python run_phase1.py --dry-run | tee "$weekly_run/sales_refresh.log"
cp data/output/eligible_model_ranking.csv data/output/top_100_targets.csv \
   data/output/phase1_run_summary.json data/output/period_summary.csv \
   data/output/source_shop_names.csv "$weekly_run/inputs/"
python check_target_tables.py --from-sheets --output-dir "$weekly_run/inputs"

# 2. Build and audit the Top 100 Shopee variations and their contexts.
python build_top_shopee_variations.py \
  --ranking "$weekly_run/inputs/eligible_model_ranking.csv" \
  --source-top-100 "$weekly_run/inputs/top_100_targets.csv" \
  --target-master "$weekly_run/inputs/target_master_validated.csv" \
  --source-model-map "$weekly_run/inputs/source_model_map_validated.csv" \
  --match-review "$weekly_run/inputs/match_review_validated.csv" \
  --output-dir "$weekly_run/shopee_variations"
python audit_top_shopee_variations.py --variation-dir "$weekly_run/shopee_variations" \
  --ranking "$weekly_run/inputs/eligible_model_ranking.csv" \
  --target-master "$weekly_run/inputs/target_master_validated.csv" \
  --source-model-map "$weekly_run/inputs/source_model_map_validated.csv" \
  --match-review "$weekly_run/inputs/match_review_validated.csv" \
  --legacy-dir "$weekly_run/verified_top_100"   # optional: before/after grain audit

# 3. Retailer layer, once per context (preview first; see WEEKLY_WORKFLOW.md for live discovery).
V="$weekly_run/shopee_variations"
python discover_products.py --preview --scope-file "$V/context_scope.csv" \
  --target-master "$weekly_run/inputs/target_master_validated.csv" \
  --selected-list "$V/context_selected_list.csv" --contributions "$V/context_contributions.csv" \
  --retailers laptop_factory,datablitz,pcx,pcc,oct --output-dir "$weekly_run/discovery_preview"
#    A live run (python discover_products.py --live --collect ...) takes the same arguments plus
#    --store; it is a separate, supervised step (rate limits: see the discovery notes).

# 4. Context report (existing tracker on the context list), then variation reports.
python track_prices.py --store data/history/price_history.sqlite3 \
  --target-master "$weekly_run/inputs/target_master_validated.csv" \
  --source-model-map "$weekly_run/inputs/source_model_map_validated.csv" \
  --match-review "$weekly_run/inputs/match_review_validated.csv" \
  --selected-list "$V/context_selected_list.csv" --contributions "$V/context_contributions.csv" \
  --sales-ranking "$weekly_run/inputs/eligible_model_ranking.csv" \
  --week-start "$week_start" --as-of "$as_of" --discovery-dir "$weekly_run/discovery" \
  --output-dir "$weekly_run/context_reports"
python build_variation_reports.py --variation-dir "$V" \
  --context-report-dir "$weekly_run/context_reports" --output-dir "$weekly_run/variation_reports"

# 5. Publisher preview (reads the live workbook with the read-only scope; writes nothing).
python publish_variation_prices.py --preview --variation-dir "$V" \
  --reports-dir "$weekly_run/variation_reports" \
  --context-report-dir "$weekly_run/context_reports" \
  --output-root "$weekly_run/publications"
# Only after review and explicit approval:
#   python publish_variation_prices.py --write-sheets ...   (same arguments)
#   python publish_variation_prices.py --rollback <publication folder>
```

`track_prices.py` is run on a *disposable copy* of the store when validating (it records pending
versioned matches); the weekly run uses the real store as before.

## Outputs

`build_top_shopee_variations.py`: `shopee_variation_ranking.csv` (every eligible variation, ranked),
`top_100_shopee_variations.csv`, `mapping_exclusions.csv` (since 2026-11-03; it replaces
`top_100_shopee_variations_review.csv`: eligible variations outside the list, with reason and a
suggested action), `variation_contexts.csv`, and the context list /
ledger / scope (`context_selected_list.csv`, `context_contributions.csv`, `context_scope.csv`) in the
formats the existing retailer layer reads, plus `shopee_variation_run_summary.json` (schema,
grain, metric, counts, input fingerprints). `audit_top_shopee_variations.py` rebuilds everything
from the same inputs and fails on any difference, then writes `grain_audit.json/.md`.

`build_variation_reports.py`: `weekly_variation_comparison.csv`, `weekly_variation_selections.csv`
(row x retailer), `weekly_variation_alternatives.csv`, `weekly_variation_rechecks.csv`,
`weekly_variation_history_associations.csv`, `variation_coverage.json`,
`variation_run_summary.json`. Every row carries `comparison_row_id`, `source_key`, the three IDs,
`row_grain` and `schema_version`.

> **Update 2026-11-05.** The publisher is now `variation-sheets-2` with sheet layout
> `variation-compact-1`: `PILOT_PRICE_COMPARISON` has a compact visible block and collapsed column
> groups (the contract header is 140 columns; the 80-column header below is kept as an earlier layout),
> the comparison and overview carry banner rows, and the publisher refreshes the owner's
> `TRACKER_GUIDE` text. The report schema (`shopee-variation-sheets-1`), ranking and every number are
> unchanged. See [SHEETS_DASHBOARD_2026-11-05.md](SHEETS_DASHBOARD_2026-11-05.md).

## Publisher (`publish_variation_prices.py`, `variation-sheets-1`)

- **Header contract:** `config/shopee_variation_sheet_contract.json` (the owner-supplied exact
  headers). The live headers are read first; a template tab must carry exactly the contract
  header. `PILOT_PRICE_HISTORY` / `PILOT_PRICE_REVIEW` keep their earlier columns and gain the
  appended ones; the run-log header is resolved from the live tab (the live workbook has the
  pilot-sheets-6 columns plus the ten appended; rows are filled by column name).
- **Tabs written:** `TOP_100_SHOPEE_VARIATIONS`, `PILOT_PRICE_COMPARISON`, `PILOT_PRICE_OVERVIEW`,
  `PILOT_PRICE_ALTERNATIVES`, `PILOT_PRICE_HISTORY`, `PILOT_PRICE_REVIEW`, `PILOT_PRICE_RUN_LOG`
  only, and only when they already exist. A missing template is refused, never created.
  `PILOT_PRICE_SUMMARY` and `P1_TOP_100_CONFIGURATIONS` are retired: they are never read as a
  destination, written, restored, recreated or part of any allowlist. `raw`, `MAIN`, `TOP SHOPS`,
  helper tabs, Phase 1 tabs, `TARGET_MASTER`, `SOURCE_MODEL_MAP`, `MATCH_REVIEW` and the hidden
  `ARCHIVE_*_20261027` tabs are not in the allowlist. `TRACKER_GUIDE` is not written by this
  publisher (its instructions are edited by the owner; this document is the reference).
- **Wrong grain is rejected before any Sheets access:** legacy configuration reports, any
  schema other than `shopee-variation-sheets-1`, rows not `SHOPEE_VARIATION`; and a template that
  still holds legacy configuration-grain content is refused. The legacy `publish_prices.py`
  refuses a workbook that has `TOP_100_SHOPEE_VARIATIONS` (it would also recreate the retired
  summary).
- **History and review:** records the publication regenerates (its own scope) are rebuilt from
  the local history; every other earlier live record is **retained as it is** (values, formats,
  links, own ranks), placed first, with the explicit legacy grain `TARGET_CONFIGURATION` and empty
  association fields (nothing is guessed). Regenerated earlier records carry the legacy grain and a
  blank rank (a context's order is not a rank). New association rows (grain `SHOPEE_VARIATION`,
  `Association ID`, rank, difference tag/count/details/unknowns) follow. Stale target-grain
  recheck rows are not kept: rechecks are republished per variation row.
- **Layout:** navy/orange headers, Roboto, banding, frozen identifiers, widths, PHP and USD
  formats; fills green MATCH / yellow ALT 1 / orange ALT 2 / grey RECHECK always with text (the
  status cell leads with its tag; the overview has notes with basis, actual configuration,
  differences, unknowns, stock, evidence and links); overview cells repeat the comparison cells.
- **Safety:** full local snapshot of each tab before writing, whole-tab replacement, readback
  verification of every value, link, format, note, fill, frozen panes and widths, run-log row per
  publication, `--rollback` restoring only backed-up data tabs of the variation allowlist.

## Before / after grain audit (saved 2026-10-26 inputs; run 20261026T105832+0800)

| | Before (configuration grain) | After (variation grain) |
|---|---|---|
| Rows | 86 configurations (shortfall 14) | 100 variations (shortfall 0) |
| Distinct configurations / contexts | 86 | 36 |
| Mapped, trusted | 86 | 65 |
| Unmapped | not rankable | 23 |
| Mapped but untrusted (8 listing-only, 4 CPU-model conflict) | not rankable | 12 |
| Eligible variations represented | 148 of 1357 | 100 of 1357 (own GMV each) |
| Own Shopee price | one configuration minimum | 100 of 100 own prices |
| Contexts shared by several selected rows | n/a | 6 |

Rows that assumed one row per `target_id`, and their disposition:

| Place | Disposition |
|---|---|
| `verified_top.build_verified_ranking` / `audit_verified_ranking` | Legacy configuration grain, kept as is (legacy reports stay readable). |
| `price_tracking.validate_selected_list`, `build_price_tracking`, `shopee_snapshot.select_shopee_prices` | Context layer: one row per *context* is correct; fed the context list. The configuration-level Shopee snapshot is not copied into variation rows. |
| `sheet_publication.load_pilot_report` (`by_target`, scope, selections) and `*_plan` | Legacy publisher, legacy grain only; it refuses variation workbooks. Reused only for the history/review base rows. |
| `variation_ranking`, `variation_reports`, `variation_publication` | Keyed by `source_key` / `comparison_row_id`; `target_id` is a value, never a key. |

## Validation

`tests/test_variation_ranking.py`, `tests/test_variation_reports.py`,
`tests/test_variation_publication.py` and the sidecar test in `tests/test_ranking.py` cover: two
sellers sharing a target, repeated target IDs, unmapped/untrusted rows skipped before the limit
(`tests/test_mapped_top_cli.py` covers the builder and audit CLIs end to end),
source-grain eligibility and ID uniqueness, own-price selection (no fallback), missing prices,
context reuse and separate row/context/observation coverage, contexts that differ or are unknown,
alternatives/minima/tags copied unchanged, legacy history retained with its grain, preview
accepting the new headers and rejecting the wrong grain, and retired/source/archive tabs untouched.
The offline suite has 14 failures that exist at baseline (`test_listed_spec` 9, `test_variant_axes`
3, `test_verification_safety` 2); they are unchanged.
