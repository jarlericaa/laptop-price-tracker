# laptop-price-tracker

This project processes Shopee Electronics data from Google Sheets and produces a ranked list
of laptop models for future retailer price tracking. The current Python pipeline reads the
`raw` worksheet, validates and classifies data, detects M-1 and MTD, and generates local ranking
and review files. A shared, retailer-neutral scraping framework (overall Phase 6) and two
offline-tested retailer adapters, Laptop Factory and DataBlitz (Phase 7), are implemented. Live
use is enabled under the project-owner policy; retailer permission remains unverified, and no
live scraper request has been made. Exact configuration matching, price comparison, and GitHub
Actions automation are planned; they are not implemented.

The latest read-only live dry run completed in 57.49 seconds and generated 100 targets. The
21 historical conflicting keys had fingerprint-matched `EXCLUDE_KEY` decisions; their source
rows remain excluded, with no disputed values selected or summed. The user accepted the current
Top 100 and eligibility CSV reports on 2026-09-29. The user reports that the nine approved `P1_*`
production tabs were created with headers only and verified on 2026-09-29. A synthetic-data
writer test completed on a dedicated test spreadsheet; a read-only report confirmed the nine
tabs and headers, expected synthetic rows, and a run-log count of 1. The full
`run_phase1.py --write-sheets` command has not been run, and no production sheet write has
occurred. The user later reported the three Phase 5 tabs have header rows and no data. The local
`TARGET_MASTER` and `MATCH_REVIEW` headers have since changed to the revised identity schema and
need to be copied to the Sheet.

Phase 5 is PARTIALLY_COMPLETE. The user revised the `TARGET_MASTER` identity key on 2026-09-29
to match the requested 1:1 product fields. Local validators exist for `TARGET_MASTER`,
`SOURCE_MODEL_MAP`, and `MATCH_REVIEW`, along with a
read-only Google Sheets reader for those tabs. A command seeds the Top 100 into a local
`MATCH_REVIEW` candidate file. No real configurations or mappings have been entered, no approver
has been named. The current local templates need to be reflected in the master Sheet before the
read-only validator will accept its `TARGET_MASTER` and `MATCH_REVIEW` headers. See
[docs/TARGET_MASTER_SCHEMA.md](docs/TARGET_MASTER_SCHEMA.md).

Phase 6 is COMPLETE as a framework. `src/laptop_tracker/offers.py` and
`src/laptop_tracker/scraping.py` define the adapter contract and the normalized offer
observation (`Decimal` price, ISO currency, UTC retrieval time, no `target_id`). They also
provide timeouts, bounded retries, and per-retailer throttling. An offline example adapter
under `tests/` exercises them with synthetic fixtures. See
[docs/SCRAPING_FRAMEWORK.md](docs/SCRAPING_FRAMEWORK.md).

Phase 7 is PARTIALLY_COMPLETE. `src/laptop_tracker/adapters/laptop_factory.py` parses fetched
Laptop Factory product pages offline, tested against minimal local fixtures. Project-owner
policy enables its live-use flag; external retailer permission remains unverified. No project
scraper request has been made to the retailer. See
[docs/adapters/LAPTOP_FACTORY.md](docs/adapters/LAPTOP_FACTORY.md).
`src/laptop_tracker/adapters/datablitz.py` (DBZ, added 2026-10-01) does the same for DataBlitz
product pages on `ecommerce.datablitz.com.ph`, under the same owner policy. No project scraper
request has been made to DataBlitz. See [docs/adapters/DATABLITZ.md](docs/adapters/DATABLITZ.md).

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

## Local verification

```bash
python -m pytest -q
python -m ruff check .
python -m compileall src run_phase1.py smoke_test.py check_target_master.py check_target_tables.py seed_match_review.py
python smoke_test.py
python run_phase1.py --dry-run
```

The final two commands need the live configuration above. Writing Sheets is never part of the
verification sequence; run `python run_phase1.py --write-sheets` only after reviewing the local
outputs.
