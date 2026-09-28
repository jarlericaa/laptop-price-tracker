# Progress Log

Append new dated rows as work is verified. Keep earlier rows as historical evidence; add a follow-up row when a prior status changes. Do not include credentials, raw worksheet rows, or private listing details.

## Current status summary

- **Overall status:** The read-only live raw-to-local-output pipeline completed successfully on 2026-09-28. The Top 100 and related outputs still need business review; no Google Sheets output writes have been run.
- **Performance:** The synthetic test for 12,000 models passes its under-10-second aggregation assertion. The latest live dry run took 57.49 seconds. Individual pipeline stages have not been timed.
- **Google access:** Read-only `raw` and `TARGET_MASTER` checks succeeded. On 2026-09-28, `raw` had 23,544 data rows and expected headers; `TARGET_MASTER` had no headers or data rows.
- **Duplicate review:** 21 historical conflicting duplicate keys have fingerprint-matched `EXCLUDE_KEY` decisions in `config/duplicate_decisions.csv`. The source rows remain quarantined; no conflicting values were selected or summed.
- **Sheet outputs:** The writer allowlist exists, but no `P1_*` output tabs exist in the real spreadsheet and no sheet writes were run.
- **Automation and scraping:** No retailer adapters, exact configuration matcher, history output, or GitHub Actions workflow exists.
- **Git state:** Branch `main`; README is modified and application/config/test files are untracked in the current worktree. No files were committed.

## Phase status

| Phase | Status | Evidence / next gate |
|---|---|---|
| 0. Architecture and operating rules | COMPLETE | This documentation set records the current and planned design. |
| 1. Google access and local environment | PARTIALLY_COMPLETE | Raw and `TARGET_MASTER` reads pass; populate and define `TARGET_MASTER`. |
| 2. Repository and project structure | NEEDS_REVIEW | Code and tests pass; review current untracked/modified Git state. |
| 3. Sheets output structure | PARTIALLY_COMPLETE | Allowlist exists; create and test output tabs in a separate test spreadsheet. |
| 4. Raw processing and Top 100 | PARTIALLY_COMPLETE | Tests pass; live read-only dry run completed and generated 100 targets. Review outputs before accepting them; no Sheets write was run. |
| 5. `TARGET_MASTER` | NOT_STARTED | Current worksheet is empty; schema and validator are absent. |
| 6. Shared scraping framework | NOT_STARTED | No framework exists. |
| 7. Retailer adapters | NOT_STARTED | No adapters exist. |
| 8. Exact matching | NOT_STARTED | No matcher exists. |
| 9. Comparisons and price history | NOT_STARTED | No comparison/history outputs exist. |
| 10. Controlled pilot | BLOCKED | Depends on completed Phase 4 and Phases 5-9. |
| 11. GitHub Actions | NOT_STARTED | No workflow exists; pilot must pass first. |
| 12. Production operation | NOT_STARTED | Future work after automation. |

## Chronological entries

| Date | Phase | Area | Task | Status | Evidence | Files or commands | Notes | Next action |
|---|---|---|---|---|---|---|---|---|
| Date not recorded | 1 / 4 | Initial live setup | Prior read tests and ranking attempt | PARTIALLY_COMPLETE | Initial status supplied with the project request: service account configured, `raw` smoke and `TARGET_MASTER` read succeeded, and an earlier live run was interrupted around `aggregate_models()`. | User-provided context; earlier command output unavailable | Historical baseline only; do not treat the interrupted run as a successful pipeline run. | Use current checks below as the active evidence. |
| 2026-09-28 | 1 | Google Sheets read access | Read-only smoke test of the real `raw` worksheet | COMPLETE | Output confirmed spreadsheet title `Laptops Brand Tracker`, worksheet `raw`, 23,544 data rows, expected headers, and string-valued sample cells. | `python smoke_test.py` | No sheet was modified. The count is a point-in-time check and may change as source data grows. | Recheck during the next operating run. |
| 2026-09-28 | 1 / 5 | Google Sheets read access | Read-only check of `TARGET_MASTER` | PARTIALLY_COMPLETE | Worksheet exists; 0 data rows and 0 header columns returned. | Read-only `gspread` access through `sheets_source.connect()` | This does not validate a configuration schema; no such module exists. | Agree on schema and populate reviewed configurations in Phase 5. |
| 2026-09-28 | 2 / 4 | Local verification | Unit tests, lint, and compilation | COMPLETE | 35 tests passed; Ruff reported all checks passed; `compileall` completed. The ranking test includes a 12,000-model aggregation assertion under 10 seconds. | `.venv/bin/python -m pytest -q`; `.venv/bin/python -m ruff check .`; `.venv/bin/python -m compileall src run_phase1.py smoke_test.py` | These checks do not prove a complete live run or a production sheet write. | Preserve test coverage while resolving live conflicts. |
| 2026-09-28 | 4 | Live pipeline | Read-only live run with temporary local output | BLOCKED | Run reached ranking, then raised a safe `PipelineSafetyError`: 21 duplicate or model-identity records lacked decisions. It took about 90 seconds. The error is raised after ranking and local output construction in the pipeline. | `run_phase1(Settings.from_env(), output_dir=<temporary directory>)`; temporary files were removed automatically | No write scope was requested and no Google Sheet was modified. The temporary outputs are not available for review. `config/duplicate_decisions.csv` currently has only its header. | Run the standard local dry run to create `data/output/duplicate_review.csv`, review conflicts, record justified decisions, and rerun. Profile full-run stages if latency remains material. |
| 2026-09-28 | 3 | Sheet output structure | Read-only check for approved output tabs | PARTIALLY_COMPLETE | No approved `P1_*` output tabs were present. | Read-only worksheet-title check | The writer can create approved tabs when explicitly invoked; no write test was performed. | Test only against a dedicated test spreadsheet before production use. |
| 2026-09-28 | 2 | Repository state | Inspect current Git worktree | NEEDS_REVIEW | Branch is `main`; README is modified and application/config/test files are untracked. | `git status --short --untracked-files=all` | No commit was made, as requested. | Review and stage/commit later through the project's normal process. |
| 2026-09-28 | 0 | Documentation | Create project documentation set and README links | COMPLETE | Six requested docs are present; source-based statements and statuses are cross-linked. | `docs/*.md`, `README.md` | Documentation does not claim retailer scraping or a successful live pipeline. | Append future verified work; keep this log historical. |
| 2026-09-28 | 4 | Live pipeline and duplicate review | Record conservative decisions and rerun the read-only live pipeline | COMPLETE | 21 current review entries were recorded as fingerprint-matched `EXCLUDE_KEY` decisions. All 21 conflicts are from 2026-01, 2026-03, or 2026-05, outside active M-1/MTD months. The dry run exited successfully in 57.49 seconds, reported 100 Top 100 targets, and emitted `EXCLUDED_BY_DECISION` for all 21 conflict records. | `config/duplicate_decisions.csv`; `python run_phase1.py --dry-run` | Reviewer is transparently recorded as Codex under the user's request. The disputed source rows remain excluded; no row was selected, summed, or edited. `raw` and `TARGET_MASTER` were not modified. No `--write-sheets` run occurred. | Review the generated ranking and eligibility outputs; then define and populate the approved `TARGET_MASTER` schema for Phase 5. Test the output writer only on a dedicated test spreadsheet. |

## Current blockers

1. The generated Top 100 and eligibility outputs still need business review and acceptance.
2. `TARGET_MASTER` has no headers or rows; its schema, validation, and downstream consumption are not implemented.
3. The real spreadsheet has no `P1_*` output tabs. The optional writer has not been exercised against a dedicated test spreadsheet.
4. The latest full read-only run took 57.49 seconds. Stage-level timings are unavailable if runtime needs further optimization.
5. The current Git worktree has many untracked implementation files and a modified README; no commit was made.

## Facts not reverified

- The earlier snapshot diagnostics for unique model IDs, item IDs, shop IDs, and duplicate keys were not recomputed. The current smoke test reports worksheet rows and headers only; its row count is now 23,544 rather than the earlier approximate 23,338.
- The earlier unresolved-conflict run took about 90 seconds and stopped at the conflict gate. The latest successful run took 57.49 seconds; neither run recorded per-stage timings, so the share of runtime spent in validation, duplicate handling, eligibility, or aggregation is unknown.
- The latest run's output file presence, Top 100 count, and conflict resolution-status counts were checked. Business-row contents were not printed or included in this log.

## Recommended next actions

1. Review `data/output/top_100_targets.csv`, `eligible_model_ranking.csv`, `excluded_listings.csv`, and `eligibility_review.csv` locally. These files contain private business data. Confirm the conservative duplicate exclusions are acceptable for the intended use.
2. Start Phase 5 by agreeing on the `TARGET_MASTER` schema and identity rules, then populate and validate reviewed configurations. The current tab is empty and is not consumed by the pipeline.
3. Complete Phase 3 by creating and testing approved output tabs in a dedicated test spreadsheet. Do not use the production spreadsheet for initial writer testing; `raw` remains read-only.
4. If the 57-second runtime is too long for operations, add temporary stage timings for validation, duplicate handling, eligibility, and aggregation before making further performance changes.
5. Review the Git worktree and handle it through the project's normal version-control process; do not commit credentials or output data.
