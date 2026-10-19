# Weekly workflow: from Google Sheets to retailer prices

Updated: **2026-10-15**. This guide describes the commands that exist today.

## Quick navigation

- [Google Sheets tabs](#1-what-the-google-sheets-tabs-mean)
- [CSV and database dictionary](#2-where-the-files-come-from)
- [What the different dates mean](#3-understand-the-dates)
- [Weekly commands, step by step](#4-weekly-run-commands-in-order)
- [Which commands write to Sheets](#5-which-commands-actually-change-things)
- [Troubleshooting](#8-when-something-stops)
- [Weekly checklist](#9-weekly-checklist)

## Start here

The tracker answers two questions:

1. **Which laptops matter?** Shopee sales plus the approved catalog determine the verified Top 100.
2. **What prices do retailers display?** Automatic search finds pages; extraction and matching check the configurations; price reports compare the offers.

**Google Sheets changing does not change a saved CSV.** Refresh the inputs before each weekly run. The dated October pilot folders are historical snapshots, not live links to Sheets.

The program can search retailer websites automatically. You still start the commands yourself, review uncertain results and explicitly publish the reports. There is no weekly scheduler or single command covering every step yet.

### The dataflow

```mermaid
flowchart TD
    raw["Sheets: raw sales"] --> sales["Refresh eligible sales ranking"]
    catalog["Sheets: catalog, mappings, review"] --> copies["Validate and export catalog CSVs"]
    sales --> top["Build and audit verified Top 100"]
    copies --> top
    top --> scope["Choose a small pilot scope"]
    scope --> search["Search retailer websites"]
    search --> pages["Rank candidates and collect pages"]
    pages --> store["Local observations and match history"]
    copies --> store
    store --> reports["Generate weekly price reports"]
    reports --> preview["Review and preview publication"]
    preview --> output["Sheets: four PILOT_PRICE tabs"]
```

The `raw` and catalog tabs are inputs. The four `PILOT_PRICE_*` tabs are report outputs. Searching, matching and publishing are separate operations.

## 1. What the Google Sheets tabs mean

The workbook may also contain dated backups and additional manually created review tabs.

| Tab | What it contains | How it is refreshed |
|---|---|---|
| `raw` | Imported Shopee data, including `grass_date`, `shopid`, `itemid`, `modelid`, listing names, `current_price`, `gmv_usd`, orders and amount. | Your upstream weekly import. This project's commands read it; they do not replace it. |
| `TARGET_MASTER` | Approved configurations with stable target IDs and specifications. | Reviewed catalog edits. Weekly validation exports a copy; it does not add targets. |
| `SOURCE_MODEL_MAP` | Approved Shopee variation-to-target links. Several variations can point to one configuration. | Reviewed mapping edits. The weekly commands do not create mappings. |
| `MATCH_REVIEW` | Catalog candidates, missing specifications, conflicts and review history. | Human/evidence review. Validation does not promote candidates. |
| `P1_TOP_100_TARGETS` | Source-level sales Top 100: Shopee variations, not unique configurations. | Optional legacy P1 writer; local generation alone does not update this tab. |
| Other generated `P1_*` tabs | Eligible ranking, exclusions, eligibility review, duplicate conflicts, validation errors, period summary and run log. | Optional `run_phase1.py --write-sheets`; see section 7. |
| `P1_ELIGIBILITY_DECISIONS` | Existing Sheet copy of eligibility decisions. | The optional P1 writer preserves/merges it. Classification currently reads local `config/eligibility_decisions.csv`, so editing this tab alone does not change classification. |
| `P1_TOP_100_CONFIGURATIONS` | Published display of the verified configuration-level Top 100. | Separate/manual publication. Neither the build command nor price publisher updates this tab. |
| `PILOT_PRICE_COMPARISON` | Pilot configurations, retailer prices/links, stock/status labels and lowest price. | `publish_prices.py --write-sheets`. |
| `PILOT_PRICE_HISTORY` | Dated pilot observations, stock, matches, prices and changes. | Same publisher, from cumulative local history. |
| `PILOT_PRICE_REVIEW` | Retailer matching problems and price exclusions in the published history/scope. | Same publisher. Older unresolved observations can remain here. |
| `PILOT_PRICE_RUN_LOG` | Publication IDs, cutoffs, counts, success/failure and readback results. | Same publisher; retains earlier publications. |

### The IDs are different

- Shopee `modelid` / `source_model_id`: a marketplace variation, **not** a manufacturer model code.
- `target_id`, such as `TGT-000042`: one catalog configuration across retailers.
- Manufacturer code, such as `ANV15-42-R2UH`: part of the laptop's identity.
- Retailer variant ID: an option on one store's page, bound to selected RAM/storage.

### The five required identity fields

**Model + screen size + CPU family + RAM capacity + storage capacity.**

Example: `ANV15-42-R2UH / 15.6 / Ryzen 5 / 16 / 512`.

`CONFIGURATION` targets use this identity and can match across retailers. `LISTING_ONLY` targets track individual marketplace listings; they do not enter configuration matching or the verified Top 100. Missing secondary specifications do not automatically block a match, but explicit secondary conflicts can require review.

`MATCH_REVIEW` concerns building the catalog. `PILOT_PRICE_REVIEW` concerns retailer observations. They are different queues.

## 2. Where the files come from

### Input snapshots and rankings

| File | Source and purpose |
|---|---|
| `eligible_model_ranking.csv` | Processed `raw` data: full eligible Shopee source ranking. This is derived data, not a complete raw-sheet export. |
| `top_100_targets.csv` | Same sales refresh: source-level Top 100, retained as a reference. |
| `phase1_run_summary.json`, `period_summary.csv` | Sales run ID, selected months, counts and period information. |
| `target_master_validated.csv` | Live `TARGET_MASTER` after validation: catalog snapshot for matching. |
| `source_model_map_validated.csv` | Live `SOURCE_MODEL_MAP` after validation: approved source-to-target links. |
| `match_review_validated.csv` | Live `MATCH_REVIEW` after validation: catalog review state. |
| `verified_configuration_ranking.csv` | Sales ranking + catalog + mappings: all qualifying configurations ranked by combined M-1 and MTD GMV. |
| `top_100_verified_configurations.csv` | First 100 qualifying rows: working list for discovery and reporting. |
| `verified_configuration_contributions.csv` | Explains which source variations contributed sales to targets and which were excluded. |
| `verified_configuration_exclusions.csv` | Reasons sources/targets were excluded. |
| `verified_configuration_review_priority.csv` | High-priority unresolved catalog work. |
| `verified_configuration_run_summary.json` | Sales provenance, fingerprints, counts and shortfall below 100. |
| `pilot_scope.csv` | Chosen subset of the current verified list to search and publish. |

Validated copies may add `source_row_number` to trace problems back to Sheets. The snapshot readers handle it. Keep IDs as text when opening/editing CSVs.

### Discovery and collection outputs

| File/folder | What to look for |
|---|---|
| `search_plan.csv` | Queries and retailer search mechanisms. |
| `search_requests.csv`, `search_results.csv` | Requests, returned products, empty results, failures and budget outcomes. |
| `candidates.csv` | Candidate URLs, ranking reasons and collection decisions. |
| `product_inspections.csv`, `variants.csv` | Product inspections and RAM/storage variants. |
| `manifest.csv` | URLs selected for collection. Expected target IDs are checks, never assignments. |
| `discovery_summary.json` | Overall status, counts, limits and provenance. |
| `evidence/` | Saved search responses and inspected-page evidence. |
| `collection/observations.csv` | Fresh page observations: price, stock, raw specs, URL, timestamp and source hash. |
| `collection/match_results.csv` | Matched target or unresolved result for each offer. |
| `collection/match_evidence.csv` | Field-level source text, extraction and matching evidence. |
| `collection/match_review_queue.csv` | Matching problems from this collection. |
| `collection/request_report.csv`, `collection/request_failures.csv` | Collection success/failure details. |
| `collection/collection_run_summary.json` | Counts, fingerprints, store changes and matching context. |

Search can use titles and URLs to find candidates. A similar title does not fill missing trusted specifications. The collector reads the page and the matcher checks the five fields, including any explicitly recorded owner approvals.

### Price reports and persistent history

| File | Purpose |
|---|---|
| `data/history/price_history.sqlite3` | Persistent observations, import batches and versioned matches. History accumulates here; do not delete it each week. |
| `weekly_price_comparison.csv` | Retailer prices and lowest price for the verified list. |
| `weekly_price_selections.csv` | Selected offer per target/retailer, reason and observation ID. |
| `weekly_price_series.csv` | Weekly prices and changes. |
| `price_history.csv` | Dated observations under the current matching context. |
| `price_review_queue.csv` | Matching problems and excluded prices in report history. |
| `configuration_source_ledger.csv` | Marketplace links, historical GMV and available Shopee snapshot prices. |
| `price_tracking_run_summary.json` | Week, cutoff, policy, sales snapshot label, counts and fingerprints. |
| `publications/*/publication_report.json` | Preview/write outcome, local backups, destination links and readback issues. |

An unchanged page fetched next week is a **new dated observation**. Importing the identical saved observation again does not duplicate it. Do not import reparsed historical records with changed content under their original identities.

### Local decision files

| File | Purpose |
|---|---|
| `config/eligibility_rules.csv` | Laptop inclusion/exclusion rules. |
| `config/eligibility_decisions.csv` | Eligibility decisions used by the sales refresh. |
| `config/duplicate_decisions.csv` | Recorded handling of conflicting source duplicates. |
| `config/identity_review_register.csv` | Known unresolved catalog/source identity conflicts. |
| `config/retailer_evidence_approvals.csv` | Listing-specific owner approvals, including the approved OPI/storage interpretations. These do not apply generally to every listing. |

## 3. Understand the dates

| Date | Meaning |
|---|---|
| Sales period | M-1 is the previous month; MTD is the latest source month. Ranking uses combined GMV, not just the last seven days. |
| Sales extraction/run date | When the program processed `raw`. It does not independently prove the upstream Sheet was updated that day. |
| Catalog snapshot date | When the catalog/mappings were exported. It says nothing about sales freshness. |
| Retailer retrieval time | When the retailer page was fetched. |
| Report week/cutoff | Monday-to-Monday in Asia/Singapore, with an explicit latest included time. Current-week reports are provisional. |
| Publication time | When saved reports were copied to the tracker. Publishing does not refresh prices. |

The historical pilot used sales extracted September 28 (August + September MTD), even though its catalog and retailer observations were refreshed in October. Refresh sales first for new weekly runs.

The sales freshness guard checks that the latest source month is the current month in Asia/Manila (the same UTC offset as Singapore). It does **not** check whether an upstream weekly import occurred within seven days. Confirm that import through your normal source-data process.

## 4. Weekly run: commands in order

**Run these blocks in the same Ubuntu/WSL terminal, one step at a time.** If a command fails, stop and inspect it; do not continue using old outputs.

From Windows PowerShell, enter Ubuntu:

```powershell
wsl.exe -d Ubuntu
```

### Step 0 - Open the project and create a dated run folder

```bash
cd /home/phsp8184/laptop-price-tracker
source .venv/bin/activate
set -euo pipefail

weekly_run="data/output/weekly_runs/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$weekly_run/inputs" "$weekly_run/before_refresh"
export WEEKLY_RUN="$weekly_run"

# Preserve previous default CSVs before the sales command replaces them.
python - <<'PY'
import os
import shutil
from pathlib import Path

source = Path("data/output")
destination = Path(os.environ["WEEKLY_RUN"]) / "before_refresh"
for pattern in ("*.csv", "phase1_run_summary.json"):
    for path in source.glob(pattern):
        if path.is_file():
            shutil.copy2(path, destination / path.name)
print("Run folder:", os.environ["WEEKLY_RUN"])
PY
```

Your existing `.env` must point to the tracker and external service-account credentials. Do not copy credentials into this folder. See [setup and operations](SETUP_AND_OPERATIONS.md) for initial setup.

If the terminal closes, restore `weekly_run` and `WEEKLY_RUN` to the saved folder before continuing. Use a new output name such as `discovery_retry` for another live attempt; preserve earlier evidence.

### Step 1 - Refresh sales from the current raw Sheet

```bash
python run_phase1.py --dry-run | tee "$weekly_run/sales_refresh.log"

cp data/output/eligible_model_ranking.csv \
   data/output/top_100_targets.csv \
   data/output/phase1_run_summary.json \
   data/output/period_summary.csv \
   "$weekly_run/inputs/"
```

**Does:** reads live `raw`, validates rows, applies recorded duplicate decisions, determines M-1/MTD, classifies listings and creates sales rankings.

**Outputs:** default files in `data/output/`; the copy freezes this run's ranking and provenance. Inspect `phase1_run_summary.json` and `period_summary.csv` for expected months/counts.

`--dry-run` still reads Google Sheets and writes local files. It means **no Google Sheets writes**. This historical script name produces the sales-ranking inputs used by later phases.

Unresolved duplicate/identity conflicts or stale source months can stop this command. Review diagnostics in `data/output/` and decision files before continuing. Do not turn off freshness checks to present old sales as current.

### Step 2 - Export and validate the live catalog/mappings

```bash
python check_target_tables.py --from-sheets \
  --output-dir "$weekly_run/inputs" \
  | tee "$weekly_run/catalog_check.log"
```

**Does:** reads only `TARGET_MASTER`, `SOURCE_MODEL_MAP` and `MATCH_REVIEW`; checks headers, values, required fields and mapping references.

**Outputs:** three `*_validated.csv` snapshots plus validation reports. Continue when all three pass. No Sheet changes occur.

### Step 3 - Rebuild and audit the verified Top 100

```bash
python build_verified_top_100.py \
  --ranking "$weekly_run/inputs/eligible_model_ranking.csv" \
  --source-top-100 "$weekly_run/inputs/top_100_targets.csv" \
  --target-master "$weekly_run/inputs/target_master_validated.csv" \
  --source-model-map "$weekly_run/inputs/source_model_map_validated.csv" \
  --match-review "$weekly_run/inputs/match_review_validated.csv" \
  --output-dir "$weekly_run/verified_top_100" \
  | tee "$weekly_run/verified_top_100_build.log"

python audit_verified_top_100.py \
  --run-dir "$weekly_run/verified_top_100" \
  --ranking "$weekly_run/inputs/eligible_model_ranking.csv" \
  --target-master "$weekly_run/inputs/target_master_validated.csv" \
  --source-model-map "$weekly_run/inputs/source_model_map_validated.csv" \
  --match-review "$weekly_run/inputs/match_review_validated.csv" \
  | tee "$weekly_run/verified_top_100_audit.log"
```

**Does:** combines approved variations into unique configurations and ranks them using refreshed sales. The audit checks identities, mappings, totals and order.

**Check:** the run summary's dates, selected count and `shortfall`; inspect exclusions and review priorities. A successful build can report fewer than 100 if fewer qualify. New raw listings do not automatically gain verified specs or mappings.

These commands do not update `P1_TOP_100_CONFIGURATIONS`, `TARGET_MASTER`, or mappings in Sheets.

### Step 4 - Choose the pilot configurations

This example chooses the **first seven in this week's rebuilt list**. It is not automatically the same seven as the earlier October pilot. For a consistent comparison group, deliberately select retained target IDs and confirm they still belong to the current verified list.

```bash
python - <<'PY'
import csv
import os
from pathlib import Path

run = Path(os.environ["WEEKLY_RUN"])
with (run / "verified_top_100/top_100_verified_configurations.csv").open(
    encoding="utf-8", newline=""
) as handle:
    reader = csv.DictReader(handle)
    columns = reader.fieldnames
    selected = list(reader)[:7]
if len(selected) != 7:
    raise SystemExit("Fewer than seven verified configurations: review the ranking first.")
with (run / "pilot_scope.csv").open("w", encoding="utf-8", newline="") as handle:
    writer = csv.DictWriter(handle, fieldnames=columns)
    writer.writeheader()
    writer.writerows(selected)
print("Pilot scope: 7 configurations. Review pilot_scope.csv before collection.")
PY
```

The supervised pilot is 5-19 configurations. Rebuilding all 100 does not mean all 100 are searched. The following limits are a small-pilot example, not a full-100 budget.

### Step 5 - Preview automatic search

```bash
pilot_args=(
  --scope-file "$weekly_run/pilot_scope.csv"
  --target-master "$weekly_run/inputs/target_master_validated.csv"
  --selected-list "$weekly_run/verified_top_100/top_100_verified_configurations.csv"
  --contributions "$weekly_run/verified_top_100/verified_configuration_contributions.csv"
  --retailers laptop_factory,datablitz,pcx,pcc,oct
  --max-total-requests 95
  --max-search-requests 60
  --max-search-pages 2
  --max-candidates-per-target-retailer 2
  --max-product-fetches 30
)

python discover_products.py --preview "${pilot_args[@]}" \
  --output-dir "$weekly_run/discovery_preview"
```

**Does:** validates local inputs and writes a plan. No retailer request, store import or Sheet update occurs. Review queries, scope and limits.

### Step 6 - Search, collect, extract and match

```bash
python discover_products.py --live --collect "${pilot_args[@]}" \
  --store data/history/price_history.sqlite3 \
  --output-dir "$weekly_run/discovery" \
  | tee "$weekly_run/discovery.log"
```

**Does:** searches retailers, ranks candidates, inspects variants, creates a manifest, collects selected pages and stores/matches fresh observations. No assistant needs to search manually for this command to work.

The collector backs up history before import. Observations accumulate across weeks. New catalog/rule matching contexts preserve earlier results rather than overwriting them.

**Review:** the discovery summary, candidates, variants and `discovery/collection/` reports. There must be an actual collection with valid observations before proceeding. Discovery can finish with no collectable manifest; limits/failures can produce partial success. A nonzero exit means inspect the run, not discard successful evidence or blindly retry every request.

Finding a result does not guarantee a match. Since 2026-10-15 a page without a model row can still match when its title shows exactly one full model code and the other four fields come from the page's own specifications (`TITLE_MODEL_CORROBORATED`). Conflicting model rows or URLs, unexplained suffixes such as `OPI`, and ambiguous specifications stay in review.

**Known search gap:** Laptop Factory appeared to omit sold-out pages in the October 13 search pilot. Since 2026-10-19, add `--seed-store data/history/price_history.sqlite3` (or `--seed-urls <earlier collection>/match_results.csv`) to `pilot_args` so offer URLs that matched a scope configuration `EXACT` before are re-checked as `HISTORICAL_VERIFIED_URL` candidates; they are re-fetched and re-matched, never carried forward. Searches now continue past results that show only another RAM/storage configuration or an unresolved code (`search_jobs.csv` records the tiers run and why each job stopped). To replay a saved discovery offline, rerun it with `--live --replay-only --search-cache <saved discovery folder>` into a new output folder (no request is sent; no collection). Otherwise retain reviewed known URLs for recollection with `collect_and_match.py` when needed. `--baseline-manifest` only measures discovery coverage; it does not automatically fetch missing old URLs. Earlier-week prices are never carried into a new week as current prices.

### Step 7 - Generate weekly reports from local history

Run after collection finishes. The cutoff generated now includes completed retrievals.

```bash
week_start="$(python - <<'PY'
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

today = datetime.now(ZoneInfo("Asia/Singapore")).date()
print(today - timedelta(days=today.weekday()))
PY
)"

as_of="$(python - <<'PY'
from datetime import UTC, datetime
print(datetime.now(UTC).isoformat())
PY
)"

python track_prices.py \
  --store data/history/price_history.sqlite3 \
  --target-master "$weekly_run/inputs/target_master_validated.csv" \
  --source-model-map "$weekly_run/inputs/source_model_map_validated.csv" \
  --match-review "$weekly_run/inputs/match_review_validated.csv" \
  --selected-list "$weekly_run/verified_top_100/top_100_verified_configurations.csv" \
  --contributions "$weekly_run/verified_top_100/verified_configuration_contributions.csv" \
  --sales-ranking "$weekly_run/inputs/eligible_model_ranking.csv" \
  --week-start "$week_start" \
  --as-of "$as_of" \
  --output-dir "$weekly_run/reports" \
  | tee "$weekly_run/price_report.log"
```

**Does:** uses cumulative local history and this run's catalog/list to create six price/history/ledger CSVs plus a summary. No retailer or Google Sheets request occurs.

No `--observations` argument is needed: Step 6 imported the collection already. Reports cover the verified list (up to 100); uncollected configurations may have blank prices. Publication filters to your seven-item scope.

**Current policy:** valid displayed PHP prices from exactly matched IN_STOCK **and OUT_OF_STOCK** offers are eligible. A selected sold-out price is labeled OUT OF STOCK. Since 2026-10-19 purchase terms decide bundles: an ordinary offer with stated inclusions (Office, warranty, backpack) is comparable and labeled `INCLUDED_ITEMS_OFFER`; a paid package, a price requiring another purchase, a restricted (financing, card, trade-in, member) price, or bundle wording without evidence of the terms is excluded with that reason. Add `--offer-terms-policy bundle-flag/2026-10-15` to reproduce the earlier rule; `price_tracking_run_summary.json` → `offer_terms_effect` lists every exact match whose eligibility differs between the two rules (identity held fixed). Unknown bundle status, preorder/unknown availability, unknown price type and unresolved matches stay excluded. No accessory value is deducted; no missing price becomes zero; vouchers/shipping are not inferred.

Add `--search-context "$weekly_run/discovery/collection/match_results.csv"` to the command above so a blank price can name the offer found for that configuration that is not a confirmed match (`<code>_Candidate_URL`, `_Candidate_Status`, `_Candidate_Detail`, cell status `CANDIDATE_NOT_CONFIRMED`). Its price is never shown or used in a minimum.

Check week, cutoff, sales label, selections, lowest prices and review. The **lowest observed** price (`Lowest_Price`) can be sold out; the **lowest in-stock** price (`Lowest_In_Stock_Price`, 2026-10-15) uses only in-stock eligible offers and is blank when there is none. Each retailer cell now has its stock, observation time, match basis, warnings and a reason for any blank. Review contains historical observations too, so its count is not the number of fresh failures.

Then explain coverage per configuration and retailer (offline; matches the saved collection again with the current rules and imports nothing):

```bash
python coverage_report.py \
  --observations "$weekly_run/discovery/collection/observations.csv" \
  --match-results "$weekly_run/discovery/collection/match_results.csv" \
  --target-master "$weekly_run/inputs/target_master_validated.csv" \
  --scope "$weekly_run/pilot_scope.csv" \
  --discovery-dir "$weekly_run/discovery" \
  --collection-dir "$weekly_run/discovery/collection" \
  --manifest "$weekly_run/discovery/manifest.csv" \
  --history "$weekly_run/reports/price_history.csv" \
  --output-dir "$weekly_run/coverage"
```

`coverage.csv` gives one status per pair: `PRICE_AVAILABLE` (comparable offer), `EXACT_PRICE_EXCLUDED`, `AMBIGUOUS_TARGET`, `IDENTITY_CONFLICT`, `MODEL_UNVERIFIED`, `EXTRACTION_INCOMPLETE` (specifications missing or extraction failed), `SAME_MODEL_DIFFERENT_CONFIGURATION`, `DIFFERENT_CONFIGURATION`, `FETCH_FAILED` (candidate found, fetching failed), `CANDIDATE_NOT_FETCHED`, `NO_CANDIDATE_FOUND`, `SEARCH_FAILED`, `DISCOVERY_NOT_ATTEMPTED` (including searches never sent because of a limit), or `UNKNOWN_COVERAGE` (no search record). Rows for configurations outside the scope appear only when an offer was confirmed for them (`CONFIRMED_OUTSIDE_SCOPE`). Each row has a `current_reason` sentence, `candidate_url`, `differing_fields`, `confirmed_price` and a separate `unconfirmed_candidate_price`, and the discovery `search_stop_reason`. Repeat `--observations`/`--match-results` for earlier collections: the latest observation of each offer decides, older ones stay as history. `coverage_offers.csv` keeps every offer behind it with its relation (`MATCHED` vs search context). "Not found" never means out of stock.

### Step 8 - Preview the tracker update

```bash
python publish_prices.py --preview \
  --report-dir "$weekly_run/reports" \
  --pilot-scope "$weekly_run/pilot_scope.csv" \
  --collection-dir "$weekly_run/discovery/collection" \
  --selected-list "$weekly_run/verified_top_100/top_100_verified_configurations.csv" \
  --output-root "$weekly_run/publications"
```

**Does:** validates reports and reads the four pilot destination tabs to check existing content/headers. No Sheet changes occur. Check counts, week, cutoff and sales-period label.

### Step 9 - Publish the reviewed reports

```bash
python publish_prices.py --write-sheets \
  --report-dir "$weekly_run/reports" \
  --pilot-scope "$weekly_run/pilot_scope.csv" \
  --collection-dir "$weekly_run/discovery/collection" \
  --selected-list "$weekly_run/verified_top_100/top_100_verified_configurations.csv" \
  --output-root "$weekly_run/publications"
```

**Does:** backs up existing pilot tabs locally, writes the three data tabs, records the publication, then verifies values, links and PHP formats by readback.

Finish with `Publication SUCCESS` and zero readback issues. Keep the run folder. This publisher does not update `raw`, catalog/mappings, or either Top 100 tab. Its backup is per-pilot-tab JSON, not a full workbook copy.

## 5. Which commands actually change things?

| Command | Reads live Sheets? | Requests retailers? | Local outputs | Writes Sheets? |
|---|---|---|---|---|
| `run_phase1.py --dry-run` | `raw` | No | Sales CSVs/diagnostics | No |
| `check_target_tables.py --from-sheets` | Three catalog/review tabs | No | Validated CSVs/diagnostics | No |
| `build_verified_top_100.py` with local inputs | No | No | Rankings, ledger, review priorities | No |
| `audit_verified_top_100.py` with local inputs | No | No | Console audit result | No |
| `discover_products.py --preview` | No | No | Query plan | No |
| `discover_products.py --live --collect` | No | Yes | Evidence, observations, matches, store backup | No |
| `track_prices.py` | No | No | Reports and pending versioned matches in the store | No |
| `publish_prices.py --preview` | Four pilot tabs | No | Publication plan | No |
| `publish_prices.py --write-sheets` | Four pilot tabs | No | Plan, backups, publication report | **Four pilot tabs only** |
| `run_phase1.py --write-sheets` (optional) | `raw` and P1 decision/log inputs | No | New sales outputs | **Legacy P1 outputs** |

## 6. Folder structure

```text
data/
  history/
    price_history.sqlite3              Persistent across weeks
  output/
    weekly_runs/
      <UTC run timestamp>/
        before_refresh/                Previous default CSVs, preserved before Step 1
        inputs/                        This run's sales/catalog snapshots
        verified_top_100/              Rankings, contributions and review priorities
        pilot_scope.csv                Configurations searched/published
        discovery_preview/             Request plan
        discovery/
          evidence/                    Search/inspection responses
          manifest.csv
          candidates.csv
          variants.csv
          discovery_summary.json
          store_backup_before_collection.sqlite3
          collection/                  Fresh observations, matches and evidence
        reports/                       Prices, selections, series, history and review
        publications/                  Plans, backups and readback results
        *.log                          Console logs
```

Default sales files in `data/output/` are replaced on refresh. Dated folders preserve the exact files used for each run. Refreshing a CSV means exporting/building again; it is not automatic synchronization.

## 7. Optional actions and current boundaries

### Updating the older P1 sales-output tabs

If you also want these tabs refreshed, use this **instead of** Step 1's dry-run command:

```bash
python run_phase1.py --write-sheets
```

It reads `raw`, creates a new sales run and replaces the permitted generated P1 tabs; it also preserves/merges eligibility decisions and appends the P1 run log. Follow the existing P1 writer full-spreadsheet-backup procedure before that write. Then freeze the resulting files and continue Steps 2-9. Running it later as an unrelated extra refresh would create a different sales run from the current weekly files.

It does **not** publish `P1_TOP_100_CONFIGURATIONS` or retailer-price tabs. See [setup and operations](SETUP_AND_OPERATIONS.md) and [the decision log](DECISIONS.md) for writer details.

### Publishing the verified Top 100 display

`build_verified_top_100.py` produces CSVs locally. There is no repository CLI that writes `P1_TOP_100_CONFIGURATIONS`. Updating that display is a separate Sheet task using the built CSV and existing layout. The retailer pipeline reads the CSV you pass, not the display tab.

### Known URLs and human review

- If search misses a known page, recollect its reviewed URL through `collect_and_match.py` with fresh inputs and a new output directory. Expected IDs must belong to the current verified list. Review a supplemental collection's own reports too.
- Incomplete catalog candidates need evidence, reviewed promotion and mapping. Weekly commands do not manufacture specifications or create target IDs.
- Good search results can still need model/specification review. PC Express model evidence and some new DataBlitz/Octagon/PCC layouts remain gaps from the October 13 pilot.
- Shopify predictive search returns at most 10 results without pagination; broad queries can miss products. Empty results do not prove a store never sells a model.
- No scheduled job is installed. Refresh, search, reporting and publication happen when the commands run.

## 8. When something stops

| Situation | Next action |
|---|---|
| Raw month is stale | Check the upstream weekly import and source periods. New CSVs can still contain old sales. |
| Duplicate/identity conflict | Inspect `conflicting_duplicates.csv` / `duplicate_review.csv`, resolve recorded decisions, then refresh sales again. |
| Catalog validation fails | Read the table's validation CSV, correct the live source through review and export again. |
| Catalog fingerprint or sales-run mismatch | Rebuild from this run's exact files. Do not mix old/new folders. |
| Fewer than 100 verified configurations | Review shortfall, exclusions and priorities. A raw update does not automatically resolve catalog coverage. |
| Search limit or retailer failure | Inspect search outcomes and completed work; use a scoped retry, preserving the first run. |
| No collection folder | There was no usable collected manifest. Review discovery status/candidates before reporting. |
| `MISSING_MODEL` / ambiguous field | Review evidence. A promising title does not authorize a match. |
| Collection matches a target outside the pilot scope | The publisher excludes that configuration from pilot comparison/history and prints the count. A related scoped search mismatch remains in review. Keep the original collection evidence and pilot scope; no recollection is needed. |
| Partial/failed publication | Read `publication_report.json`, address its issue and rerun the publisher on the same reviewed reports. |
| Restarted terminal | Restore `weekly_run` / `WEEKLY_RUN`; recreate `pilot_args` before using it. |

## 9. Weekly checklist

- [ ] Upstream raw import is complete and sales periods are correct.
- [ ] Sales ranking and all three catalog snapshots were refreshed into this run.
- [ ] Verified-list build/audit passed; count and shortfall were reviewed.
- [ ] Pilot scope belongs to the current verified list.
- [ ] Search/collection outcomes were reviewed, including limits and unresolved items.
- [ ] Prices, links, stock labels, cutoff and sales dates were reviewed.
- [ ] Publication preview passed.
- [ ] Publication succeeded with zero readback issues.
- [ ] Dated run folder and cumulative history store were retained.

Further details: [setup and operations](SETUP_AND_OPERATIONS.md), [catalog schema](TARGET_MASTER_SCHEMA.md), [matching](MATCHING.md), [price output schema](PRICE_TRACKING_OUTPUT_SCHEMA.md), and [pilot status](PHASE_10_PILOT.md).
