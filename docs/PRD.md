# Laptop Price Tracker: Product Requirements

**Status:** Phase 4 (the raw-data and ranking pipeline) is implemented in code, and the user accepted the generated CSV reports on 2026-09-29. On 2026-10-05, the user set the Phase 5 and Phase 8 core identity fields to `model`, `screen_size_in`, `cpu_series`, `ram_capacity_gb`, and `storage_capacity_gb`, and authorized promotion when these are populated and unambiguous. Missing resolution, display type, refresh rate, GPU model, OS, or source URL does not block promotion. On 2026-10-06 the local validators were synchronized with this policy and Phase 8 offline matching was completed ([MATCHING.md](MATCHING.md)); the user reported the same day that the live target tabs pass the read-only validator and allow `COMPLETE`. This repository changed no Sheet rows. *Current state (2026-10-12):* the catalog has 205 `TARGET_MASTER` rows (193 `CONFIGURATION`, 12 `LISTING_ONLY`), 360 mappings, and 5,154 review rows, all passing the validators. The working list is the **Top 100 verified configurations**: 128 configurations qualify, 100 are selected, and the list is published in `P1_TOP_100_CONFIGURATIONS`. CPU identity is matched at the family level (owner policy 2026-10-12). Replaying the saved nine-offer Laptop Factory check against the current catalog gives 9 `EXACT`.

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

- Automated production collection, Playwright browser automation, or scheduled price-history updates (offline price history exists since 2026-10-12). The shared framework and the Laptop Factory, DataBlitz, and PC Express adapters are tested offline and enabled for live use under project-owner decisions. Laptop Factory and DataBlitz have only one-page user-reported live checks; PC Express has no live check, and its external-policy finding remains RESTRICTED. No production collection has been run.
- Live, unsupervised matching of scraped offers. Exact matching runs offline over saved observations ([MATCHING.md](MATCHING.md)).
- Automatic promotion or mapping of `TARGET_MASTER` rows. The catalog is populated and validated (205 targets on 2026-10-12), but promotion stays a human-approved Sheet step. See [TARGET_MASTER_SCHEMA.md](TARGET_MASTER_SCHEMA.md).
- Inferring specifications that are absent or ambiguous in Shopee item or variation names.
- GitHub Actions scheduling or production monitoring.
- Writing to `raw` or automatically resolving a human-review conflict.

## Scope and phase terminology

The current Python entry point is named `run_phase1.py`, and its internal label is “Phase 1.” In the revised project plan, this work belongs mainly to **overall Phase 4, “Read raw and generate Top 100.”** Google access and the local environment are overall Phase 1. The two phase labels must not be treated as interchangeable.

### Current implementation (overall Phase 4)

The current code reads `raw`, validates rows, collapses exact duplicates, applies recorded duplicate decisions, detects periods, checks freshness, classifies eligibility, aggregates models, selects MTD prices, creates a full eligible ranking and Top 100, and saves local diagnostics and outputs. An explicit `--write-sheets` flag enables guarded writes to the approved Phase 1 output tabs.

### Planned long-term product

Remaining work includes working the verified-configuration review-priority queue, saving retailer observations for matching, producing comparisons and price history, running a controlled pilot, and automating an operating run. The Sheet and local validation rules are synchronized, and the `model`/`cpu_series` conventions are applied. The downstream interfaces are the read-only `TARGET_MASTER` handoff to `match_offers.py` and the Top 100 verified configurations working list.

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
| FR-11 | Validate `TARGET_MASTER` configurations. | Partially implemented. The user-approved policy is to promote candidates when the five core fields (`model`, `screen_size_in`, `cpu_series`, `ram_capacity_gb`, `storage_capacity_gb`) are populated and unambiguous. Missing secondary fields and source URL do not block promotion. Local validators enforce this policy since 2026-10-06 (`COMPLETE` review status included). The user reported on 2026-10-06 that the live tabs pass the read-only validator (168 targets, 326 mappings, 5,154 review rows) and that the dropdown allows `COMPLETE`. `audit_top_100_coverage.py` shows 63 accepted Top 100 IDs mapped to ACTIVE targets and 37 pending human review. Since 2026-10-08, a product confirmed to have no verifiable model may be tracked as a `LISTING_ONLY` target (`shop_id` + `item_id` + `source_model_id`, with reason and evidence), which is excluded from cross-retailer matching. This is implemented locally; live sheet headers are pending migration. **Update 2026-10-12:** the tracking-mode migration and the READY convention corrections are applied in the Sheet. The read-only validator passes 205 targets (193 `CONFIGURATION`, 12 `LISTING_ONLY`), 360 mappings, and 5,154 review rows. |
| FR-11a | Seed the Phase 4 Top 100 as `MATCH_REVIEW` candidates without guessing specifications. | Implemented (`seed_match_review.py`). On 2026-09-29 it produced 100 `NEW` local candidates from the accepted Top 100. The file is local and ignored; nothing was written to Sheets. |
| FR-11b | Publish the **Top 100 verified configurations** (owner decision 2026-10-12): unique ACTIVE `CONFIGURATION` targets with complete, unconflicted five-field identities, ranked by the combined GMV of eligible, approved mapped sources. Each target is counted once, and the source-level sales Top 100 is kept as a reference. | Implemented (`build_verified_top_100.py`, audited by `audit_verified_top_100.py`). On 2026-10-12, 127 configurations qualified from the 2026-09-28 sales snapshot in the first run, and 100 were selected. After the MSI reconciliation the list was rebuilt on the 205-target catalog: 128 qualify and 100 are selected. The audit passed, and the list is published in `P1_TOP_100_CONFIGURATIONS`. See [TARGET_MASTER_SCHEMA.md](TARGET_MASTER_SCHEMA.md#top-100-verified-configurations-2026-10-12). |
| FR-12 | Scrape retailer offers, match exact configurations, compare prices, and record history. | Partially implemented. The shared scraping framework (overall Phase 6; see [SCRAPING_FRAMEWORK.md](SCRAPING_FRAMEWORK.md)) and the offline-tested Laptop Factory, DataBlitz, PC Express, PC Central, and Octagon adapters (Phase 7; see [adapters/LAPTOP_FACTORY.md](adapters/LAPTOP_FACTORY.md), [adapters/DATABLITZ.md](adapters/DATABLITZ.md), [adapters/PCX.md](adapters/PCX.md), [adapters/PCC.md](adapters/PCC.md), and [adapters/OCT.md](adapters/OCT.md)) are implemented and live-enabled under project-owner decisions. PC Central's external-policy finding is UNKNOWN; its bare-URL live check stopped safely with `AMBIGUOUS_VARIANT`, and a selected-variant check returned one in-stock offer. Laptop Factory and DataBlitz have one-page user-reported live checks; a user-reported PC Express check returned one in-stock Acer offer. Controlled 2026-10-01 checks confirmed plain-string URLs for Laptop Factory and PC Express. PC Express's external-policy finding remains RESTRICTED and no retailer written consent is recorded. Octagon's external-policy finding is also RESTRICTED, because its terms prohibit scraping; a user-reported live check returned one in-stock HP offer at PHP 37,999. Exact matching is implemented as an offline path over saved observations ([MATCHING.md](MATCHING.md)); the saved nine-offer Laptop Factory check gives 9 `EXACT` against the current catalog (2026-10-12). Comparisons and history are implemented offline (Phase 9, 2026-10-12): a local append-only SQLite store, weekly Asia/Singapore comparisons over the selected list, and per-observation and weekly price history. Live collection, the controlled pilot, and automation remain later work. |
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

`TARGET_MASTER` holds validated laptop configurations. An earlier read-only check found no header row or data. Since then the Phase 5 tabs have been populated in the Sheet with headers that match the local templates; on 2026-10-12 they held 205 targets, 360 mappings, and 5,154 review rows, all passing the read-only validator.

The current policy is described in [TARGET_MASTER_SCHEMA.md](TARGET_MASTER_SCHEMA.md). Its core identity is the exact combination of `model`, `screen_size_in`, `cpu_series`, `ram_capacity_gb`, and `storage_capacity_gb`. Resolution, display type, refresh rate, GPU model, and OS are lower-priority details: record them when known, but their absence does not block promotion or a core match. Shopee source IDs belong in a separate mapping table; candidates with incomplete or ambiguous core fields remain in `MATCH_REVIEW`. Reuse an existing target row for the same core identity; otherwise assign a new stable `target_id`. Multiple source IDs may map to the same target. The accepted Top 100 and explicitly included sibling variations feed the reusable catalog, so it is not limited to one week's ranking. Local validators enforce this five-field policy, and the live Sheet headers and `MATCH_REVIEW` status dropdown were synchronized by 2026-10-08.

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

The planned downstream product and comparison fields are recorded in [PRICE_TRACKING_OUTPUT_SCHEMA.md](PRICE_TRACKING_OUTPUT_SCHEMA.md). Phase 8 matching uses the same five core fields and treats secondary specs as corroboration or conflict signals; it is implemented offline. Phase 9 comparison and history outputs are implemented offline (2026-10-12) by `track_prices.py`; see that schema document for the policy and files. This does not change current Phase 4 outputs or the Phase 6 offer observation model.

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
- The three Phase 5 tabs are populated and match the local templates (205 / 360 / 5,154 rows on 2026-10-12). Validators and a read-only reader exist for all three tables; matching and the verified-configuration build read validated snapshots. Promotion and mapping remain manual. Several behaviors not covered by the approved decisions use documented conservative defaults; see [TARGET_MASTER_SCHEMA.md](TARGET_MASTER_SCHEMA.md#unspecified-behaviors-and-chosen-defaults).
- Eligibility is keyword-based and should be reviewed against real listings; ambiguous cases go to human review.
- The optional remote eligibility-decision tab is not read into the same run's classification.
- No scheduled retailer collection, supervised collector, Playwright dependency, GitHub Actions workflow, or production output-tab setup exists in the repository. The exact matcher and the Phase 9 observation store, comparisons, and history run only offline on saved CSV snapshots.
- Future work should work the review-priority queue, persist supervised retailer observations, add price-history outputs, run controlled pilot tests, and then plan scheduled operations.

### Displayed-price comparison clarification (2026-10-13)

The owner approves OUT_OF_STOCK displayed prices in exact configuration comparisons. Stock
status stays visible so price comparison and current availability remain separate information.
All five-field matching, currency, price-type and missing-price conditions still apply.
Owner update (2026-10-15): confirmed bundle prices participate with a visible bundle-offer note,
including both lowest observed and lowest in-stock; unchecked bundle status stays excluded.
