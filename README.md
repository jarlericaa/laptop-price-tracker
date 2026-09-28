# laptop-price-tracker

This project processes Shopee Electronics data from Google Sheets and produces a ranked list
of laptop models for future retailer price tracking. The current Python pipeline reads the
`raw` worksheet, validates and classifies data, detects M-1 and MTD, and generates local ranking
and review files. Retailer scraping, exact configuration matching, price comparison, and
GitHub Actions automation are planned; they are not implemented.

The current code and tests are present, but the latest live dry run stopped safely after
ranking because 21 duplicate or model-identity records have no human decision. A synthetic
12,000-model aggregation test passes its under-10-second check; a complete live run has not yet
finished successfully. The current `TARGET_MASTER` worksheet is empty.

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

## Conflicting duplicates

Exact duplicate rows for a `period_month`/`shop_id`/`item_id`/`model_id` key are kept once.
Rows sharing a key but differing in any field are quarantined, reported in
`conflicting_duplicates.csv`, and block publication until a human records a decision. Each
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
The latest live dry run took about 90 seconds and stopped before any Google Sheets write.

## Documentation

- [Product requirements](docs/PRD.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Implementation phases](docs/IMPLEMENTATION_PHASES.md)
- [Progress log](docs/PROGRESS_LOG.md)
- [Decision log](docs/DECISIONS.md)
- [Setup and operations](docs/SETUP_AND_OPERATIONS.md)

## Local verification

```bash
python -m pytest -q
python -m ruff check .
python -m compileall src run_phase1.py smoke_test.py
python smoke_test.py
python run_phase1.py --dry-run
```

The final two commands need the live configuration above. Writing Sheets is never part of the
verification sequence; run `python run_phase1.py --write-sheets` only after reviewing the local
outputs.
