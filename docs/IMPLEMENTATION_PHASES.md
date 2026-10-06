# Implementation Phases

## Phase-label note

The Python implementation and CLI are named “Phase 1” (`run_phase1.py` and `run_phase1()`), but that internal label predates the revised end-to-end plan. This implementation is mainly **overall Phase 4: Read raw and generate Top 100**. Google access and the local environment are **overall Phase 1**. Progress should use the overall phase numbers below, and mention the legacy internal name only when referring to the code.

## Status summary

| Overall phase | Status | Current evidence |
|---|---|---|
| 0. Finalize architecture and operating rules | COMPLETE | This documentation set records the architecture, requirements, safety rules, and decisions based on the repository. |
| 1. Configure Google access and local environment | COMPLETE | Read-only `raw` and `TARGET_MASTER` access succeeded. Defining and populating the empty `TARGET_MASTER` is Phase 5. |
| 2. Create repository and project structure | COMPLETE | Four grouped commits cover setup, implementation, tests, and initial docs; follow-up commits record the Phase 5 validator and this status refresh. Local business decisions and outputs remain ignored. |
| 3. Create Google Sheets output structure | COMPLETE | User-reported verification confirms the nine production output tabs and headers. A read-only report for the dedicated test spreadsheet confirmed all nine test tabs and headers, expected synthetic output rows, and `top_100_count=1`. The sequential-write recovery procedure is documented. The test report did not list or inspect `raw`; no production write occurred. |
| 4. Read raw and generate Top 100 | COMPLETE | The 2026-09-28 read-only run completed in 57.49 seconds and generated 100 targets after 21 historical keys were explicitly excluded. The user accepted the current CSV reports on 2026-09-29; no production Sheets write was made. |
| 5. Build and validate `TARGET_MASTER` | PARTIALLY_COMPLETE | On 2026-10-05 the user approved rule-based promotion when the five core identity fields are populated and unambiguous. On 2026-10-06 the local validators were synchronized: only the five core fields (plus `target_id`/`status`) are required, and `MATCH_REVIEW` accepts `COMPLETE`. Sheet dropdowns, promotion, and mappings remain outstanding. No Sheet data was read or changed. |
| 6. Build shared scraping framework | COMPLETE | The retailer-neutral framework (`offers.py`, `scraping.py`) and an offline example-adapter harness were added on 2026-09-29. 203 tests, Ruff, and `compileall` passed. No live request was made. The framework does not read Sheets or match offers to targets. |
| 7. Implement retailer adapters | COMPLETE | LF, DBZ, PCX, PCC, and OCT adapters meet the Phase 6 contract, have offline tests and documentation, and have live-check evidence recorded. Jarelle Ricaforte is the named maintainer for all five. Unobserved page states remain documented limitations; retailer-policy findings remain separate from the project-owner live-use decision. |
| 8. Normalize and perform exact matching | COMPLETE (offline) | Per-retailer extraction, the five-field `ExactMatcher`, and `match_offers.py` match saved observation CSVs against a validated `TARGET_MASTER` snapshot and write results, a review queue, and field evidence. Verified on synthetic fixtures only (763 tests, Ruff). See [MATCHING.md](MATCHING.md) for limitations. |
| 9. Generate comparisons and price history | NOT_STARTED | No comparison or history outputs exist. |
| 10. Test and run a controlled pilot | BLOCKED | Requires accepted Phase 4 outputs, approved and populated `TARGET_MASTER` configurations, and adapters first. |
| 11. Add GitHub Actions automation | NOT_STARTED | No workflow exists; this follows a passing controlled pilot. |
| 12. Production operation and maintenance | NOT_STARTED | Operational automation and ownership are future work. |

## Phase details

### Phase 0 — Finalize architecture and operating rules

- **Objective:** Agree on system boundaries, data safety rules, and project sequence.
- **Tasks:** Record source-of-truth rules; define dry-run and write gates; distinguish current Python work from planned scraping and automation.
- **Deliverables:** `PRD.md`, `ARCHITECTURE.md`, `DECISIONS.md`, and this phase plan.
- **Dependencies:** Repository and current implementation inspection.
- **Validation:** Compare documentation with source, tests, configuration, and read-only command results.
- **Definition of done:** Current and planned components, safety boundaries, and status labels are documented without treating plans as implemented.
- **Current status:** `COMPLETE` for this documentation pass.

### Phase 1 — Configure Google access and local environment

- **Objective:** Support safe local access to the production spreadsheet.
- **Tasks:** Configure the service account and external credential path; install Python dependencies; share the spreadsheet; verify `raw` and `TARGET_MASTER` reads.
- **Deliverables:** Local `.env`, external service-account JSON, installed environment, and read-only smoke checks.
- **Dependencies:** Google Cloud project, Sheets API, spreadsheet ID, and service-account sharing.
- **Validation:** `python smoke_test.py`; read-only `TARGET_MASTER` check; verify headers and string values.
- **Definition of done:** A documented local setup can read the source and required tabs with no write scope.
- **Current status:** `COMPLETE`. Read-only access to `raw` and `TARGET_MASTER` worked on 2026-09-28. `TARGET_MASTER` has no header or data rows; its schema and contents are Phase 5 work.

### Phase 2 — Create repository and project structure

- **Objective:** Keep the project organized, reproducible, and reviewable in version control.
- **Tasks:** Maintain package modules, config files, tests, dependencies, ignore rules, README, and docs.
- **Deliverables:** `src/laptop_tracker/`, `tests/`, `config/`, project metadata, and documentation.
- **Dependencies:** Phase 0 decisions and a local Python environment.
- **Validation:** `pytest -q`, `ruff check .`, `compileall`, and `git status --short`.
- **Definition of done:** Project files are reviewed, tracked appropriately, and no credentials or generated private data are staged.
- **Current status:** `COMPLETE`. Four purpose-based commits cover project setup, implementation, tests, and the initial docs. Follow-up commits record the Phase 5 validator and current status refresh; private local decision files and generated outputs remain ignored.

### Phase 3 — Create Google Sheets output structure

- **Objective:** Establish safe destinations for Phase 4 reports.
- **Tasks:** Create and verify the approved output tabs in a test spreadsheet first; confirm dimensions and headers; maintain the `raw` write guard.
- **Deliverables:** The `P1_*` tabs listed in [the PRD](PRD.md#outputs), with approved output schemas.
- **Dependencies:** Phases 1 and 2; reviewed output schema.
- **Validation:** Read tab names and headers; exercise writes only against a dedicated test spreadsheet; confirm `raw` remains unchanged.
- **Definition of done:** All expected output tabs and headers are verified in the intended destination, with a documented rollback/recovery procedure.
- **Current status:** `COMPLETE`. The user reported on 2026-09-29 that all nine approved `P1_*` tabs were created in the intended production spreadsheet, every header was verified, and the second row was blank. The linked `gid` was `TOP SHOPS`; `raw` is a separate tab. A later user-provided read-only report confirmed all nine tabs and headers in the dedicated test spreadsheet, one target in the top-target and eligible-ranking tabs, one charger row in excluded listings, and a run-log `top_100_count` of 1. The sequential-write recovery procedure is documented in [SETUP_AND_OPERATIONS.md](SETUP_AND_OPERATIONS.md). The report did not list or inspect a `raw` tab. No production `--write-sheets` run has been performed.

### Phase 4 — Read raw and generate Top 100

- **Objective:** Produce a validated, explainable laptop target ranking from Shopee `raw`.
- **Tasks:** Validate rows; resolve duplicates; detect M-1/MTD; classify laptops; apply decisions; aggregate and rank; produce diagnostics and local outputs.
- **Deliverables:** Top 100, full eligible ranking, review/exclusion/conflict reports, period summary, and run summary.
- **Dependencies:** Phases 1 and 2; reviewed eligibility and duplicate decisions.
- **Validation:** Unit tests, static checks, raw smoke test, and a complete live local-only run with no unresolved conflicts.
- **Definition of done:** A live run completes and produces reviewed outputs; any optional sheet write is separately verified and explicitly opted into.
- **Current status:** `COMPLETE`. The 2026-09-28 read-only run completed in 57.49 seconds and generated 100 targets. All 21 historical conflicting keys had fingerprint-matched `EXCLUDE_KEY` decisions, so their source rows remained excluded without selecting or summing disputed values. The user accepted the generated CSV reports on 2026-09-29; no changes were requested. The synthetic 12,000-model aggregation test passed its under-10-second assertion. No production Google Sheets write or `run_phase1.py --write-sheets` run occurred.

### Phase 5 — Build and validate `TARGET_MASTER`

- **Objective:** Create the authoritative list of laptop configurations to compare against retailers.
- **Scope:** Keep a reusable catalog of laptop configurations, not just the current week's Top 100. Include accepted Top 100 source model IDs and sibling variation IDs that are added to `MATCH_REVIEW`. Multiple Shopee source IDs may map to one configuration.
- **Tasks:** Promote a review candidate when its five core identity fields are populated and unambiguous: `model`, `screen_size_in`, `cpu_series`, `ram_capacity_gb`, and `storage_capacity_gb`. Reuse a matching target row or create one with a new stable `target_id`; map each source model ID to that target. Keep missing secondary fields from blocking promotion. Implement matching validators and a stable downstream interface.
- **Deliverables:** Synchronized `TARGET_MASTER`, `SOURCE_MODEL_MAP`, and `MATCH_REVIEW` schemas; promoted rows and mappings; a repeatable local snapshot check; a read-only sheet path for consumers.
- **Dependencies:** Phase 4 candidate data and the user's 2026-10-05 authorization for rule-based promotion using the five core fields. Do not infer missing specs. Conflicting or incomplete core identity data remains in review.
- **Validation:** Five-core-field completeness, exact core-key deduplication, stable target-ID uniqueness, one-target-per-source-ID mapping, and review-state checks. Missing secondary fields are allowed; known secondary-field disagreements must be surfaced without silently overwriting values.
- **Definition of done:** The master and supporting tables contain validated records promoted under the five-field rule; local CSV snapshots can be checked repeatably; downstream code can read the authoritative sheet tables without fuzzy identity assumptions.
- **Current status:** `PARTIALLY_COMPLETE`.
  - **Done:** The user approved a five-field core identity and rule-based promotion on 2026-10-05. See [TARGET_MASTER_SCHEMA.md](TARGET_MASTER_SCHEMA.md), [PRICE_TRACKING_OUTPUT_SCHEMA.md](PRICE_TRACKING_OUTPUT_SCHEMA.md), and [DECISIONS.md](DECISIONS.md).
    - `supporting_tables.py` validates `SOURCE_MODEL_MAP` and `MATCH_REVIEW` against their exact templates.
    - `check_target_tables.py` checks local CSV snapshots of all three tables, or reads the tabs with the read-only scope using `--from-sheets`.
    - `seed_match_review.py` produced 100 local `NEW` candidates from the accepted Top 100, with all specifications, URLs, and approvals blank.
    - Three stale `TARGET_MASTER` test expectations were corrected to the approved rules; the validator's rules were unchanged.
    - 129 tests, Ruff, and `compileall` passed on 2026-09-29.
    - 2026-10-06: `target_master.py` and `supporting_tables.py` now implement the five-field policy. Secondary fields, `brand`, `source_url`, `reviewer`, and `reviewed_at` are optional in `TARGET_MASTER`; duplicate configurations are detected on the five core fields with the matcher's normalization (`14` equals `14.0`). `MATCH_REVIEW` accepts `COMPLETE`, which requires all five core fields. Header templates were already correct and are unchanged.
  - **Not done:**
    - Add `COMPLETE` to the Sheet's `MATCH_REVIEW` status dropdown and check the live headers; no Sheet was read or written for the local validator change.
    - Promote eligible rows and populate `SOURCE_MODEL_MAP`; the current state of the live sheet was not checked during this documentation update.
    - No downstream consumer interface or automatic full Top 100 source-ID coverage check exists.

### Phase 6 — Build shared scraping framework

- **Objective:** Define common behavior for retailer data collection.
- **Tasks:** Specify adapter inputs and outputs, retrieval metadata, retries and timeouts, throttling, parsing errors, and test fixtures.
- **Deliverables:** Shared scraper interface, normalized offer model, logging, and adapter test harness.
- **Dependencies:** Phase 5 schema and approved data-use approach. The framework needs neither populated `TARGET_MASTER` rows nor live access. The data-use approval gates live use in Phase 7, not the offline framework.
- **Validation:** Offline fixtures, error-path tests, and deterministic normalization tests.
- **Definition of done:** A retailer adapter can plug into the shared framework and return attributable, normalized observations.
- **Current status:** `COMPLETE` for the shared framework, verified offline on 2026-09-29. See [SCRAPING_FRAMEWORK.md](SCRAPING_FRAMEWORK.md).
  - **Done:**
    - A `RetailerAdapter` protocol and `ParsedOffer` output. The framework adds all attribution: retailer, URLs, UTC retrieval time, attempts, body SHA-256, and adapter version.
    - An `OfferObservation` model with `Decimal` prices, ISO currency codes, availability evidence, and a deterministic `to_record()`. It has no `target_id`.
    - A `Fetcher` with configurable timeouts, bounded retries with backoff, `Retry-After` handling, and per-retailer throttling. Transport and clock are injectable.
    - Retryable fetch failures are kept separate from non-retried parse and validation failures, and host allowlists cover requests, redirects, and offer links.
    - Redacted structured logs.
    - An offline example adapter with synthetic fixtures and 74 new tests.
  - **Not done (later phases):** No live request, browser automation, observation storage, or target matching. The retrieval defaults are conservative implementation choices that need per-retailer confirmation in Phase 7. The first retailer adapter and data-use review were added in Phase 7.

### Phase 7 — Implement retailer adapters

- **Objective:** Collect candidate offers from approved retailers.
- **Tasks:** Select retailers; implement and maintain each adapter; capture source URL, retrieval time, availability, price, and source text.
- **Deliverables:** Tested retailer-specific adapters and representative fixtures.
- **Dependencies:** Phase 6 framework and Phase 5 configuration schema.
- **Validation:** Fixture tests, controlled manual spot checks, and documented adapter failures.
- **Definition of done:** Each approved adapter meets the shared contract and has an owner and maintenance notes.
- **Current status:** `COMPLETE`. LF, DBZ, PCX, PCC, and OCT meet the Phase 6 adapter contract, have offline tests and documentation, and have live-check evidence recorded in [PROGRESS_LOG.md](PROGRESS_LOG.md). Jarelle Ricaforte is the named maintainer for all five adapters. Known unobserved page states remain documented limitations. External-policy findings, including RESTRICTED findings for PCX and OCT, remain distinct from the project-owner decision that enables internal live use.
  - **Done (Laptop Factory, 2026-09-29):**
    - **Data-use review:** recorded in [RETAILER_DATA_USE_REVIEW.md](RETAILER_DATA_USE_REVIEW.md). External-policy finding UNKNOWN: robots.txt does not exclude product pages; the only "terms" page is a warranty policy, and the privacy policy is a template. Project status OWNER_APPROVED under the user's internal-use assumption in [DECISIONS.md](DECISIONS.md); this does not verify retailer permission.
    - **Adapter:** [`adapters/laptop_factory.py`](adapters/LAPTOP_FACTORY.md) parses one fetched product page. It prefers JSON-LD and falls back to visible content and the Tech Specs table. It separates sale and regular prices and fails on unclear prices. It combines stock wording, JSON-LD availability, and Add to Cart state, and gives `UNKNOWN` on conflict. It flags bundles with evidence without dropping them, and preserves raw specs without inferring refresh rate or other values.
    - **Framework:** optional `OfferDetails` (backward-compatible; `to_record()` unchanged). Redirects are followed by the `Fetcher` only after the target host is checked; `urllib` previously followed them first. A fail-closed live-use gate.
    - **Tests:** minimal fixtures and 70 new offline tests, 273 in total. Ruff (changed files) and `compileall` passed.
  - **Done (DataBlitz, 2026-10-01):**
    - **Host:** `ecommerce.datablitz.com.ph`, confirmed because `datablitz.com.ph` and `www.datablitz.com.ph` redirect there. Only that exact host is allowlisted.
    - **Data-use review:** recorded in [RETAILER_DATA_USE_REVIEW.md](RETAILER_DATA_USE_REVIEW.md). External-policy finding UNKNOWN. robots.txt (Shopify-generated) allows product pages. The storefront's Shopify terms and privacy policy pages return 404, and no other terms page was found. Project status OWNER_APPROVED under the same owner policy; this does not verify retailer permission.
    - **Adapter:** [`adapters/datablitz.py`](adapters/DATABLITZ.md) parses one fetched Shopify product page. It prefers JSON-LD and falls back to visible content and ignores the quick-buy `<template>` copy. It separates sale and compare-at prices, requires peso evidence, and fails on unclear prices. It combines JSON-LD availability with the add-to-cart button and gives `UNKNOWN` without an explicit signal or on conflict. It flags bundle wording with evidence and keeps raw description specs.
    - **Tests:** four minimal fixtures and 69 new offline tests, 342 in total. Ruff passed on the changed Python files. No framework change was needed.
  - **Done (PC Express, 2026-10-01):**
    - **Host:** `pcx.com.ph`, the PC Express Shopify storefront. `www.pcx.com.ph` redirects there with HTTP 301. Only that exact host is allowlisted.
    - **Data-use review:** recorded in [RETAILER_DATA_USE_REVIEW.md](RETAILER_DATA_USE_REVIEW.md). External-policy finding **RESTRICTED**: the retailer's terms of service prohibit systematic or automated data collection, including scraping, without PC Express' express written consent. robots.txt allows product pages, but that does not override the terms. An explicit owner decision now sets `live_use_approved = True` for internal use; see [DECISIONS.md](DECISIONS.md). No written retailer consent is recorded.
    - **Adapter:** [`adapters/pcx.py`](adapters/PCX.md) parses one fetched Shopify (Kalles theme) product page. It reconciles the page's two Product JSON-LD blocks, which come from SEO and review apps, and fails when they disagree. It separates `<del>` regular and `<ins>` sale prices and ignores the "SAVE" badge. It combines JSON-LD availability, the add-to-cart button, and the "Notify Me When Available" button. It fails on variant markup or unclear prices, flags bundle wording with evidence, and keeps the raw configuration summary, Description table, and categories.
    - **Tests:** five minimal fixtures and 80 new offline tests, 422 in total. Ruff passed on the changed Python files. No framework change was needed.
  - **Done (PC Central, 2026-10-01):**
    - **Retailer and host:** the user confirmed that `PCC` is PC Central, because more than one retailer fits the code. The storefront is `pccentralcomputerstore.com` (Shopify, Dawn theme); `www` redirects there with HTTP 301. The previously indexed `pccentral.com.ph` now redirects to an unrelated site and is not allowlisted. Only the exact storefront host is allowed.
    - **Data-use review:** recorded in [RETAILER_DATA_USE_REVIEW.md](RETAILER_DATA_USE_REVIEW.md). External-policy finding UNKNOWN: the terms and privacy pages contain only a warranty line, and robots.txt allows product pages. Project status OWNER_APPROVED under the project-wide owner decision; this is not retailer consent.
    - **Adapter:** [`adapters/pcc.py`](adapters/PCC.md) parses one fetched product page. Reviewed laptops are sold in RAM variants at different prices, so a multi-variant page needs a `?variant=<id>` URL whose variant the page selected; otherwise it fails with `AMBIGUOUS_VARIANT`. The adapter cross-checks the displayed price with the selected JSON-LD offer and the variant JSON. It gives SALE only for Dawn's on-sale state with a higher compare-at price. It combines four availability signals, checks product identity, flags bundle wording in the title or variant title, and keeps raw description lines.
    - **Tests:** three minimal fixtures and 107 new offline tests, 529 in total. Ruff check passed on the changed Python files. No framework change was needed.
    - **Live checks:** a bare-URL check fetched the page and failed safely with `AMBIGUOUS_VARIANT`; a selected-variant check parsed one in-stock ASUS VivoBook offer at PHP 36,999.00. See [PROGRESS_LOG.md](PROGRESS_LOG.md#pcc-live-smoke-checks-2026-10-01).
  - **Done (Octagon, 2026-10-01):**
    - **Retailer and host:** the request identified `OCT` as Octagon. Octagon's own terms, privacy, and About Us pages confirm Octagon Computer Superstore (owned by Upson International Corp.) at `www.octagon.com.ph`, a Shopify storefront on the "Multi" theme. The apex redirects there with HTTP 301. Only the exact `www` host is allowlisted.
    - **Data-use review:** recorded in [RETAILER_DATA_USE_REVIEW.md](RETAILER_DATA_USE_REVIEW.md). External-policy finding **RESTRICTED**: the terms prohibit using the site or its content to "spider, crawl, or scrape". robots.txt allows product pages, but that does not override the terms. Project status OWNER_APPROVED under the existing project-wide decision; no new owner decision was made, and no Octagon permission is recorded.
    - **Adapter:** [`adapters/oct.py`](adapters/OCT.md) parses one fetched product page. It cross-checks the displayed `dl.#price` amount, the selected JSON-LD offer, and the analytics variant price. It gives SALE only for `@on-sale` with a higher `@compare` amount. It combines JSON-LD, the stock alert, and the button. It checks SKU and product-path identity, and quotes the theme's invalid leading-zero `gtin12` values so JSON-LD stays readable. It never takes the store name as brand. It follows the PCC `?variant=<id>` rule, flags bundle wording, and keeps raw "Tech Specification" lines.
    - **Tests:** four minimal fixtures and 121 new offline tests, 650 in total. Ruff passed on the changed Python files. No framework change was needed.
    - **Live check:** the user reported `ok: True` for an HP 15-FC0413AU offer at PHP 37,999, `IN_STOCK`; the displayed offer URL includes a variant ID.
  - **Done (follow-up, 2026-10-01):**
    - **LF bare-URL live check:** one controlled call for a reviewed product page returned `ok: True`. It was an Acer Aspire AI 14 at SALE PHP 45,999.00 (regular PHP 50,999.00), `IN_STOCK`. The `repr()` of the requested, final, and offer URLs was the same bare URL.
    - **PCX offer URL:** one controlled call for the reviewed HP 14-EP1144TU page returned `ok: True` (REGULAR PHP 47,990.00, `IN_STOCK`). `repr(offer.offer_url)` was the bare `https://pcx.com.ph/products/…` URL as a `str`.
    - **LF request hardening:** `product_request` now requires `https://laptopfactory.com.ph/product/<slug>/` with no query or fragment. It previously accepted `http`, nested paths, and any query, including the cart-changing `?add-to-cart=`. 10 new offline tests; 660 in total.
    - **Maintainers:** no CODEOWNERS, `.github`, `AGENTS.md`, or maintainer record existed during the initial review. On 2026-10-01, the user named Jarelle Ricaforte as maintainer for LF, DBZ, PCX, PCC, and OCT.
    - **Coverage:** unobserved page states remain documented per adapter. No suitable product for them was known without collection browsing or search, so no further live request was made.
    - See [PROGRESS_LOG.md](PROGRESS_LOG.md#phase-7-live-follow-up-checks-2026-10-01).
  - **Not done:**
    - Page states not observed live remain assumptions. They include sold-out and pre-order pages for every adapter, multi-variant pages for LF, DBZ, PCX, and OCT, and bundle pages for PCX, PCC, and OCT. Review them when a suitable product page is known.
    - Search or category discovery, which is out of scope for the product-page adapter.

### Phase 8 — Normalize and perform exact matching

- **Objective:** Match retailer offers only to the correct target configuration.
- **Tasks:** Normalize attributes; first match exactly on `model`, `screen_size_in`, `cpu_series`, `ram_capacity_gb`, and `storage_capacity_gb`. Use resolution, display type, refresh rate, GPU model, OS, and other known details as corroborating or conflict signals. Missing secondary values do not block a core match. Classify exact, unmatched, ambiguous, and review-required offers; do not silently publish ambiguous core matches.
- **Deliverables:** Normalization rules, exact matcher, match evidence, and review queue.
- **Dependencies:** Phases 5 and 6; retailer observations from Phase 7.
- **Validation:** Positive, negative, near-match, and ambiguous fixtures; require exact outcomes for all reviewed test cases.
- **Definition of done:** No ambiguous or partial match is silently published as exact.
- **Current status:** `COMPLETE` as an offline path (2026-10-06). See [MATCHING.md](MATCHING.md).
  - **Done:** `extraction.py` maps each adapter's labeled spec rows to canonical core and secondary fields with per-field evidence and fails closed (`MISSING`, `UNPARSEABLE`, `AMBIGUOUS`, `NOT_EXTRACTED` for shared multi-variant descriptions). `ExactMatcher.match_observation()` applies the five-field rule, treats secondary conflicts as review, lists one-field near candidates without assigning them, and never assigns inactive or duplicate-key targets. `match_offers.py` runs the batch offline against a fully valid `TARGET_MASTER` snapshot and writes results, a review queue, and evidence. Synthetic tests cover exact, safe-normalization, one-field near-match, missing/invalid core, duplicate-key, inactive-target, missing-secondary, secondary-conflict, and every retailer extractor, plus the end-to-end command.
  - **Limitations (not blocking the offline deliverable):** Only Laptop Factory and single-variant PC Central output carries a `model` label, so other retailers' offers always go to review. The `cpu_series` family form and retailer-wording `model` need owner confirmation as `TARGET_MASTER` conventions. No real target snapshot or saved real observation has been matched, and no command persists live observations yet.

### Phase 9 — Generate comparisons and price history

- **Objective:** Report retailer prices against target configurations over time.
- **Tasks:** Define comparison cadence, currency and availability policy, observation identity, history retention, and implement the requested fields in [PRICE_TRACKING_OUTPUT_SCHEMA.md](PRICE_TRACKING_OUTPUT_SCHEMA.md). Confirm retailer codes, units, precision, and missing-price representation.
- **Deliverables:** Weekly comparison and historical price outputs.
- **Dependencies:** Validated `TARGET_MASTER`, adapter observations, and exact matches.
- **Validation:** Reconcile sample comparisons to source observations; test missing offers, price changes, duplicates, and reruns.
- **Definition of done:** Repeated runs produce traceable comparisons without losing observation provenance.
- **Current status:** `NOT_STARTED`.

### Phase 10 — Test and run a controlled pilot

- **Objective:** Validate end-to-end behavior with a limited retailer and configuration set.
- **Tasks:** Agree on pilot scope; run several supervised cycles; review errors, match quality, operational effort, and recovery.
- **Deliverables:** Pilot results, issue list, acceptance decision, and runbook updates.
- **Dependencies:** Phases 4-9 complete enough for an end-to-end pilot.
- **Validation:** Reconcile target, source offers, matches, comparisons, and output tabs with human reviewers.
- **Definition of done:** Stakeholders accept the pilot results and residual risks before automation.
- **Current status:** `BLOCKED` by incomplete Phase 5 and unstarted Phases 7-9.

### Phase 11 — Add GitHub Actions automation

- **Objective:** Run the approved process on a schedule with safe credential handling and visible failures.
- **Tasks:** Define schedule, secrets, write approval policy, logs, artifact retention, notifications, retries, and concurrency controls.
- **Deliverables:** Reviewed workflow and operating alerts.
- **Dependencies:** Passing Phase 10 pilot and approved production write procedure.
- **Validation:** Manual workflow dispatch, read-only run, controlled output test, failure notification, and credential exposure checks.
- **Definition of done:** Scheduled runs are observable, repeatable, and recoverable by a named operator.
- **Current status:** `NOT_STARTED`. No GitHub Actions workflow exists.

### Phase 12 — Production operation and maintenance

- **Objective:** Operate the tracker reliably and maintain data quality and adapters.
- **Tasks:** Assign ownership; review scheduled runs; rotate credentials; update retailer adapters; triage conflicts and price anomalies; periodically review rules and schemas.
- **Deliverables:** Operating calendar, escalation path, maintenance records, and service-level expectations.
- **Dependencies:** Successful automation and an accepted pilot.
- **Validation:** Regular run reviews, recovery drills, and output-quality audits.
- **Definition of done:** An owner can operate, diagnose, and recover the pipeline using current documentation.
- **Current status:** `NOT_STARTED`.
