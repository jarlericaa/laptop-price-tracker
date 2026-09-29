# Laptop Price Tracker: Product Requirements

**Status:** Phase 4 (the raw-data and ranking pipeline) is implemented in code, and the user accepted the current generated CSV reports on 2026-09-29. The latest read-only live run completed after fingerprint-matched decisions excluded 21 historical conflicting keys. The user revised the Phase 5 `TARGET_MASTER` identity key on 2026-09-29 to match the bolded fields in the downstream output requirements. Local validators for `TARGET_MASTER`, `SOURCE_MODEL_MAP`, and `MATCH_REVIEW` are implemented, as are a read-only sheet reader and Top 100 candidate seeder. The master Sheet headers need to be updated to the revised local templates. No configurations or mappings have been populated, and no approver has been named. Phase 5 remains partially complete. Retailer scraping and price comparison are future work.

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

- Automated production collection, Playwright browser automation, or price history. The shared framework and Laptop Factory adapter are tested offline; the adapter is enabled for live use under project-owner policy, but no live collection has been run.
- Exact matching of scraped offers to `TARGET_MASTER` configurations.
- Real configuration rows or pipeline consumption of `TARGET_MASTER`. Its revised identity schema and validators exist locally, and a read-only reader covers it and its supporting tables. The Sheet header still needs to be updated. See [TARGET_MASTER_SCHEMA.md](TARGET_MASTER_SCHEMA.md).
- Automatic promotion of review candidates, automatic source-to-target mapping, or inferring specifications from Shopee titles.
- GitHub Actions scheduling or production monitoring.
- Writing to `raw` or automatically resolving a human-review conflict.

## Scope and phase terminology

The current Python entry point is named `run_phase1.py`, and its internal label is “Phase 1.” In the revised project plan, this work belongs mainly to **overall Phase 4, “Read raw and generate Top 100.”** Google access and the local environment are overall Phase 1. The two phase labels must not be treated as interchangeable.

### Current implementation (overall Phase 4)

The current code reads `raw`, validates rows, collapses exact duplicates, applies recorded duplicate decisions, detects periods, checks freshness, classifies eligibility, aggregates models, selects MTD prices, creates a full eligible ranking and Top 100, and saves local diagnostics and outputs. An explicit `--write-sheets` flag enables guarded writes to the approved Phase 1 output tabs.

### Planned long-term product

Remaining work includes researching the seeded candidates, preparing and approving configurations in `TARGET_MASTER` and their mappings, defining the downstream interface for the matching phase, collecting retailer offers through retailer adapters, performing exact configuration matching, producing weekly comparisons and price history, and automating an operating run. These components are not currently implemented.

## Functional requirements

| ID | Requirement | Current state |
|---|---|---|
| FR-01 | Read Google Sheets `raw` as strings without numeric coercion. | Implemented; read-only smoke test passes. |
| FR-02 | Require the Phase 4 raw columns listed below and reject missing or repeated headers. | Implemented. |
| FR-03 | Quarantine invalid rows and stop if validation errors exceed `MAX_VALIDATION_ERRORS`. | Implemented. |
| FR-04 | Collapse exact duplicates and require a recorded decision for conflicting duplicate keys before publication. | Implemented; the latest live run completed with recorded decisions for its 21 historical conflicts. New, stale, or invalid conflicts still block publication. |
| FR-05 | Detect the latest month and immediately preceding calendar month dynamically. | Implemented. |
| FR-06 | Classify eligibility from configured terms, then apply a valid model-level manual decision. | Implemented from local CSV configuration. |
| FR-07 | Rank eligible models with the defined GMV formula and deterministic tie-breaking. | Implemented and unit-tested; the 2026-09-28 live run completed after recorded decisions for its historical conflicts. The user accepted the current output reports on 2026-09-29. |
| FR-08 | Select one distinct MTD price without averaging; flag conflicting prices. | Implemented. |
| FR-09 | Save local outputs and diagnostics by default. | Implemented under `data/output/`. |
| FR-10 | Write only approved output tabs after explicit opt-in; never write `raw`. | Guarded writer implemented and tested with synthetic data against a dedicated test spreadsheet; a read-only report confirmed its nine output tabs and expected synthetic rows. No production write occurred. |
| FR-11 | Validate `TARGET_MASTER` configurations. | Partially implemented. The revised schema and local templates are in place. Local CSV validators exist for `TARGET_MASTER`, `SOURCE_MODEL_MAP`, and `MATCH_REVIEW`; a read-only reader and `check_target_tables.py --from-sheets` handle the three tabs. The user reported the tabs had headers and no data rows; the `TARGET_MASTER` and `MATCH_REVIEW` sheet headers now need to be updated to the revised templates. No real configurations or mappings have been added, no approver is named, and there is no downstream consumer yet. |
| FR-11a | Seed the Phase 4 Top 100 as `MATCH_REVIEW` candidates without guessing specifications. | Implemented (`seed_match_review.py`). On 2026-09-29 it produced 100 `NEW` local candidates from the accepted Top 100. The file is local and ignored; nothing was written to Sheets. |
| FR-12 | Scrape retailer offers, match exact configurations, compare prices, and record history. | Partially implemented. The shared scraping framework (overall Phase 6; see [SCRAPING_FRAMEWORK.md](SCRAPING_FRAMEWORK.md)) and the offline-tested Laptop Factory adapter (Phase 7; see [adapters/LAPTOP_FACTORY.md](adapters/LAPTOP_FACTORY.md)) are implemented. Live use is enabled under project-owner policy, although retailer permission remains unverified and no live collection has been run. Other retailer adapters, exact matching, comparisons, and history are planned. |
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

`TARGET_MASTER` is intended to hold validated laptop configurations. An earlier read-only check found no header row or data. The user later reported that the Phase 5 tabs had headers and no data rows; the local header has since been revised and must be synchronized.

The revised schema is described in [TARGET_MASTER_SCHEMA.md](TARGET_MASTER_SCHEMA.md). It requires the ten user-designated 1:1 identity fields, brand context, a source URL, stable IDs, and approval details; Shopee source IDs belong in a separate mapping table, and incomplete candidates belong in `MATCH_REVIEW`. The full accepted Top 100 is in scope: every distinct configuration with verified specs should be tracked, while several source IDs may point to one configuration. `check_target_master.py` validates a local CSV export against the schema. `check_target_tables.py` validates snapshots of all three tables, or with `--from-sheets` reads the three tabs using the read-only scope. No code feeds validated configurations into the ranking pipeline or a matcher yet.

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

The optional Google Sheets writer uses these approved tabs: `P1_TOP_100_TARGETS`, `P1_ELIGIBLE_RANKING`, `P1_EXCLUDED_LISTINGS`, `P1_ELIGIBILITY_REVIEW`, `P1_ELIGIBILITY_DECISIONS`, `P1_CONFLICTING_DUPLICATES`, `P1_VALIDATION_ERRORS`, `P1_PERIOD_SUMMARY`, and `P1_RUN_LOG`. The user reported that all nine tabs were created header-only in the production spreadsheet and their headers verified on 2026-09-29. A synthetic-data writer test against a dedicated test spreadsheet completed, and a user-provided read-only report confirmed the expected tabs, headers, synthetic output rows, and run-log count. No production write or `run_phase1.py --write-sheets` run has occurred. The user must explicitly enable writes with `--write-sheets`; unresolved conflicts stop the run before any sheet output write.

The planned downstream product and comparison fields are recorded in [PRICE_TRACKING_OUTPUT_SCHEMA.md](PRICE_TRACKING_OUTPUT_SCHEMA.md). The Phase 5 `TARGET_MASTER` identity key has been revised to the user's bolded 1:1 fields; Phase 8 matching and Phase 9 comparison outputs are not implemented yet. This does not change current Phase 1 outputs or the Phase 6 offer observation model.

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
- A live run completes all validation, classification, aggregation, and ranking without unresolved conflicts and saves local outputs. **Met on 2026-09-28:** the read-only run completed in 57.49 seconds and generated 100 targets with the 21 historical conflicting keys excluded by recorded decisions. **Business acceptance met on 2026-09-29:** the user accepted the current CSV reports.
- The nine production output tabs exist with verified headers and blank second rows, according to the user's 2026-09-29 report. **Test-writer criterion met:** the guarded writer completed against a dedicated test spreadsheet with synthetic data, and a read-only report confirmed the expected output tabs, headers, and rows. No production write occurred.

### Long-term acceptance

- `TARGET_MASTER` has approved headers, evidence-backed configurations, and a repeatable validation path.
- Retailer adapters collect attributable offers and exact configuration matching rejects ambiguous matches.
- Comparisons and price history are reproducible and reviewed.
- A controlled pilot, scheduled automation, alerting, and operational ownership are documented and tested.

## Known limitations and future enhancements

- The latest successful live run took 57.49 seconds; stage timings were not captured. The synthetic 12,000-model aggregation test passes its under-10-second assertion, which does not establish end-to-end runtime.
- An earlier read-only check found no Phase 5 headers or supporting tabs. The user later reported all three tabs were created with headers and no rows. The local `TARGET_MASTER` and `MATCH_REVIEW` templates have since been revised; the sheet still needs to reflect those changes. Validators and a read-only reader exist for all three tables, but there is no pipeline integration. Several behaviors not covered by the approved decisions use documented conservative defaults; see [TARGET_MASTER_SCHEMA.md](TARGET_MASTER_SCHEMA.md#unspecified-behaviors-and-chosen-defaults).
- Eligibility is keyword-based and should be reviewed against real listings; ambiguous cases go to human review.
- The optional remote eligibility-decision tab is not read into the same run's classification.
- No retailer scraper, Playwright dependency, exact matcher, price-history store, GitHub Actions workflow, or production output-tab setup exists in the repository.
- Future work should research the seeded candidates, populate approved configurations and mappings, and run the read-only sheet check. It should then add retailer-specific adapters, exact configuration matching, price-history outputs, controlled pilot tests, and scheduled operations.
