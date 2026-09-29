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
| 5. Build and validate `TARGET_MASTER` | PARTIALLY_COMPLETE | Schema approved on 2026-09-29. Validators exist for all three tables, with a read-only sheet reader and a Top 100 candidate seeder; Claude reported 129 passing tests. A live read-only check found no `TARGET_MASTER` header and found the supporting tabs missing. No real configurations or mappings have been entered, and no approver is named. |
| 6. Build shared scraping framework | COMPLETE | The retailer-neutral framework (`offers.py`, `scraping.py`) and an offline example-adapter harness were added on 2026-09-29. 203 tests, Ruff, and `compileall` passed. No live request was made. The framework does not read Sheets or match offers to targets. |
| 7. Implement retailer adapters | NOT_STARTED | No retailer adapters exist. Retailer selection and the data-use review are still required before any live request. |
| 8. Normalize and perform exact matching | NOT_STARTED | No offer normalization or exact configuration matcher exists. |
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
- **Scope:** Account for all 100 accepted Phase 4 Top 100 source model IDs. Track every distinct configuration whose exact specs and evidence can be verified; keep unresolved candidates in `MATCH_REVIEW`. Multiple source IDs may map to one configuration.
- **Tasks:** Prepare evidence-backed, complete configuration rows; identify the designated manager/category owner who approves additions and retirements; implement validators for the mapping and review tables; build a read-only sheet reader and a stable downstream interface.
- **Deliverables:** Approved `TARGET_MASTER`, `SOURCE_MODEL_MAP`, and `MATCH_REVIEW` schemas; validated rows and mappings; a repeatable local snapshot check; a read-only sheet path for consumers.
- **Dependencies:** Phase 4 target review, authoritative product evidence, and approval by the designated manager or category owner.
- **Validation:** Required-field and exact-identity checks, duplicate-ID and mapping checks, review-state checks, and representative manual approval review.
- **Definition of done:** The master and supporting tables contain approved, validated records; local CSV snapshots can be checked repeatably; downstream code can read the authoritative sheet tables without fuzzy identity assumptions.
- **Current status:** `PARTIALLY_COMPLETE`.
  - **Done:** The user approved the schema choices on 2026-09-29. [TARGET_MASTER_SCHEMA.md](TARGET_MASTER_SCHEMA.md), `config/target_master_template.csv`, and the local validator reflect the approved identity fields, required specifications, decimal storage units, source URL, stable IDs, and human approval fields.
    - `supporting_tables.py` validates `SOURCE_MODEL_MAP` and `MATCH_REVIEW` against their exact templates.
    - `check_target_tables.py` checks local CSV snapshots of all three tables, or reads the tabs with the read-only scope using `--from-sheets`.
    - `seed_match_review.py` produced 100 local `NEW` candidates from the accepted Top 100, with all specifications, URLs, and approvals blank.
    - Three stale `TARGET_MASTER` test expectations were corrected to the approved rules; the validator's rules were unchanged.
    - 129 tests, Ruff, and `compileall` passed on 2026-09-29.
  - **Not done:**
    - The designated manager or category owner has not been named.
    - No candidate has been researched, approved, or mapped. `TARGET_MASTER` and `SOURCE_MODEL_MAP` contain no real rows, and the live `TARGET_MASTER` worksheet was last reported empty on 2026-09-28.
    - A live read-only `--from-sheets` run found no `TARGET_MASTER` header and found `SOURCE_MODEL_MAP` and `MATCH_REVIEW` missing. Their approved headers must be added before the tables can pass validation.
    - The conservative defaults for unspecified behaviors (listed in the schema doc) need owner confirmation.
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
  - **Not done (later phases):** No retailer-specific adapter, live request, browser automation, data-use review, observation storage, or target matching. The retrieval defaults are conservative implementation choices that need per-retailer confirmation in Phase 7.

### Phase 7 — Implement retailer adapters

- **Objective:** Collect candidate offers from approved retailers.
- **Tasks:** Select retailers; implement and maintain each adapter; capture source URL, retrieval time, availability, price, and source text.
- **Deliverables:** Tested retailer-specific adapters and representative fixtures.
- **Dependencies:** Phase 6 framework and Phase 5 configuration schema.
- **Validation:** Fixture tests, controlled manual spot checks, and documented adapter failures.
- **Definition of done:** Each approved adapter meets the shared contract and has an owner and maintenance notes.
- **Current status:** `NOT_STARTED`. Retailer scraping and Playwright are not in the repository. Adapters must implement the Phase 6 contract in [SCRAPING_FRAMEWORK.md](SCRAPING_FRAMEWORK.md). Each retailer's terms, robots.txt, and rate limits must be reviewed and approved before its first live request.

### Phase 8 — Normalize and perform exact matching

- **Objective:** Match retailer offers only to the correct target configuration.
- **Tasks:** Normalize attributes; compare required configuration dimensions; classify exact, unmatched, and ambiguous offers.
- **Deliverables:** Normalization rules, exact matcher, match evidence, and review queue.
- **Dependencies:** Phases 5 and 6; retailer observations from Phase 7.
- **Validation:** Positive, negative, near-match, and ambiguous fixtures; require exact outcomes for all reviewed test cases.
- **Definition of done:** No ambiguous or partial match is silently published as exact.
- **Current status:** `NOT_STARTED`.

### Phase 9 — Generate comparisons and price history

- **Objective:** Report retailer prices against target configurations over time.
- **Tasks:** Define comparison cadence, currency and availability policy, observation identity, history retention, and output schemas.
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
