# Implementation Phases

## Phase-label note

The Python implementation and CLI are named “Phase 1” (`run_phase1.py` and `run_phase1()`), but that internal label predates the revised end-to-end plan. This implementation is mainly **overall Phase 4: Read raw and generate Top 100**. Google access and the local environment are **overall Phase 1**. Progress should use the overall phase numbers below, and mention the legacy internal name only when referring to the code.

## Status summary

| Overall phase | Status | Current evidence |
|---|---|---|
| 0. Finalize architecture and operating rules | COMPLETE | This documentation set records the architecture, requirements, safety rules, and decisions based on the repository. |
| 1. Configure Google access and local environment | COMPLETE | Read-only `raw` and `TARGET_MASTER` access succeeded. Defining and populating the empty `TARGET_MASTER` is Phase 5. |
| 2. Create repository and project structure | COMPLETE | Four grouped commits cover setup, implementation, tests, and initial docs; follow-up commits record the Phase 5 validator and this status refresh. Local business decisions and outputs remain ignored. |
| 3. Create Google Sheets output structure | PARTIALLY_COMPLETE | User reported that all nine approved `P1_*` tabs were created with verified headers and blank second rows in the real spreadsheet. The Python writer has not been tested against a dedicated test spreadsheet. |
| 4. Read raw and generate Top 100 | PARTIALLY_COMPLETE | Live read-only run completed in 57.49 seconds and generated 100 targets after 21 historical keys were explicitly excluded; business review of reports remains pending. |
| 5. Build and validate `TARGET_MASTER` | PARTIALLY_COMPLETE | Proposed schema, local validator, and CSV check command exist, covered by synthetic tests. The schema still needs business approval. The real tab is still empty and unchanged; no pipeline consumer exists. |
| 6. Build shared scraping framework | NOT_STARTED | No scraping framework exists. |
| 7. Implement retailer adapters | NOT_STARTED | No retailer adapters exist. |
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
- **Current status:** `PARTIALLY_COMPLETE`. The user reported on 2026-09-29 that all nine approved `P1_*` tabs were created in the intended spreadsheet, every header was verified, and the second row was blank. The linked `gid` was `TOP SHOPS`; `raw` is a separate tab. The Python writer still needs a synthetic-data test against a dedicated test spreadsheet, and recovery behavior needs to be documented. No pipeline `--write-sheets` run has been performed.

### Phase 4 — Read raw and generate Top 100

- **Objective:** Produce a validated, explainable laptop target ranking from Shopee `raw`.
- **Tasks:** Validate rows; resolve duplicates; detect M-1/MTD; classify laptops; apply decisions; aggregate and rank; produce diagnostics and local outputs.
- **Deliverables:** Top 100, full eligible ranking, review/exclusion/conflict reports, period summary, and run summary.
- **Dependencies:** Phases 1 and 2; reviewed eligibility and duplicate decisions.
- **Validation:** Unit tests, static checks, raw smoke test, and a complete live local-only run with no unresolved conflicts.
- **Definition of done:** A live run completes and produces reviewed outputs; any optional sheet write is separately verified and explicitly opted into.
- **Current status:** `PARTIALLY_COMPLETE`. The 2026-09-28 read-only run completed in 57.49 seconds and generated 100 targets. All 21 historical conflicting keys had fingerprint-matched `EXCLUDE_KEY` decisions, so their source rows remained excluded without selecting or summing disputed values. Review of the ranking and eligibility reports is still pending. No Google Sheets writes were run. The synthetic 12,000-model aggregation test passed its under-10-second assertion.

### Phase 5 — Build and validate `TARGET_MASTER`

- **Objective:** Create the authoritative list of laptop configurations to compare against retailers.
- **Tasks:** Agree on configuration fields and uniqueness rules; populate reviewed configurations; validate required fields, brands, model/series, CPU, memory, storage, display, and relevant variants as agreed.
- **Deliverables:** A documented `TARGET_MASTER` schema, validated rows, and a repeatable import/check path.
- **Dependencies:** Phase 4 target review and business agreement on exact configuration identity.
- **Validation:** Required-field, duplicate-configuration, and representative manual-review checks.
- **Definition of done:** The tab has approved headers and configurations that pass validation; downstream code can consume it without fuzzy identity assumptions.
- **Current status:** `PARTIALLY_COMPLETE`.
  - **Done:** A proposed schema is documented in [TARGET_MASTER_SCHEMA.md](TARGET_MASTER_SCHEMA.md), with a header-only template at `config/target_master_template.csv`. `src/laptop_tracker/target_master.py` validates headers, required fields, formats, allowed values, and dates. It also flags duplicate `target_id` values and duplicate configuration identities, keeps every value as an exact string, and never picks between duplicates. `check_target_master.py` checks a local CSV export without Google access and writes local reports only. Synthetic unit tests cover valid, invalid, duplicate, optional-field, and exact-string cases.
  - **Not done:** The column set, mandatory fields, identity key, and conventions are proposals awaiting business approval; the open questions are listed in the schema doc. No real configurations have been defined. The real `TARGET_MASTER` worksheet was not written and remains empty. There is no read-only sheet check for the tab, and no pipeline consumer. Phase 4 report review, which this phase depends on, is still pending.

### Phase 6 — Build shared scraping framework

- **Objective:** Define common behavior for retailer data collection.
- **Tasks:** Specify adapter inputs and outputs, retrieval metadata, retries and timeouts, throttling, parsing errors, and test fixtures.
- **Deliverables:** Shared scraper interface, normalized offer model, logging, and adapter test harness.
- **Dependencies:** Phase 5 schema and approved data-use approach.
- **Validation:** Offline fixtures, error-path tests, and deterministic normalization tests.
- **Definition of done:** A retailer adapter can plug into the shared framework and return attributable, normalized observations.
- **Current status:** `NOT_STARTED`.

### Phase 7 — Implement retailer adapters

- **Objective:** Collect candidate offers from approved retailers.
- **Tasks:** Select retailers; implement and maintain each adapter; capture source URL, retrieval time, availability, price, and source text.
- **Deliverables:** Tested retailer-specific adapters and representative fixtures.
- **Dependencies:** Phase 6 framework and Phase 5 configuration schema.
- **Validation:** Fixture tests, controlled manual spot checks, and documented adapter failures.
- **Definition of done:** Each approved adapter meets the shared contract and has an owner and maintenance notes.
- **Current status:** `NOT_STARTED`. Retailer scraping and Playwright are not in the repository.

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
- **Current status:** `BLOCKED` by incomplete Phases 4 and 5 and unstarted Phases 6-9.

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
