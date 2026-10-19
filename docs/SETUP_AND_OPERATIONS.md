# Setup and Operations

For a plain-language weekly checklist and copyable commands, start with [Weekly workflow and dataflow](WEEKLY_WORKFLOW.md). This page provides detailed setup and command contracts.

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
- **`TARGET_MASTER`:** Master copy of approved configurations. Its header matches `config/target_master_template.csv`; on 2026-10-12 it held 205 targets (193 `CONFIGURATION`, 12 `LISTING_ONLY`).
- **`SOURCE_MODEL_MAP` and `MATCH_REVIEW`:** Supporting Phase 5 tabs, with 360 mappings and 5,154 review rows on 2026-10-12. Their headers match the `config/` templates (`MATCH_REVIEW` may use any column order). `check_target_tables.py --from-sheets` reads these tabs but never creates or changes them.
- **`PILOT_PRICE_COMPARISON`, `PILOT_PRICE_HISTORY`, `PILOT_PRICE_REVIEW`, `PILOT_PRICE_RUN_LOG`:** Phase 10 pilot outputs, created and written only by `publish_prices.py --write-sheets` (first published 2026-10-12). See [Publishing pilot results](#publishing-pilot-results-to-google-sheets-phase-10-manual).
- **`P1_TOP_100_CONFIGURATIONS`:** The published **Top 100 verified configurations** working list (owner publication, 2026-10-12; header in row 10, data in A11:U110). No repository command writes this tab.
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

This command reads `TARGET_MASTER`, `SOURCE_MODEL_MAP`, and `MATCH_REVIEW` from the configured spreadsheet using the read-only scope and `get_all_values()` only. It validates each tab against the current templates and writes local reports under `data/output/`. It prints table names, counts, and error codes, but never the spreadsheet title or cell values. A missing tab is reported and fails the check. The command never creates, clears, or writes tabs. On 2026-10-12 it passed all three tabs (205 targets, 360 mappings, 5,154 review rows). Each passing table also gets a `<table>_validated.csv` copy, which downstream commands (`match_offers.py`, `build_verified_top_100.py`, and `track_prices.py`) read.

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

## Offline price comparisons and history (overall Phase 9)

```bash
python track_prices.py \
  --observations data/output/pilot_matching_2026-10-08/observations.csv \
  --week-start 2026-10-05 --as-of 2026-10-12T09:00:00+08:00
```

The command:

1. Validates the three catalog snapshots (`data/output/*_validated.csv` by default), the selected list (`data/output/verified_top_100/top_100_verified_configurations.csv`), and its contribution ledger, then reads every `--observations` file completely. Any failure stops the run before the store is touched.
2. Imports each observation file into `data/history/price_history.sqlite3` in one transaction. Exact repeats are counted, not stored again; a record that contradicts a stored one refuses the whole file.
3. Matches every stored observation that has no result under the current matching context (catalog SHA-256 plus rule version) with the five-field `ExactMatcher`.
4. Writes the weekly comparison, retailer selections, weekly series, price history, review queue, configuration-to-source ledger, and run summary under `data/output/price_tracking/week_<week-start>/`.

- `--week-start` must be a Monday; weeks run Monday 00:00 to Monday 00:00 in Asia/Singapore.
- `--as-of` is required and needs a UTC offset. It is the cut-off for observations, so a rerun with the same dates gives the same reports. Use a time after the week's end for a closed (historical) week.
- `--observations` is optional and repeatable. Without it, the run reports from what the store already holds.
- `--history-start <Monday>` limits the history window; the default is the first week with a stored observation.
- `--sales-ranking` (default `data/output/eligible_model_ranking.csv`, used only if present) adds each source's Shopee snapshot price to the ledger; `--no-sales-ranking` skips it. It must be the same sales run as the list.
- The console shows counts only. Reports and the store contain business data and stay Git-ignored.
- Rerunning is safe. To start a history from scratch, move the store file aside; never edit it, because the tables are append-only by design.

Policies, identities, and file columns are in [PRICE_TRACKING_OUTPUT_SCHEMA.md](PRICE_TRACKING_OUTPUT_SCHEMA.md#phase-9-implementation-2026-10-12). A supervised collector can call `persist_scrape_results()` from `laptop_tracker.observation_store` to store `scrape()` results directly; the manual `collect_and_match.py` collector is implemented; no schedule exists.

## Manual online collection and matching

Phase 10 started on 2026-10-12 with seven verified configurations at Laptop Factory. The first
cycle yielded seven expected exact matches, three comparable prices and four stock exclusions.
See [PHASE_10_PILOT.md](PHASE_10_PILOT.md) for scope, artifacts, subsequent cycles and acceptance gates.

This is a manual, supervised path for a short list of known product pages. It is not the
multi-retailer, multi-week pilot and has no schedule.

**1. Write a manifest.** Copy `config/collection_manifest_template.csv` to a Git-ignored path, such
as `config/collection_manifest.csv` or a folder under `data/collection/`.

| Column | Rule |
|---|---|
| `request_id` | Required and unique: letters, digits, `_`, `.`, or `-`, starting with a letter or digit, at most 64 characters. |
| `retailer_id` | `laptop_factory`, `datablitz`, `pcx`, `pcc`, or `oct`. |
| `product_url` | One product page, accepted unchanged by that retailer's `product_request()`. For example, Laptop Factory needs `https://laptopfactory.com.ph/product/<slug>/` with no query. PC Central and Octagon accept only `?variant=<digits>`; a variant is never added or guessed, and a bare multi-variant URL fails with `AMBIGUOUS_VARIANT`. |
| `expected_target_id` | Optional verification hint. If given, it must be in the selected working list (an ACTIVE `CONFIGURATION`). It never decides the match. |
| `enabled` | `TRUE` or `FALSE`. Disabled rows are still validated. |

Cells must have no surrounding spaces. Two rows for the same page (host case and trailing slash
ignored) are refused, and so is a manifest with no enabled row. Any issue refuses the whole file
before a request is sent; messages name the row, field, and code, never the value.

**2. Preview, then collect.**

```bash
python collect_and_match.py --preview --manifest config/collection_manifest.csv
python collect_and_match.py --live --manifest config/collection_manifest.csv
```

- **Preview** validates `TARGET_MASTER` (default `data/output/target_master_validated.csv`, or
  `--target-master-from-sheets` read-only), the selected list and its contribution ledger, and
  the manifest. It writes the request plan. It sends no request and does not open the store.
- **Live** repeats the validation, then fetches each enabled page once, in manifest order. It uses
  one shared `Fetcher`, so the per-retailer throttle (at least 10 s), timeouts, bounded retries,
  checked redirects, host allowlists, and the live-use gate apply unchanged.
- Valid observations are stored with `persist_scrape_results()` in `--store` (default
  `data/history/price_history.sqlite3`). They are matched under the current matching context;
  results under earlier contexts are kept.
- Failed requests become rows in `request_failures.csv` with the stage, code, attempts, and HTTP
  status. A failure never becomes a price, a stock status, or an observation.

**3. Read the run directory.** Each run gets a new directory,
`data/output/collection_runs/<UTC timestamp>_<mode>/` (or `--output-dir`, which must not exist):

| File | Contents |
|---|---|
| `request_report.csv` | One row per manifest row: `SUCCEEDED`, `PARTIAL`, `FAILED`, `NO_OFFERS`, `SKIPPED_DISABLED`, or `NOT_SENT_PREVIEW`, with observation IDs and failure codes. |
| `request_failures.csv` | One row per failure: request ID, URL, redacted URL, stage, code, message, attempts, HTTP status. |
| `observations.csv` | `to_full_record()` columns; accepted by `match_offers.py` and `track_prices.py`. |
| `match_results.csv` | `match_offers.py` result columns plus request ID, `expected_target_check` (`NO_EXPECTATION`, `AS_EXPECTED`, `DIFFERENT_TARGET`, `NOT_EXACT`), `selected_list_status` (`SELECTED`, `NOT_IN_SELECTED_LIST`), observation ID, matching context, price, currency, price type, and availability. |
| `match_review_queue.csv` | Results with `review_reasons`: `MATCH_<status>`, `EXPECTED_TARGET_DIFFERS`, `EXPECTED_TARGET_NOT_MATCHED`, `EXACT_TARGET_NOT_IN_SELECTED_LIST`. |
| `match_evidence.csv` | Extraction and match evidence per field. |
| `collection_run_summary.json` | Timestamps, input SHA-256 values, matching context, adapter versions and live-use flags, fetch settings, store counts and import statistics, and outcome counts. |

The console shows counts and codes only. Exit codes: `0` success or preview, `2` partial success,
`1` failure (including no valid observation). All files, manifests, and stores stay Git-ignored.

DataBlitz, PC Express, and Octagon pages have no model label, so their observations are kept as
`REVIEW_REQUIRED` with `MISSING_MODEL`. No model is taken from a title or URL.

**Smoke check (2026-10-12).** One Laptop Factory page,
`https://laptopfactory.com.ph/product/acer-aspire-ai-14-a14-11m-x3bp/`, with hint TGT-000226. It
used a separate store (`data/history/smoke/`) and run directory
(`data/output/collection_runs/smoke_lf_2026-10-12/`). Results are in
[PROGRESS_LOG.md](PROGRESS_LOG.md).

## Publishing pilot results to Google Sheets (Phase 10, manual)

`publish_prices.py` publishes **reviewed, saved** `track_prices.py` reports for the authorized pilot
configurations. It is separate from the legacy `run_phase1.py --write-sheets` writer and runs only
when an operator starts it.

```bash
python publish_prices.py --preview \
  --report-dir data/output/phase10_2026-10-12/cycle_01/with_retailer_links \
  --pilot-scope data/collection/phase10_2026-10-12/pilot_scope.csv
python publish_prices.py --write-sheets \
  --report-dir data/output/phase10_2026-10-12/cycle_01/with_retailer_links \
  --pilot-scope data/collection/phase10_2026-10-12/pilot_scope.csv
```

- **Inputs.** The report folder must hold `weekly_price_comparison.csv` (with the
  `<code>_Offer_URL` columns), `weekly_price_selections.csv`, `price_history.csv`,
  `price_review_queue.csv`, and `price_tracking_run_summary.json`. Collection evidence defaults to
  `<report-dir>/../collection` (`--collection-dir`). The verified selected list is the input file
  whose SHA-256 the summary recorded (`--selected-list` overrides it).
- **Validation before any Sheets access.** The checks are:
  - unique, well-formed scope IDs that are in the reviewed selected list, with rank and five-field
    identity agreeing between the scope, the report, and the list;
  - one week and cutoff across the reports;
  - every comparison price is the in-week, exact, PHP offer recorded in the selections file, and
    its link is that offer;
  - a blank price has no invented link: only a current-week exact offer may supply a fallback
    link (for example, out of stock);
  - the lowest price and retailers agree with the retailer prices;
  - history stays within the cutoff, and collected observations are present in the history.

  Any issue stops the run and lists every problem.
- **Destination.** Exactly four tabs in the configured spreadsheet: `PILOT_PRICE_COMPARISON`,
  `PILOT_PRICE_HISTORY`, `PILOT_PRICE_REVIEW`, `PILOT_PRICE_RUN_LOG`. Names are matched exactly.
  Every write request is checked against those tabs' sheet IDs and allowed operations. An existing
  destination tab whose header was not written by this publisher is refused. `raw`, the catalog
  tabs, `P1_TOP_100_CONFIGURATIONS`, and the `P1_*` outputs are never read or written.
- **Preview** uses the read-only scope. It prints the reporting week, cutoff, Shopee sales-period
  label, scope and outcome counts, and each destination tab's current state and planned rows. It
  makes no Sheet mutation.
- **Write** uses the write scope:
  1. Saves a local JSON backup of every existing, non-empty publisher tab.
  2. Creates missing publisher tabs.
  3. Writes an `IN_PROGRESS` run-log row, then replaces each data tab's values, number formats,
     links, and bold. Other destination formatting is kept. It adds a frozen header row and a
     filter.
  4. Reads every cell back and compares values, links, and PHP formats, and checks that no stray
     cells remain.
  5. Updates the run-log row to `SUCCESS`, `READBACK_FAILED`, or `PARTIAL_FAILURE`.

  Only `SUCCESS` exits with `0`. Text is sent as literal strings, never formulas. Prices are
  numbers formatted as PHP. Links are cell links on the full URL text, so variant parameters stay
  intact.
- **Repeats** replace the four tabs' content and update the same run-log row (keyed by a
  publication ID derived from the input fingerprints; its attempt count increases), so history is
  never duplicated. The observation store is not opened.
- **Recovery.** A failure part-way records which tabs were written, in the run log and in
  `data/output/sheet_publications/<UTC time>_write/publication_report.json`. Fix the cause and
  rerun the same command; it replaces all four tabs. Each request has an HTTP timeout, so a
  stalled network call fails rather than hangs.
- **Backups.** The legacy writer's rule is a separate copy of the whole spreadsheet. The service
  account has only the Sheets scope, and copying a spreadsheet needs Drive access. The publisher
  therefore backs up its own existing tabs locally before replacing them. Make a whole-spreadsheet
  copy manually before a publication if the owner wants one.

## Automatic product discovery (Phase 10, manual runs)

`discover_products.py` searches the five retailers for verified configurations, ranks the
results, inspects the best candidates' own structured data, writes a manifest that
`collect_and_match.py` accepts, and (with `--collect`) collects and matches those pages through
the existing collector. Nothing is scheduled; every run is started by a person.

```bash
# 1. Preview: validates the catalog, working list, scope, and approvals file; writes the query
#    plan and request limits; sends no request.
python discover_products.py --preview --scope-file <pilot_scope.csv> \
  --target-master <inputs>/target_master_validated.csv \
  --selected-list <inputs>/top_100_verified_configurations.csv \
  --contributions <inputs>/verified_configuration_contributions.csv \
  --output-dir data/output/<run>/discovery_preview

# 2. Live search, ranking, and inspection only (writes manifest.csv for review).
python discover_products.py --live --targets TGT-000059 <same inputs> \
  --max-total-requests 20 --max-search-requests 10 --max-product-fetches 8 \
  --baseline-manifest <earlier manifest.csv> --output-dir data/output/<run>/discovery_one

# 3. Search, collect, extract, match, and report in one run.
python discover_products.py --live --scope-file <pilot_scope.csv> <same inputs> \
  --max-total-requests 95 --max-search-requests 60 --max-product-fetches 30 \
  --max-search-pages 2 --max-candidates-per-target-retailer 2 --collect \
  --store data/history/price_history.sqlite3 --output-dir data/output/<run>/discovery_all
```

**Searches** (2026-10-19). Per target and retailer, in order:
1. The complete model code from `TARGET_MASTER`, with no specification filter.
2. The verified `manufacturer_part_number`, when the catalog has one.
3. The model code with the configuration's RAM and storage (`B13UC-3474PH 16GB 512GB`).
4. Brand + series. 5. Brand + series with CPU family, RAM, and storage.

Queries 2-5 are fallbacks: each runs only while the results so far hold no *plausible*
candidate (the complete code or verified part number in the title, with no title clue stating
another screen, CPU, RAM, or storage). A result showing only another RAM/storage configuration,
an unexplained suffix (`OPI`), or the code only in the URL keeps the search going; that result
is kept and explained, not discarded. `search_requests.csv` gives each query's `run_reason` and
`cache` source; `search_jobs.csv` gives the tiers run and why the job stopped
(`PLAUSIBLE_CANDIDATE_FOUND`, `ALL_QUERY_TIERS_TRIED`, `QUERY_LIMIT_PER_TARGET_RETAILER`,
`SEARCH_REQUEST_LIMIT_REACHED`, `TOTAL_REQUEST_BUDGET_REACHED`, `RETAILER_STOPPED_AFTER_ERRORS`).
`--max-queries-per-target-retailer` (default 5) caps the tiers.

**Limits, caching, seeds** (2026-10-19):
- An identical search or product page already answered in the run is reused (`SAME_RUN`); nothing
  is sent and no limit is used. `--search-cache <earlier discovery folder>` (repeatable) replays
  saved search/inspection responses (`SAVED:<folder>`), optionally only within
  `--cache-max-age-hours`. `--replay-only` sends nothing at all (an offline discovery replay of a
  saved run; it cannot be combined with `--collect`).
- `--min-seconds-between-requests` can raise, never lower, the per-retailer spacing.
- `--seed-urls <match_results.csv>` (repeatable) and `--seed-store <store>` add offer URLs that
  matched a scope configuration `EXACT` in an earlier collection as `HISTORICAL_VERIFIED_URL`
  candidates (up to `--max-seed-urls-per-target-retailer`, default 2). This recovers known Laptop
  Factory pages that its search omits while out of stock. A seed is re-fetched and re-matched; it
  never carries a match forward.
- A same-code page whose title states another configuration is collected
  (`COLLECT_CHECK_CONFIGURATION`, within the candidate limit) so its own specifications decide;
  `--no-collect-title-differences` restores review-only handling.

Retailer SKUs and unverified identifiers are never search terms. A search result is a candidate,
never a match, and the configuration's values never fill an offer's missing specification. The mechanisms were
inspected live before implementation (evidence: ignored `data/collection/search_inspection_2026-10-13/`):

| Retailer | Mechanism | Limits |
|---|---|---|
| Laptop Factory | WooCommerce `/?s=<q>&post_type=product`, server-rendered cards | 12 per page; `--max-search-pages` pages; out-of-stock products appeared absent from search |
| DataBlitz, PC Express, PC Central, Octagon | Shopify predictive search `/search/suggest.json` (JSON) | At most 10 results; no pagination; no variant list (DataBlitz's HTML search page is client-rendered and unusable) |

**Outcomes** are kept apart:
- `RESULTS` and `NO_RESULTS`: a readable answer.
- `SEARCH_ERROR`: the request failed.
- `UNSUPPORTED_LAYOUT`: a response that could not be read.
- `NOT_SENT_*`: a limit, the total budget, or a retailer stopped after two consecutive errors.

A failure is never reported as "not found", and one failing retailer does not stop the others.

**Ranking** (`candidates.csv`) explains every result. Categories:
- `CODE_MATCH`.
- `CODE_MATCH_WITH_UNEXPLAINED_SUFFIX`: for example `OPI`, kept as written.
- `VERIFIED_PART_NUMBER_MATCH`.
- `CODE_SPELLING_VARIANT`: spacing or punctuation differs.
- `CODE_IN_URL_ONLY`.
- `FAMILY_ONLY`.
- `DIFFERENT_MODEL_CODE`.
- `UNRELATED`.

Title RAM, storage, screen, and CPU clues, plus bundle wording, only order candidates; they never
fill a field. `configuration_difference` states a title's differing values (`ram_capacity_gb:
title 8 vs target 16`). Only the historical-seed, code, part-number, and URL categories can be
collected, up to `--max-candidates-per-target-retailer` per target and retailer.

**Variants.** For Shopify candidates, the product page is fetched once and its own JSON-LD offers
list the variants; no variant URL or ID is built.
- A variant is collected only when its option title states the target RAM and storage, and only
  at PC Central and Octagon, whose adapters can bind a `?variant=` page.
- Two drives such as `512GB + 512GB SSD` are not 512GB.
- Other variants are kept in `variants.csv` as alternatives or review items. The default,
  cheapest, or first variant is never chosen.

**Collection** runs `collect_and_match.py --live` on the validated manifest:
- It shares the run's request budget.
- It waits one per-retailer interval first.
- It backs up the store with SQLite's backup API (`store_backup_before_collection.sqlite3`).
- Expected target IDs are hints that the collector checks; the five-field matcher decides.
- Listing-specific owner approvals apply only to their exact recorded offer URL. The summary lists
  collected offers that hit one.

**Outputs:**
- `search_plan.csv`, `search_requests.csv` (URLs, times, hashes, outcomes), `search_results.csv`.
- `candidates.csv`, `product_inspections.csv`, `variants.csv`, `manifest.csv`.
- `coverage_vs_baseline.csv` (with `--baseline-manifest`).
- `evidence/<sha256>.html|json`, `collection/` (the collector's reports).
- `discovery_summary.json`: limits, outcomes, and retailer evidence, manufacturer evidence, and
  owner decisions kept separately.

**Limitations:**
- These requests identify themselves only by the `laptop-price-tracker/0.1` user agent. The
  Shopify stores' `agents.md` asks automated server-to-server traffic to use Web Bot Auth, which
  this project does not implement; reading public pages is described there as allowed.
- No authentication, CAPTCHA, or access control is bypassed; an HTTP 403 is a `SEARCH_ERROR`.

## Offline extraction replay and candidate discovery (Phase 10)

Both commands are offline: they make no requests, run no searches, never write Google Sheets,
never write the observation store, and never change matching, mappings, or the catalog. Each
writes into a new `--output-dir` that must not exist yet (keep it under the ignored `data/`), and
prints counts only. Example commands with real paths are in
[PHASE_10_PILOT.md](PHASE_10_PILOT.md#extraction-replay-and-candidate-discovery-2026-10-13).

**`replay_extraction.py`** replays a saved collection through the current code.

- Path 1 (always): the original observation CSV is matched again with the current extractor
  (`observation_replay/`).
- Path 2 (`--html-dir`): each saved body `<content_sha256>.html` is checked against its hash and
  re-parsed with the current adapter, reusing the original retrieval time and attempt count
  (`html_replay/`). Missing or altered bodies are listed in `reparse_failures.csv`, never guessed.
  `reparse_differences.csv` lists the record columns that changed. The re-parsed records are
  written to `reparsed_observations_NOT_FOR_IMPORT.csv`: they keep the original observation
  identity, so importing them would put changed content under an old identity. Do not import them.
- `--conflict-check-store` opens the named store read-only, copies it with SQLite's backup API,
  tries the import on the copy, and deletes the copy; the outcome is in `replay_summary.json`.
- `before_after_comparison.csv` puts the saved results (`--before-results`) beside both paths:
  status, target, reasons, primary core statuses, supporting fields, and the weekly price rule.
  `--cases` names review cases by content hash.

**`discover_candidates.py`** ranks saved observations as research candidates for reference
configurations (`--references`, the verified Top 100 layout; filter with `--targets` or
`--scope-file`) and plans search queries per retailer host.

- `discovery_candidates.csv`: one row per reference and linked candidate, with URL, variant ID,
  `COLLECTED` or `DISCOVERED_NOT_FETCHED` (a variant the page listed with its own JSON-LD URL but
  nobody fetched), title, the matched code and where it was seen (`LABELED_MODEL`,
  `LABELED_MODEL_WITH_UNEXPLAINED_SUFFIX`, `TITLE_ONLY`, `PAGE_TEXT_ONLY`, `URL_ONLY`), any
  retained suffix, identifiers with their source, per-field `AGREE` / `DIFFER` / `UNKNOWN`, and a
  next verification step.
- Categories are review priorities, not assignments: `STRONG_CANDIDATE` (labeled model code, no
  trusted field differs), `NEEDS_VERIFICATION` (code only in a title, URL, or page text, or with an
  unexplained suffix), `POSSIBLE_CANDIDATE` (brand and family in the title, no code),
  `DIFFERENT_CONFIGURATION` (same code, a trusted field differs: keep as a separate variant).
  Agreement uses only `EXTRACTED` values; supporting evidence is shown but never counts.
- `discovery_queries.csv`: per reference and retailer, in order: `A_COMPLETE_MODEL_CODE`;
  `B_FORMATTING_VARIATION` (spellings seen on saved pages, and an observed suffixed designation
  marked unverified); `C_VERIFIED_MANUFACTURER_PART_NUMBER` (from `TARGET_MASTER`, with
  `--target-master`); `C_SUPPLEMENTARY_UNVERIFIED` (part-number rows or part-number-length title
  tokens, marked unverified); `D_BRAND_FAMILY_SPECS`. Retailer SKUs, barcodes, and product or
  variant IDs are never used as queries. Comparisons ignore case, whitespace, and trademark marks;
  suffixes are kept.

## Offline scraping framework and adapter tests (overall Phases 6-7)

```bash
python -m pytest -q tests/test_offers.py tests/test_scraping.py tests/test_laptop_factory.py
python -m pytest -q tests/test_datablitz.py tests/test_pcx.py tests/test_pcc.py tests/test_oct.py
```

These tests use fixtures under `tests/scraping_fixtures/`, a fake transport, and a fake clock. They need no credentials and no network; any socket connection fails the test. The only collection command is `collect_and_match.py`; see [Manual online collection and matching](#manual-online-collection-and-matching).

Laptop Factory live use is enabled under the project-owner policy in [DECISIONS.md](DECISIONS.md). Its external-policy finding remains UNKNOWN, so this is not a claim that retailer permission was verified. The user reported one live smoke test, and a controlled bare-URL check on 2026-10-01 confirmed clean URL attribution. Build its requests with `product_request(url)` and a bare `https://laptopfactory.com.ph/product/<slug>/` URL; queries, fragments, and `http` are refused. For listed product pages, use `collect_and_match.py` (manual online collection); a direct Python call using `scrape(..., Fetcher())` can also send live requests. Maintenance notes are in [adapters/LAPTOP_FACTORY.md](adapters/LAPTOP_FACTORY.md). DataBlitz (`DataBlitzAdapter`, host `ecommerce.datablitz.com.ph`) is enabled the same way, with the same UNKNOWN external-policy finding, and the user reported a one-product live spot check. Its notes are in [adapters/DATABLITZ.md](adapters/DATABLITZ.md). PC Express (`PCXAdapter`, host `pcx.com.ph`) is enabled by an explicit project-owner decision. Its external-policy finding remains RESTRICTED because the retailer's terms require written consent for automated collection; no written consent is recorded. A user-reported call returned one in-stock Acer offer, and a controlled 2026-10-01 check confirmed with `repr()` that the offer URL is a plain string. Its notes are in [adapters/PCX.md](adapters/PCX.md). PC Central (`PCCAdapter`, host `pccentralcomputerstore.com`) is enabled under the project-wide owner decision. Its external-policy finding is UNKNOWN. Build its requests with `product_request(url)`. A product sold in several variants needs `?variant=<id>` (or `product_request(url, variant_id)`); a bare URL for such a product fails with `AMBIGUOUS_VARIANT`. A bare-URL check returned that failure; a selected-variant live check returned one in-stock ASUS VivoBook offer. Never use the old `pccentral.com.ph` domain, which redirected to an unrelated site on 2026-10-01. Its notes are in [adapters/PCC.md](adapters/PCC.md). Octagon (`OCTAdapter`, host `www.octagon.com.ph`) is enabled under the project-wide owner decision. Its external-policy finding is RESTRICTED, because Octagon's terms prohibit scraping; no Octagon permission is recorded. Build its requests with `product_request(url)` on the `www` host; the apex `octagon.com.ph` is refused. The same `?variant=<id>` rule as PC Central applies to multi-variant products. A user-reported live check returned one in-stock HP 15-FC0413AU offer at PHP 37,999. Its notes are in [adapters/OCT.md](adapters/OCT.md). The retrieval defaults (20 s timeout, 3 attempts, 5/10 s backoff capped at 60 s, and at least 10 s between requests per retailer) are documented in [SCRAPING_FRAMEWORK.md](SCRAPING_FRAMEWORK.md#retrieval-defaults). Scraping log lines use the `laptop_tracker.scraping` logger and contain redacted URLs and error codes only.

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
| `TARGET_MASTER` check reports a header error | Ensure the sheet header exactly matches `config/target_master_template.csv` (or the pre-tracking-mode legacy header). |
| `check_target_tables.py` reports a header error for `SOURCE_MODEL_MAP` or `MATCH_REVIEW` | `SOURCE_MODEL_MAP` must match its `config/` template exactly, including column order. `MATCH_REVIEW` may use any column order, but it must have exactly the template's columns (or the pre-tracking-mode set), with no blank, duplicate, misspelled, or extra header; the error lists which. |
| `check_target_tables.py --from-sheets` reports a worksheet not found | Create the tab manually with the template headers. The command never creates tabs. |
| `check_target_master.py` reports header or row errors | Compare the header with `config/target_master_template.csv`, then review the local `target_master_validation_errors.csv`. Duplicates are never resolved automatically; correct or remove rows in the source. |
| `track_prices.py` stops "before any store write" | A catalog table, the selected list, the contribution ledger, or an observation file failed validation. Rerun `check_target_tables.py`, or rebuild and audit the verified list, before retrying. |
| `track_prices.py` reports observations that "contradict stored records" | The file repeats an observation identity (same offer and retrieval time) with different content. Nothing from the file was stored. Investigate the source of the file; never edit the store to force it in. |
| `collect_and_match.py` stops "before any retailer request" | The catalog, selected list, ledger, or manifest failed validation. The message lists rows, fields, and codes; fix the manifest or rerun the catalog checks. Nothing was sent or stored. |
| `collect_and_match.py` exits with 2 or 1 | Partial success or failure. Read `request_failures.csv`; retry only the failed rows in a new manifest and run. |
| `publish_prices.py` stops "before any Sheets access" | A report, the scope, or the selected list is inconsistent. Fix or regenerate the reviewed reports; never edit published values by hand to make them pass. |
| `publish_prices.py` reports `PARTIAL_FAILURE` or `READBACK_FAILED` | Read `publication_report.json` (tabs written, readback issues, error). Rerun the same `--write-sheets` command after fixing the cause; it replaces all four pilot tabs. |
| Pipeline appears slow | Unit aggregation performance does not represent total runtime. Time each processing stage on a safe local-only run before changing the implementation. |
| Write-enabled run does not create outputs | Check for a pipeline safety failure first; unresolved conflicts stop before Sheets writes. Confirm service-account Editor access only for the intended test spreadsheet. |

## Security checklist

- [ ] Credential JSON is outside the repository and has restricted filesystem access.
- [ ] `.env` contains only local settings and is not staged in Git.
- [ ] The service account is shared only on the required spreadsheet with the minimum needed access.
- [ ] Read-only commands use the read-only scope; no write test targets production.
- [ ] `raw` remains unchanged.
- [ ] Local `data/output/` reports and the `data/history/` store are treated as private business data.
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

### Comparing displayed prices when stock is unavailable (2026-10-13)

The owner-approved rule includes exactly matched OUT_OF_STOCK displayed prices. The stock
status remains visible beside selected prices in the comparison tab and in history; a sold-out
price can be the lowest price. Other price eligibility rules continue to apply. Regenerate saved
reports under the new policy in a new directory, review the selections, then preview/publish.
The selected observation ID determines the status shown, not the status of another offer.
