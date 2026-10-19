# laptop-price-tracker

**Start here:** [Weekly workflow, dataflow and commands](docs/WEEKLY_WORKFLOW.md) explains the Sheet tabs, CSVs, weekly refresh, automatic search and publishing step by step.

This project processes Shopee Electronics data from Google Sheets and produces a ranked list
of laptop models for future retailer price tracking. The current Python pipeline reads the
`raw` worksheet, validates and classifies data, detects M-1 and MTD, and generates local ranking
and review files. A shared, retailer-neutral scraping framework (overall Phase 6) and five
offline-tested retailer adapters, Laptop Factory, DataBlitz, PC Express, PC Central, and
Octagon (Phase 7), are implemented and enabled for live use under project-owner decisions.
Laptop Factory, DataBlitz, and PC Express each have a user-reported live check; Laptop Factory
and PC Express also have a controlled 2026-10-01 check with plain-string URLs. The
external-policy findings for PC Express and Octagon are RESTRICTED under the retailers' terms.
PC Central's bare-URL check failed safely; a selected-variant live check returned one in-stock
offer. A user-reported OCT live check returned one in-stock HP 15-FC0413AU offer at PHP 37,999.
Exact configuration matching (overall Phase 8) and weekly price comparisons with price history
(overall Phase 9) run offline over saved observations. `collect_and_match.py` adds manual
online collection of explicitly listed product pages into the same store and matcher. Automatic
product discovery and the supervised multi-retailer pilot are implemented;
Phase 10 remains in progress. Price reports can be published to dedicated tracker tabs.
GitHub Actions scheduling is not implemented.

The latest read-only live dry run completed in 57.49 seconds and generated 100 targets. The
21 historical conflicting keys had fingerprint-matched `EXCLUDE_KEY` decisions; their source
rows remain excluded, with no disputed values selected or summed. The user accepted the current
Top 100 and eligibility CSV reports on 2026-09-29. The user reports that the nine approved `P1_*`
production tabs were created with headers only and verified on 2026-09-29. A synthetic-data
writer test completed on a dedicated test spreadsheet; a read-only report confirmed the nine
tabs and headers, expected synthetic rows, and a run-log count of 1. The full
`run_phase1.py --write-sheets` command has not been run on the production P1 outputs. The
separate pilot price publisher has written and verified the dedicated production pilot tabs.

Phase 5 is COMPLETE (2026-10-12). The `TARGET_MASTER` identity is the five-field core key approved
on 2026-10-05 (`model`, `screen_size_in`, `cpu_series` at CPU-family level, `ram_capacity_gb`,
`storage_capacity_gb`), and the local validators for `TARGET_MASTER`, `SOURCE_MODEL_MAP`, and
`MATCH_REVIEW` enforce it. Rows have a tracking mode: `CONFIGURATION` (the default) uses the
five-field identity and exact retailer matching; `LISTING_ONLY` covers products confirmed to have
no verifiable model, is tracked by `shop_id` + `item_id` + `source_model_id`, and is never matched
across retailers. On 2026-10-12 the catalog held 205 targets (193 `CONFIGURATION`, 12
`LISTING_ONLY`), 360 mappings, and 5,154 review rows, all passing the read-only validator. The
tracker's working list is the **Top 100 verified configurations**: 128 configurations qualify,
100 are selected, and the list is published in the `P1_TOP_100_CONFIGURATIONS` tab. It excludes
`LISTING_ONLY` targets and unresolved identities. See
[docs/TARGET_MASTER_SCHEMA.md](docs/TARGET_MASTER_SCHEMA.md).

Phase 8 is COMPLETE as an offline path. Replaying the nine Laptop Factory observations saved on
2026-10-08 against the current catalog gives 9 `EXACT` matches. That is one retailer and nine
offers, not a pilot. See [docs/MATCHING.md](docs/MATCHING.md).

Phase 9 is COMPLETE as an offline path (2026-10-12). `track_prices.py` stores observations in an
append-only local SQLite history and matches them under a recorded catalog and rule version. It
then writes a weekly comparison (Monday to Monday, Asia/Singapore) for the selected working list,
with `DBZ_Price`, `LF_Price`, `PCX_Price`, `PCC_Price`, and `OCT_Price`, plus price history. On the
nine saved 2026-10-08 observations, 3 of the 100 selected configurations get a comparable price.
See [docs/PRICE_TRACKING_OUTPUT_SCHEMA.md](docs/PRICE_TRACKING_OUTPUT_SCHEMA.md).

Phase 6 is COMPLETE as a framework. `src/laptop_tracker/offers.py` and
`src/laptop_tracker/scraping.py` define the adapter contract and the normalized offer
observation (`Decimal` price, ISO currency, UTC retrieval time, no `target_id`). They also
provide timeouts, bounded retries, and per-retailer throttling. An offline example adapter
under `tests/` exercises them with synthetic fixtures. See
[docs/SCRAPING_FRAMEWORK.md](docs/SCRAPING_FRAMEWORK.md).

Phase 7 is COMPLETE. Jarelle Ricaforte maintains all five retailer adapters: LF, DBZ, PCX, PCC, and OCT. Known unobserved page states remain documented limitations, and retailer-policy findings are recorded separately from project-owner authorization.

`src/laptop_tracker/adapters/laptop_factory.py` parses Laptop Factory product pages offline and has a controlled bare-URL live check returning one in-stock Acer Aspire AI 14 offer at a sale price. Retailer permission remains unverified. See [docs/adapters/LAPTOP_FACTORY.md](docs/adapters/LAPTOP_FACTORY.md).

`src/laptop_tracker/adapters/datablitz.py` parses DataBlitz product pages on `ecommerce.datablitz.com.ph`; a user-reported live spot check returned one in-stock MSI offer. See [docs/adapters/DATABLITZ.md](docs/adapters/DATABLITZ.md).

`src/laptop_tracker/adapters/pcx.py` parses PC Express product pages on `pcx.com.ph`. Internal live use is enabled by the project-owner decision; the retailer's external-policy finding remains RESTRICTED. A controlled check confirmed the offer URL is a plain string. See [docs/adapters/PCX.md](docs/adapters/PCX.md).

`src/laptop_tracker/adapters/pcc.py` parses PC Central product pages on `pccentralcomputerstore.com`. Products with several variants need a `?variant=<id>` URL. A selected-variant live check returned one in-stock ASUS VivoBook offer. See [docs/adapters/PCC.md](docs/adapters/PCC.md).

`src/laptop_tracker/adapters/oct.py` parses Octagon product pages on `www.octagon.com.ph`. Its terms prohibit scraping, so the external-policy finding is RESTRICTED; the project-owner decision is not Octagon's consent. A user-reported live check returned one in-stock HP offer. See [docs/adapters/OCT.md](docs/adapters/OCT.md).

The script is named `run_phase1.py` for historical reasons. Its raw-data and Top 100 work maps
mainly to **overall Phase 4** in the revised plan. Google access is overall Phase 1.

## Setup

Create a local `.env` from `.env.example`. Keep the service-account JSON outside this
repository and share the spreadsheet with the service-account email address. Install the
dependencies into `.venv` with `python -m pip install -r requirements.txt`.

The minimum live configuration is:

```dotenv
GOOGLE_SERVICE_ACCOUNT_FILE=/home/<username>/.config/laptop-price-tracker/google-service-account.json
SPREADSHEET_ID=<your spreadsheet ID>
RAW_TAB_NAME=raw
```

Do not place the credential JSON in this repository. Manual eligibility overrides belong in
`config/eligibility_decisions.csv`; existing decisions are merged rather than discarded when
the optional `P1_ELIGIBILITY_DECISIONS` tab is updated.

## Safe commands

```bash
python smoke_test.py
python run_phase1.py --dry-run
```

Both commands only read the `raw` tab. `python run_phase1.py --write-sheets` additionally
updates the explicitly allowed `P1_*` output tabs; it can never write to `raw`.
The command defaults to local-output-only behavior even when `--dry-run` is omitted. Source
reads use a read-only Google Sheets OAuth scope; the write scope is requested only after the
explicit `--write-sheets` option is supplied.

```bash
python check_target_master.py path/to/target_master_export.csv
```

This validates a local `TARGET_MASTER` CSV snapshot (header template:
`config/target_master_template.csv`) without Google access.

```bash
python check_target_tables.py --target-master tm.csv --source-model-map map.csv --match-review review.csv
python check_target_tables.py --from-sheets
python seed_match_review.py
```

`check_target_tables.py` validates local snapshots of any of the three Phase 5 tables. Mapping
checks require the master snapshot, so target references can be verified. The header templates
`config/source_model_map_template.csv` and `config/match_review_template.csv` are exact
contracts. With `--from-sheets`, the command reads the three tabs with the read-only scope. It
never writes or creates tabs. `seed_match_review.py` creates
`data/review/match_review_candidates.csv`, with one `NEW` candidate per distinct Top 100 model
ID. All specifications, URLs, and approvals are left blank for human research. It never creates
`TARGET_MASTER` or mapping rows. All three commands write reports only under ignored `data/`
paths and print counts only.

```bash
python match_offers.py --target-master tm.csv --observations offer_observations.csv
python match_offers.py --target-master-from-sheets --observations offer_observations.csv
python audit_top_100_coverage.py --from-sheets
python propose_catalog_conventions.py --target-master data/output/target_master_validated.csv
python build_verified_top_100.py            # add --from-sheets to read the three catalog tabs
python audit_verified_top_100.py
```

`audit_top_100_coverage.py` classifies each accepted Phase 4 Top 100 `model_id` (exact text) as
mapped to an ACTIVE target, mapped to an inactive or invalid target, unmapped but in
`MATCH_REVIEW` (by status), or missing. It also lists `COMPLETE` review rows without an ACTIVE
mapping. It accepts the same three local snapshots as `check_target_tables.py` (exports or
validated copies) or reads the tabs read-only, writes local reports under `data/output/`, and
prints counts only. It never reads `raw` or `P1_TOP_100_TARGETS` and never writes to Sheets.

`match_offers.py` (Phase 8) matches saved retailer offer observations to a fully valid
`TARGET_MASTER` using the five-field exact rule. `--target-master` accepts a sheet export or a
`check_target_tables.py` validated copy; `--target-master-from-sheets` reads only the live
`TARGET_MASTER` tab with the read-only scope instead. It writes
`offer_match_results.csv`, `offer_match_review_queue.csv`, and `offer_match_evidence.csv` under
`data/output/` and prints counts only. See [docs/MATCHING.md](docs/MATCHING.md).

`propose_catalog_conventions.py` proposes `model`/`cpu_series` corrections under the 2026-10-08
conventions for each `target_id` in a validated snapshot. Optional mapping and review snapshots,
Top 100 coverage, and saved observations add evidence. It writes local reports under
`data/output/catalog_conventions/`, including a sheet correction report, and never edits the
catalog. See [docs/SHEET_MIGRATION_MODEL_CPU_CONVENTIONS.md](docs/SHEET_MIGRATION_MODEL_CPU_CONVENTIONS.md).

`build_verified_top_100.py` produces the **Top 100 verified configurations** (owner decision
2026-10-12). This is the tracker's working list, not the overall sales Top 100:

- **Members.** It contains unique ACTIVE `CONFIGURATION` targets with complete, unconflicted
  five-field identities.
- **Ranking.** Targets are ranked by the combined M-1 + MTD GMV of their eligible, approved
  mapped variations. Each target is counted once, and GMV is summed exactly from the Phase 4 text.
- **Inputs.** It reads the full Phase 4 eligible ranking (`eligible_model_ranking.csv`), checks
  it against the accepted source Top 100 (kept unchanged as a reference), and reads the three
  validated catalog tables. With `--from-sheets`, the catalog tabs are read with the read-only
  scope.
- **Outputs.** It writes the full ranking, the Top 100, a contribution ledger, exclusions, a
  review-priority queue, and a run summary under `data/output/verified_top_100/`.
- **Sales periods.** The GMV comes from the historical sales snapshot that Phase 4 extracted. A
  new build date does not make the sales current.

```bash
python track_prices.py --observations data/output/pilot_matching_2026-10-08/observations.csv \
  --week-start 2026-10-05 --as-of 2026-10-12T09:00:00+08:00
```

`track_prices.py` (Phase 9, offline) does four things:

- **Validates** the three catalog snapshots, the published selected list, and its contribution
  ledger.
- **Imports** observation CSVs into `data/history/price_history.sqlite3`. Exact repeats are
  skipped, and a contradicting record refuses the file.
- **Matches** stored observations under the current catalog and rule version.
- **Writes** the weekly comparison, retailer selections (URL, retrieval time, and reason), a
  weekly series, per-observation history, a review queue, a configuration-to-source ledger, and a
  summary under `data/output/price_tracking/week_<date>/`.

Dates are explicit, and the output prints counts only. It makes no retailer requests and never
touches Sheets. See [docs/SETUP_AND_OPERATIONS.md](docs/SETUP_AND_OPERATIONS.md#offline-price-comparisons-and-history-overall-phase-9).

```bash
python collect_and_match.py --preview --manifest config/collection_manifest.csv
python collect_and_match.py --live --manifest config/collection_manifest.csv
```

`collect_and_match.py` is **manual online collection and matching** of explicitly listed product
pages. The manifest (`request_id,retailer_id,product_url,expected_target_id,enabled`; synthetic
template in `config/collection_manifest_template.csv`) is validated as a whole, together with the
catalog and the selected list, before any request. `--preview` sends nothing. `--live` fetches
each enabled page once through the existing adapters and shared `Fetcher`, stores valid
observations in the Phase 9 history store, matches them with the five-field `ExactMatcher`, and
writes a new run directory under `data/output/collection_runs/`: request report, failures, an
observation CSV usable by `match_offers.py` and `track_prices.py`, match results, a review queue,
field evidence, and a run summary. `expected_target_id` is only a hint; a different outcome is
flagged for review. Exit codes: 0 success or preview, 2 partial success, 1 failure. One Laptop
Factory product page was checked live on 2026-10-12. The broader multi-retailer, multi-week pilot
and scheduled automation are separate work. See
[docs/SETUP_AND_OPERATIONS.md](docs/SETUP_AND_OPERATIONS.md#manual-online-collection-and-matching).

```bash
python publish_prices.py --preview --report-dir <reviewed report folder> --pilot-scope <scope.csv>
python publish_prices.py --write-sheets --report-dir <reviewed report folder> --pilot-scope <scope.csv>
```

`publish_prices.py` publishes **reviewed** pilot reports to four dedicated tabs of the configured
spreadsheet: `PILOT_PRICE_COMPARISON` (prices with clickable retailer links), `PILOT_PRICE_HISTORY`,
`PILOT_PRICE_REVIEW`, and `PILOT_PRICE_RUN_LOG`. It validates every input before any Sheets
access. Preview is read-only. A write backs up existing pilot tabs locally, replaces their content
(so repeats never duplicate rows), and reports `SUCCESS` only after a full cell readback. It never
collects, rematches, or touches the history store, `raw`, the catalog, or other tabs. The first
seven-configuration pilot cycle was published on 2026-10-12. See
[docs/SETUP_AND_OPERATIONS.md](docs/SETUP_AND_OPERATIONS.md#publishing-pilot-results-to-google-sheets-phase-10-manual)
and [docs/PHASE_10_PILOT.md](docs/PHASE_10_PILOT.md).

`discover_products.py` searches the five retailers for verified configurations within explicit request limits (`--preview` sends nothing), ranks the results with reasons, lists variants only from each page's own structured data, writes a `collect_and_match.py` manifest, and with `--collect` collects and matches those pages. Ranking never assigns a target. See [docs/SETUP_AND_OPERATIONS.md](docs/SETUP_AND_OPERATIONS.md#automatic-product-discovery-phase-10-manual-runs).

`replay_extraction.py` replays a saved collection offline through the current extractor (from the
observation CSV) and the current adapters (from saved HTML), and writes a before/after comparison;
re-parsed records are never imported. `discover_candidates.py` ranks saved observations as
research candidates for reference configurations and plans search queries, without making
requests or changing matching. See
[docs/SETUP_AND_OPERATIONS.md](docs/SETUP_AND_OPERATIONS.md#offline-extraction-replay-and-candidate-discovery-phase-10).

`audit_verified_top_100.py` independently checks a run: unique targets, ACTIVE `CONFIGURATION`
status, complete identities, valid contributors, totals, deterministic order, and the catalog
version used. See [docs/TARGET_MASTER_SCHEMA.md](docs/TARGET_MASTER_SCHEMA.md#top-100-verified-configurations-2026-10-12).

## Conflicting duplicates

Exact duplicate rows for a `period_month`/`shop_id`/`item_id`/`model_id` key are kept once.
Rows sharing a key but differing in any field are quarantined and reported in
`conflicting_duplicates.csv`; publication is blocked until a valid decision is recorded. Each
run writes a local, redacted `data/output/duplicate_review.csv` (keys, source row numbers,
differing field names, and a `conflict_fingerprint`; no GMV, prices, or names). To record a
decision, copy the row into `config/duplicate_decisions.csv` and fill in `decision`,
`reviewer`, `reviewed_at`, and `reason`:

- `EXCLUDE_KEY` keeps the key out of aggregation.
- `KEEP_SOURCE_ROW` retains only the row named in `keep_source_row_number`.

A decision applies only while its fingerprint matches the current source rows; if the `raw`
data changes, the key is quarantined again for re-review. Model identity conflicts cannot be
overridden this way.

Generated local CSV and JSON outputs live under `data/output/` and are intentionally ignored
by Git. If intentionally processing an older snapshot, set `SKIP_FRESHNESS_CHECK=true`.

Do not run `--write-sheets` against the production spreadsheet during setup or verification.
The latest live dry run took 57.49 seconds and did not write to any Google Sheets output tab.

## Documentation

- [Product requirements](docs/PRD.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Implementation phases](docs/IMPLEMENTATION_PHASES.md)
- [Progress log](docs/PROGRESS_LOG.md)
- [Decision log](docs/DECISIONS.md)
- [Setup and operations](docs/SETUP_AND_OPERATIONS.md)
- [TARGET_MASTER schema (approved)](docs/TARGET_MASTER_SCHEMA.md)
- [Shared scraping framework (Phase 6)](docs/SCRAPING_FRAMEWORK.md)
- [Retailer data-use review](docs/RETAILER_DATA_USE_REVIEW.md)
- [Laptop Factory adapter (Phase 7)](docs/adapters/LAPTOP_FACTORY.md)
- [DataBlitz adapter (Phase 7)](docs/adapters/DATABLITZ.md)
- [PC Express adapter (Phase 7)](docs/adapters/PCX.md)
- [PC Central adapter (Phase 7)](docs/adapters/PCC.md)
- [Octagon adapter (Phase 7)](docs/adapters/OCT.md)
- [Exact offer matching (Phase 8)](docs/MATCHING.md)
- [Sheet migration for tracking modes](docs/SHEET_MIGRATION_TRACKING_MODES.md)
- [Sheet migration for model/CPU conventions](docs/SHEET_MIGRATION_MODEL_CPU_CONVENTIONS.md)
- [Price tracking output schema](docs/PRICE_TRACKING_OUTPUT_SCHEMA.md)
- [Phase 10 pilot runbook](docs/PHASE_10_PILOT.md)

## Local verification

```bash
python -m pytest -q
python -m ruff check .
python -m compileall src run_phase1.py smoke_test.py check_target_master.py check_target_tables.py seed_match_review.py match_offers.py audit_top_100_coverage.py propose_catalog_conventions.py build_verified_top_100.py audit_verified_top_100.py track_prices.py collect_and_match.py publish_prices.py replay_extraction.py discover_candidates.py discover_products.py
python smoke_test.py
python run_phase1.py --dry-run
```

The final two commands need the live configuration above. Writing Sheets is never part of the
verification sequence; run `python run_phase1.py --write-sheets` only after reviewing the local
outputs.

### Current price comparison policy (2026-10-19)

Valid displayed prices are compared for exact configurations whether IN_STOCK or OUT_OF_STOCK.
Selected sold-out prices are labeled OUT OF STOCK; availability remains in history. Purchase terms
decide bundles (`--offer-terms-policy purchase-terms/2026-10-19`, the default): an ordinary laptop
offer with stated inclusions (Office, warranty, backpack) is comparable with an included-items
note; paid packages, prices that require another purchase, restricted (financing, card, trade-in,
member) prices and bundle wording without evidence of the terms are excluded with their reason.
`bundle-flag/2026-10-15` keeps the earlier rule (every checked bundle comparable). Unknown bundle
status, preorder/unknown availability, missing prices and unresolved matches remain excluded.
Details: `docs/PRICE_TRACKING_OUTPUT_SCHEMA.md`.
