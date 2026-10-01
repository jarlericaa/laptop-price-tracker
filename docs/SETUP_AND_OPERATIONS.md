# Setup and Operations

## Prerequisites

- WSL with a supported Linux distribution, such as the project's existing Ubuntu environment.
- Python 3.11 or later and `python3-venv`.
- Access to the Google Cloud project and the `Laptops Brand Tracker` spreadsheet.
- A service-account JSON credential stored outside the repository.
- Git, for reviewing source and documentation changes.

The project declares Python `>=3.11`; dependencies are listed in `requirements.txt` and `pyproject.toml`.

## WSL setup

From WSL, change to the repository directory:

```bash
cd ~/laptop-price-tracker
```

Create and activate a virtual environment, then install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

If `.venv` already exists, activate it and install the requirements again only when dependencies change.

## Google Cloud and service-account setup

1. Select or create the Google Cloud project used for this integration.
2. Enable the Google Sheets API for that project. See Google's [Sheets API Python setup](https://developers.google.com/workspace/sheets/api/quickstart/python).
3. Create a service account and a JSON key only if organizational policy allows downloadable keys. See Google's [service-account creation guide](https://docs.cloud.google.com/iam/docs/service-accounts-create) and [key-management guide](https://docs.cloud.google.com/iam/docs/keys-create-delete).
4. Store the JSON file outside this repository, for example under `~/.config/laptop-price-tracker/`. Restrict filesystem access to the user that runs the pipeline.
5. Share the spreadsheet directly with the service account's email address. Give it Viewer access for read-only checks. Editor access is needed only if a future approved run will write output tabs.
6. Do not grant broad Google Cloud project roles just to read a spreadsheet. Spreadsheet sharing and OAuth scopes are separate controls.

The current repository is configured for service-account authentication. The Sheets API must be enabled before the client can use it. Google may take a short time to make a newly created service account available.

## Environment configuration

Copy the example and edit the local file:

```bash
cp .env.example .env
```

Use placeholders like these; never paste real values into documentation or source control:

```dotenv
GOOGLE_SERVICE_ACCOUNT_FILE=/home/YOUR_WSL_USERNAME/.config/laptop-price-tracker/google-service-account.json
SPREADSHEET_ID=YOUR_SPREADSHEET_ID
RAW_TAB_NAME=raw
MAX_VALIDATION_ERRORS=1000
SKIP_FRESHNESS_CHECK=false
```

`GOOGLE_SERVICE_ACCOUNT_FILE` must point to the external credential file. `RAW_TAB_NAME` defaults to `raw`. Keep `.env` and the JSON key outside Git; `.gitignore` excludes them. `GOOGLE_SERVICE_ACCOUNT_JSON` is also accepted by the code, but storing JSON text in environment variables is not the documented local setup.

## Required and future worksheet tabs

- **`raw`:** Production source of truth. It must remain read-only for this pipeline.
- **`TARGET_MASTER`:** Master copy of approved configurations. The user later reported that the tab has its header and no data rows. The local target header was revised on 2026-09-29; replace the existing header with `config/target_master_template.csv` before validating against the current schema.
- **`SOURCE_MODEL_MAP` and `MATCH_REVIEW`:** Supporting Phase 5 tabs. The user reported that both tabs have their template headers and no data rows. The local `MATCH_REVIEW` template was expanded with the current product detail fields; update that tab's header from `config/match_review_template.csv`. `check_target_tables.py --from-sheets` reads these tabs but never creates or changes them.
- **`P1_*` outputs:** The user reported that all nine approved output tabs were created header-only and verified in the real spreadsheet on 2026-09-29. A separate synthetic writer test completed, and a user-provided read-only report confirmed all nine test tabs and headers, one synthetic laptop in the target outputs, the charger in excluded listings, and a run-log `top_100_count` of 1. The report did not list or inspect `raw`.
- **Eligibility and duplicate decisions:** Current run inputs are local CSV files under `config/`. The remote `P1_ELIGIBILITY_DECISIONS` tab is preserved/merged by the optional writer but is not loaded into current-run classification.

The production output tabs now exist according to the user's report. The first application-writer test completed against a separate test spreadsheet with synthetic data. Continue to use a dedicated test spreadsheet for any further write testing; do not test the writer on production tabs.

## Read-only raw smoke test

With `.venv` active and `.env` configured:

```bash
python smoke_test.py
```

The command reads the configured raw tab, validates the required headers, reports the spreadsheet and worksheet names and row count, and checks that sample values are strings. It does not modify the Sheet. A successful check on 2026-09-28 reported 23,544 data rows and all expected headers.

## Read-only Phase 5 table check

```bash
python check_target_tables.py --from-sheets
```

This command reads `TARGET_MASTER`, `SOURCE_MODEL_MAP`, and `MATCH_REVIEW` from the configured spreadsheet using the read-only scope and `get_all_values()` only. It validates each tab against the current templates and writes local reports under `data/output/`. It prints table names, counts, and error codes, but never the spreadsheet title or cell values. A missing tab is reported and fails the check. The command never creates, clears, or writes tabs. The user reported all three tabs were created with headers and no data rows; the current local `TARGET_MASTER` and `MATCH_REVIEW` templates have since changed. Update those two sheet headers manually to match `config/target_master_template.csv` and `config/match_review_template.csv`, then run the check. Expect `NO_DATA_ROWS` from `TARGET_MASTER` until approved rows are added.

## Local Phase 5 CSV checks and candidate seeding

This check validates a local CSV against the approved schema in [TARGET_MASTER_SCHEMA.md](TARGET_MASTER_SCHEMA.md). It needs no credentials and makes no Google calls. Start from the header-only `config/target_master_template.csv`. Keep real exports outside Git, for example under the ignored `data/review/` folder.

```bash
python check_target_master.py data/review/target_master_export.csv
```

- **Always writes:** `data/output/target_master_validation_errors.csv`. It is ignored by Git and can contain cell values.
- **On a full pass only:** also writes `data/output/target_master_validated.csv`.
- **Console output:** counts and error codes only.
- **Exit code:** `0` means the check passed; `1` means it failed.

The template alone fails with `NO_DATA_ROWS`, as expected. This command never reads or writes the worksheet.

To check the mapping and review tables, or all three together, run:

```bash
python check_target_tables.py --target-master data/review/target_master_export.csv \
  --source-model-map data/review/source_model_map_export.csv \
  --match-review data/review/match_review_export.csv
```

`--source-model-map` requires `--target-master`. The command writes `<table>_validation_errors.csv` for each table, and writes `<table>_validated.csv` only for tables that pass. Both go under `data/output/`.

To create the initial local review worklist from the accepted Top 100, run:

```bash
python seed_match_review.py
```

This writes `data/review/match_review_candidates.csv` with one `NEW` row per distinct Top 100 model ID. All specifications, URLs, and approvals are blank. The command refuses to overwrite an existing file and stops without writing if `data/output/top_100_targets.csv` is missing; run `python run_phase1.py --dry-run` first. It never touches Google Sheets or creates `TARGET_MASTER` or mapping rows.

## Offline scraping framework and adapter tests (overall Phases 6-7)

```bash
python -m pytest -q tests/test_offers.py tests/test_scraping.py tests/test_laptop_factory.py
python -m pytest -q tests/test_datablitz.py tests/test_pcx.py tests/test_pcc.py tests/test_oct.py
```

These tests use fixtures under `tests/scraping_fixtures/`, a fake transport, and a fake clock. They need no credentials and no network; any socket connection fails the test. There is no scraping command or CLI.

Laptop Factory live use is enabled under the project-owner policy in [DECISIONS.md](DECISIONS.md). Its external-policy finding remains UNKNOWN, so this is not a claim that retailer permission was verified. The user reported one live smoke test, which still needs a bare-URL recheck. There is no scraping CLI: a Python call using `scrape(..., Fetcher())` can send live requests. Maintenance notes are in [adapters/LAPTOP_FACTORY.md](adapters/LAPTOP_FACTORY.md). DataBlitz (`DataBlitzAdapter`, host `ecommerce.datablitz.com.ph`) is enabled the same way, with the same UNKNOWN external-policy finding, and the user reported a one-product live spot check. Its notes are in [adapters/DATABLITZ.md](adapters/DATABLITZ.md). PC Express (`PCXAdapter`, host `pcx.com.ph`) is enabled by an explicit project-owner decision. Its external-policy finding remains RESTRICTED because the retailer's terms require written consent for automated collection; no written consent is recorded. A user-reported call returned one in-stock Acer offer; the offer URL appeared Markdown-formatted in the report and needs a `repr()` check. Its notes are in [adapters/PCX.md](adapters/PCX.md). PC Central (`PCCAdapter`, host `pccentralcomputerstore.com`) is enabled under the project-wide owner decision. Its external-policy finding is UNKNOWN. Build its requests with `product_request(url)`. A product sold in several variants needs `?variant=<id>` (or `product_request(url, variant_id)`); a bare URL for such a product fails with `AMBIGUOUS_VARIANT`. A bare-URL check returned that failure; a selected-variant live check returned one in-stock ASUS VivoBook offer. Never use the old `pccentral.com.ph` domain, which redirected to an unrelated site on 2026-10-01. Its notes are in [adapters/PCC.md](adapters/PCC.md). Octagon (`OCTAdapter`, host `www.octagon.com.ph`) is enabled under the project-wide owner decision. Its external-policy finding is RESTRICTED, because Octagon's terms prohibit scraping; no Octagon permission is recorded. Build its requests with `product_request(url)` on the `www` host; the apex `octagon.com.ph` is refused. The same `?variant=<id>` rule as PC Central applies to multi-variant products. A user-reported live check returned one in-stock HP 15-FC0413AU offer at PHP 37,999. Its notes are in [adapters/OCT.md](adapters/OCT.md). The retrieval defaults (20 s timeout, 3 attempts, 5/10 s backoff capped at 60 s, and at least 10 s between requests per retailer) are documented in [SCRAPING_FRAMEWORK.md](SCRAPING_FRAMEWORK.md#retrieval-defaults). Scraping log lines use the `laptop_tracker.scraping` logger and contain redacted URLs and error codes only.

## Safe test-tab write

The application has no standalone arbitrary test-tab writer. The writer accepts only named `P1_*` tabs, and there is no `P1_TEST` destination. **Do not use the production spreadsheet for a write test.**

For a future full-pipeline write test using `run_phase1.py --write-sheets`:

1. Create a separate test spreadsheet with a small, reviewed `raw` fixture and the required headers.
2. Configure the service account as an editor on that test spreadsheet only.
3. Temporarily set `SPREADSHEET_ID` in a local test environment to the test spreadsheet ID. Do not alter the production `.env` without preserving its values securely.
4. Ensure the fixture passes freshness checks or deliberately set `SKIP_FRESHNESS_CHECK=true` for that test environment. Resolve all duplicate conflicts so the pipeline can reach the writer.
5. Review local outputs, then run `python run_phase1.py --write-sheets` against the test spreadsheet. This replaces only approved `P1_*` outputs, updates the decision tab with merge behavior, and appends the run log. It cannot target `raw`.
6. Confirm the test outputs and restore the normal local environment.

On 2026-09-29, a direct synthetic-data integration test of `write_phase1_outputs()` completed against a separate test spreadsheet. A user-provided read-only inspection report confirmed the expected output tabs, headers, synthetic rows, and run-log count. The test was not run through `run_phase1.py --write-sheets`. Do not use `--write-sheets` as a verification command against the real spreadsheet.

### Writer failure and recovery

The Sheets writer is sequential, not transactional. It reads and rewrites `P1_ELIGIBILITY_DECISIONS` first, replaces generated output tabs one at a time, and appends to `P1_RUN_LOG` last. If an API call fails midway, earlier tabs may already contain new data while later tabs still contain old data. A successful log entry is not guaranteed for a partial run. Retrying can append another run-log row; output tabs are replaced, while remote decision rows are merged and retained.

- If the decision-tab header check fails, it stops before clearing or writing any output tab.
- If a later write fails, stop and inspect the test spreadsheet before retrying. For a disposable synthetic test sheet, discard it and repeat with a fresh test spreadsheet if its state is unclear.
- Before any future production write, make a separate backup copy of the spreadsheet. If a write fails, restore the affected approved `P1_*` tabs from that copy, including `P1_ELIGIBILITY_DECISIONS` if it changed. Do not restore or edit `raw`.
- After recovery, fix the reported cause and verify the tabs before retrying. Expect a retry to append an additional run-log row.

## Local dry run

The default command is local-output-only. `--dry-run` makes that intent explicit:

```bash
python run_phase1.py --dry-run
```

It reads `raw`, validates and ranks data, and writes files under `data/output/`. A run with unresolved duplicate or model-identity conflicts writes local diagnostics and exits safely without writing Sheets. The latest read-only live run completed in 57.49 seconds after fingerprint-matched decisions excluded 21 historical conflicting keys. It generated the expected local reports; review them before treating the ranking as business-approved. No Sheets output was written.

Expected files are `top_100_targets.csv`, `eligible_model_ranking.csv`, `excluded_listings.csv`, `eligibility_review.csv`, `conflicting_duplicates.csv`, `duplicate_review.csv`, `validation_errors.csv`, `period_summary.csv`, and `phase1_run_summary.json`. These are local business data and must not be committed or shared casually.

## Explicit write-enabled command

The code supports an explicit output write path:

```bash
python run_phase1.py --write-sheets
```

This reads the same raw source, completes local processing first, then requests the write scope and writes only allowed `P1_*` tabs. It is **not** part of normal setup, was not run during documentation work, and must only be used after reviewing the local output and resolving conflicts. Prefer a dedicated test spreadsheet for the first write.

## Troubleshooting

| Symptom | Likely cause and response |
|---|---|
| Missing `SPREADSHEET_ID` or credential error | Check `.env` variable names and the external JSON path without printing the JSON contents. |
| Spreadsheet not found or access denied | Confirm the ID and share the spreadsheet directly with the service-account email. |
| Sheets API error | Confirm the Sheets API is enabled for the Cloud project associated with the service account. |
| Raw worksheet missing | Check `RAW_TAB_NAME`; default is `raw`. |
| Missing or repeated headers | Compare the first row with the required raw columns in [the PRD](PRD.md#input-data). Do not rename or alter the production source as a troubleshooting shortcut. |
| Stale source month | Confirm the source includes current Manila-month data. Disable freshness only for intentional historical test fixtures. |
| Too many validation errors | Review the private `validation_errors.csv`; fix the source or reviewed data-quality issue before increasing the threshold. |
| Unresolved duplicate/model-identity conflicts | Review the private conflict report and redacted `duplicate_review.csv`; record an authorized decision in `config/duplicate_decisions.csv`. Stale fingerprints require a new review. |
| `TARGET_MASTER` check reports a header error | Ensure the sheet header exactly matches `config/target_master_template.csv`. The current target header was revised to the new 1:1 matching fields. |
| `check_target_tables.py` reports a header error for `SOURCE_MODEL_MAP` or `MATCH_REVIEW` | These headers must match the `config/` template exactly, including column order. |
| `check_target_tables.py --from-sheets` reports a worksheet not found | Create the tab manually with the template headers. The command never creates tabs. |
| `check_target_master.py` reports header or row errors | Compare the header with `config/target_master_template.csv`, then review the local `target_master_validation_errors.csv`. Duplicates are never resolved automatically; correct or remove rows in the source. |
| Pipeline appears slow | Unit aggregation performance does not represent total runtime. Time each processing stage on a safe local-only run before changing the implementation. |
| Write-enabled run does not create outputs | Check for a pipeline safety failure first; unresolved conflicts stop before Sheets writes. Confirm service-account Editor access only for the intended test spreadsheet. |

## Security checklist

- [ ] Credential JSON is outside the repository and has restricted filesystem access.
- [ ] `.env` contains only local settings and is not staged in Git.
- [ ] The service account is shared only on the required spreadsheet with the minimum needed access.
- [ ] Read-only commands use the read-only scope; no write test targets production.
- [ ] `raw` remains unchanged.
- [ ] Local `data/output/` reports are treated as private business data.
- [ ] No raw rows, spreadsheet IDs, service-account emails, or credential values are copied into docs, logs, commits, or support messages.
- [ ] `--write-sheets` is used only after review and with an explicitly chosen destination.

## Monday operating procedure after automation exists

There is no scheduled Monday workflow today. When Phase 11 is implemented and approved, an operator should:

1. Confirm the scheduled workflow ran with the expected source month and run ID.
2. Review validation errors, duplicate decisions, model-identity conflicts, eligibility review, and price conflicts.
3. Confirm the ranking count and Top 100 output; investigate unexpected changes before publication.
4. Confirm only approved output tabs changed and `raw` was not modified.
5. Record the run result and unresolved issues in the progress log; notify the named owner if a safety gate blocks publication.
6. Avoid rerunning a write-enabled job until reviewed corrections and destination are confirmed.

This procedure is planned operations guidance, not evidence of existing automation.
