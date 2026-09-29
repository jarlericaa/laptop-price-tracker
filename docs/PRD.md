# Laptop Price Tracker: Product Requirements

**Status:** Phase 4 (the raw-data and ranking pipeline) is implemented in code. The latest read-only live run completed after fingerprint-matched decisions excluded 21 historical conflicting keys; business review of the generated reports remains pending. Phase 5 has a proposed `TARGET_MASTER` schema and a local validator, and the schema awaits business approval. Retailer scraping and price comparison are future work.

## Purpose and business problem

The project is intended to turn Shopee Electronics raw data into a stable list of laptop models to monitor, then compare those configurations with retailer prices. The current pipeline addresses the first part: it reads the Google Sheets `raw` worksheet, checks and classifies rows, and ranks active eligible models. It does not scrape retailers or produce retailer comparisons.

The current problem is to produce a repeatable Top 100 candidate list without corrupting the source sheet, losing identifier precision, silently accepting conflicting duplicate rows, or ranking products that are not laptops.

## Users and stakeholders

- **Business analysts and category owners:** review eligibility, duplicates, and the ranked target list.
- **Project operator:** configures credentials and runs the local pipeline; future operators may monitor automation.
- **Retailer and data owners:** future stakeholders for approved configuration matching and price observations.
- **Google Sheets service account:** unattended read access to the source; write access is requested only for an explicitly write-enabled run.

## Goals

- Preserve `raw` as the source of truth and read it without writes.
- Validate source structure and values, preserving IDs and text as strings.
- Identify M-1 and MTD dynamically from the latest valid source month.
- Classify laptop listings with editable keyword rules and model-level manual decisions.
- Collapse exact duplicates and quarantine conflicts for explicit review.
- Rank eligible models using M-1 and MTD GMV, with stable tie-breaking.
- Produce local diagnostic and ranking files by default.
- Keep Google Sheets writes explicit and restricted to approved `P1_*` tabs.
- Define a future route from the ranked configurations to retailer comparisons and scheduled operation.

## Non-goals for the current implementation

- Retailer scraping, Playwright browser automation, retailer adapters, or price history.
- Exact matching of scraped offers to `TARGET_MASTER` configurations.
- An *approved* `TARGET_MASTER` schema, real configuration rows, or pipeline consumption of `TARGET_MASTER`. A proposed schema and a local validator exist; see [TARGET_MASTER_SCHEMA.md](TARGET_MASTER_SCHEMA.md).
- GitHub Actions scheduling or production monitoring.
- Writing to `raw` or automatically resolving a human-review conflict.

## Scope and phase terminology

The current Python entry point is named `run_phase1.py`, and its internal label is “Phase 1.” In the revised project plan, this work belongs mainly to **overall Phase 4, “Read raw and generate Top 100.”** Google access and the local environment are overall Phase 1. The two phase labels must not be treated as interchangeable.

### Current implementation (overall Phase 4)

The current code reads `raw`, validates rows, collapses exact duplicates, applies recorded duplicate decisions, detects periods, checks freshness, classifies eligibility, aggregates models, selects MTD prices, creates a full eligible ranking and Top 100, and saves local diagnostics and outputs. An explicit `--write-sheets` flag enables guarded writes to the approved Phase 1 output tabs.

### Planned long-term product

Later phases will validate laptop configurations in `TARGET_MASTER`, collect retailer offers through retailer adapters, perform exact configuration matching, produce weekly comparisons and price history, write approved outputs, and automate an operating run. These components are not currently implemented.

## Functional requirements

| ID | Requirement | Current state |
|---|---|---|
| FR-01 | Read Google Sheets `raw` as strings without numeric coercion. | Implemented; read-only smoke test passes. |
| FR-02 | Require the Phase 4 raw columns listed below and reject missing or repeated headers. | Implemented. |
| FR-03 | Quarantine invalid rows and stop if validation errors exceed `MAX_VALIDATION_ERRORS`. | Implemented. |
| FR-04 | Collapse exact duplicates and require a recorded decision for conflicting duplicate keys before publication. | Implemented; the latest live run completed with recorded decisions for its 21 historical conflicts. New, stale, or invalid conflicts still block publication. |
| FR-05 | Detect the latest month and immediately preceding calendar month dynamically. | Implemented. |
| FR-06 | Classify eligibility from configured terms, then apply a valid model-level manual decision. | Implemented from local CSV configuration. |
| FR-07 | Rank eligible models with the defined GMV formula and deterministic tie-breaking. | Implemented and unit-tested; the 2026-09-28 live run completed after recorded decisions for its historical conflicts. Business review remains pending. |
| FR-08 | Select one distinct MTD price without averaging; flag conflicting prices. | Implemented. |
| FR-09 | Save local outputs and diagnostics by default. | Implemented under `data/output/`. |
| FR-10 | Write only approved output tabs after explicit opt-in; never write `raw`. | Guarded writer implemented. The user reported that the nine production output tabs were created header-only and verified on 2026-09-29; the Python writer itself has not been tested against a dedicated test spreadsheet. |
| FR-11 | Validate `TARGET_MASTER` configurations. | Partially implemented. A local validator and CSV check cover the proposed schema, and synthetic tests pass. The schema still needs business approval. The tab is empty and not consumed by the pipeline. |
| FR-12 | Scrape retailer offers, match exact configurations, compare prices, and record history. | Planned. |
| FR-13 | Run and monitor the pipeline in GitHub Actions. | Planned. |

## Input data

The production input is the Google Sheets worksheet named `raw` (configurable through `RAW_TAB_NAME`). The current code requires these headers:

```text
grass_date, shopid, shop_name, is_cross_border, is_official_store,
is_managed, itemid, item_name, modelid, model_name, current_price,
total_orders, gmv_usd, amount
```

The code validates `grass_date`, the three ID fields (`shopid`, `itemid`, `modelid`), `item_name`, `model_name`, `current_price`, and `gmv_usd`. IDs must be digit-only strings. Price and GMV must be finite, non-negative numbers. Other required columns are retained but are not currently subjected to equivalent type/range validation.

Internal names map the source fields to `period_date`, `shop_id`, `item_id`, `model_id`, `shopee_model_name`, `shopee_current_price_php`, and `total_gmv_usd`. Identifiers and names remain strings, including values with leading zeroes or numeric-looking text.

`TARGET_MASTER` is intended to hold validated laptop configurations. The read-only check on 2026-09-28 found the worksheet but no header row or data rows.

A proposed schema is described in [TARGET_MASTER_SCHEMA.md](TARGET_MASTER_SCHEMA.md). It covers identity, brand, series/model, CPU, memory, storage, display, variants, and review fields. The document separates what earlier docs established from choices that still need approval. `check_target_master.py` validates a local CSV export against the proposal. No code reads the worksheet's configurations or feeds them into the pipeline.

## Eligibility rules

Rules are stored in `config/eligibility_rules.csv` with `INCLUDE` or `EXCLUDE` types. Text is normalized to lowercase alphanumeric words with collapsed whitespace. A rule matches as a phrase in the combined `item_name` and `model_name` text.

- An inclusion match with no exclusion match is `ELIGIBLE`.
- An exclusion match with no inclusion match is `EXCLUDED`.
- Mixed inclusion and exclusion evidence is `REVIEW`, except a generic `laptop` match combined with a product-level exclusion is treated as `EXCLUDED`.
- No rule match is `REVIEW`.
- Accessory wording such as “charger included” is handled as bundle wording rather than automatically excluding a laptop listing.
- A manual `ELIGIBLE` or `EXCLUDED` decision keyed by `model_id` takes precedence over automatic classification. These decisions are read from `config/eligibility_decisions.csv` for the current run.
- If listings for one model have conflicting eligibility statuses, the model is assigned `REVIEW`.

The writer can merge decisions already present in `P1_ELIGIBILITY_DECISIONS` when it is explicitly used. However, the current pipeline classifies the run from the local CSV before that write step; remote sheet decisions are not read into that same run. Treat the local CSV as the effective run input until this behavior is deliberately changed and verified.

## Duplicate handling and quarantine

The composite duplicate key is `(period_month, shop_id, item_id, model_id)`. Rows under a key are compared across the required source columns.

- If all compared values match, one row is retained and marked as an exact duplicate.
- If values differ, the key is quarantined. A redacted `duplicate_review.csv` template is saved locally with key fields, differing fields, source row numbers, and a content fingerprint; it omits names, GMV, and price values.
- The local `config/duplicate_decisions.csv` accepts `EXCLUDE_KEY` or `KEEP_SOURCE_ROW`. A decision must include the exact current fingerprint, reviewer, review date, and reason. `KEEP_SOURCE_ROW` must identify one source row. Stale fingerprints, invalid rows, or ambiguous decisions do not resolve a conflict.
- A model ID associated with multiple `(shop_id, item_id)` identities is also excluded from publication and reported as a model-identity conflict.
- Unresolved duplicate or model-identity records are written to local diagnostics and block publication. The normal pipeline does not silently choose a row.

## Period and ranking logic

The latest month present after row validation is MTD. M-1 is the immediately preceding calendar month, even if no rows exist for that month. Unless `SKIP_FRESHNESS_CHECK=true`, the latest month must match the current month in `Asia/Manila`.

For each model:

```text
ranking_gmv_usd = sum(M-1 gmv_usd) + sum(MTD gmv_usd)
```

Only models classified `ELIGIBLE` and present in MTD qualify for ranking. The full ranking sorts by `ranking_gmv_usd` descending, then MTD GMV descending, then `model_id` ascending as a string. The Top 100 is the first 100 entries, or fewer when fewer models qualify.

The representative listing is the earliest source row in MTD when present, otherwise the earliest row in M-1. The price is selected from distinct valid MTD prices. One distinct value is reported; multiple values yield a `PRICE_CONFLICT` with no selected price. Prices are never averaged. Price conflicts do not, by themselves, remove a model from the GMV ranking.

## Outputs

The default local output directory is `data/output/`:

| File | Purpose |
|---|---|
| `top_100_targets.csv` | Ranked top laptop targets. |
| `eligible_model_ranking.csv` | Full eligible active-model ranking. |
| `excluded_listings.csv` | Excluded model records. |
| `eligibility_review.csv` | Models needing eligibility review. |
| `conflicting_duplicates.csv` | Duplicate and model-identity conflict report. |
| `duplicate_review.csv` | Redacted local duplicate-decision template. |
| `validation_errors.csv` | Row validation errors and source row numbers. |
| `period_summary.csv` | Period selection and row counts. |
| `phase1_run_summary.json` | Run-level counts and period metadata. |

The optional Google Sheets writer uses these approved tabs: `P1_TOP_100_TARGETS`, `P1_ELIGIBLE_RANKING`, `P1_EXCLUDED_LISTINGS`, `P1_ELIGIBILITY_REVIEW`, `P1_ELIGIBILITY_DECISIONS`, `P1_CONFLICTING_DUPLICATES`, `P1_VALIDATION_ERRORS`, `P1_PERIOD_SUMMARY`, and `P1_RUN_LOG`. The user reported that all nine tabs were created header-only in the production spreadsheet and their headers verified on 2026-09-29. The Python writer has not been tested against a dedicated test spreadsheet. The user must explicitly enable writes with `--write-sheets`; unresolved conflicts stop the run before any sheet output write.

## Non-functional requirements and security

- Preserve the source worksheet as read-only.
- Keep service-account credentials outside the repository; never commit `.env`, JSON keys, or local business-data outputs.
- The default command path is local-output-only. Source authentication requests the read-only Sheets scope; the write scope is requested only when the explicit writer path runs.
- Bound validation failures with `MAX_VALIDATION_ERRORS` and fail closed on unresolved conflicts or invalid decisions.
- Keep ranking deterministic for identical inputs.
- Do not publish raw rows or secrets in documentation, logs, or checked-in files.
- Keep outputs and review files private because most generated output files contain listing names, prices, GMV, and other business data.

## Acceptance criteria

### Current Phase 4 acceptance

- All unit tests, Ruff checks, and compilation pass.
- The real `raw` read returns expected headers and string values.
- A live run completes all validation, classification, aggregation, and ranking without unresolved conflicts and saves local outputs. **Processing met on 2026-09-28:** the read-only run completed in 57.49 seconds and generated 100 targets with the 21 historical conflicting keys excluded by recorded decisions. **Business acceptance remains pending:** review the Top 100 and eligibility reports.
- The nine production output tabs exist with verified headers and blank second rows, according to the user's 2026-09-29 report. **Partially met:** the application writer still needs testing against a dedicated test spreadsheet before it is used to publish pipeline data.

### Long-term acceptance

- `TARGET_MASTER` has an agreed schema and validation rules.
- Retailer adapters collect attributable offers and exact configuration matching rejects ambiguous matches.
- Comparisons and price history are reproducible and reviewed.
- A controlled pilot, scheduled automation, alerting, and operational ownership are documented and tested.

## Known limitations and future enhancements

- The latest successful live run took 57.49 seconds; stage timings were not captured. The synthetic 12,000-model aggregation test passes its under-10-second assertion, which does not establish end-to-end runtime.
- `TARGET_MASTER` is empty. Its proposed schema needs business approval, and there is no pipeline integration. Only a local CSV validator exists.
- Eligibility is keyword-based and should be reviewed against real listings; ambiguous cases go to human review.
- The optional remote eligibility-decision tab is not read into the same run's classification.
- No retailer scraper, Playwright dependency, exact matcher, price-history store, GitHub Actions workflow, or production output-tab setup exists in the repository.
- Future work should approve the `TARGET_MASTER` schema, populate reviewed configurations, and add retailer-specific adapters, exact configuration matching, price-history outputs, controlled pilot tests, and scheduled operations.
