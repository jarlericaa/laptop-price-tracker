# Architecture

## System overview

The current system is a local Python pipeline that reads Shopee data from Google Sheets, processes it in memory, writes local diagnostics and ranking files, and optionally writes a guarded set of output worksheets. The `raw` worksheet is always a read-only input.

```mermaid
flowchart LR
    S[Google Sheets: raw] -->|read-only scope| R[sheets_source]
    R --> N[schema normalization]
    N --> V[data validation]
    V --> D[duplicate decisions and quarantine]
    D --> P[period and freshness checks]
    P --> E[eligibility classification]
    E --> A[model aggregation and ranking]
    A --> L[local CSV and JSON outputs]
    A -. explicit --write-sheets only .-> W[guarded P1_* writer]
    W --> O[approved output tabs]
    T[TARGET_MASTER] -. manual local CSV export .-> C[check_target_master.py]
    C --> TM[target_master validator]
    TM --> TL[local error report and validated copy]
    TM -. planned; schema pending approval .-> X[future retailer adapters, exact matching, comparison]
```

The current and planned portions are different deliverables. Retailer scraping, exact configuration matching, price history, and GitHub Actions are not implemented.

## Repository structure

```text
.
├── config/
│   ├── duplicate_decisions.csv       # Human decisions for duplicate conflicts
│   ├── eligibility_decisions.csv     # Model-level ELIGIBLE/EXCLUDED overrides
│   ├── eligibility_rules.csv         # Keyword inclusion and exclusion rules
│   └── target_master_template.csv    # Header-only proposed TARGET_MASTER layout
├── data/
│   ├── output/                       # Ignored local reports and target files
│   └── review/                       # Reserved local review area
├── docs/                             # Project and operations documentation
├── src/laptop_tracker/
│   ├── config.py
│   ├── data_quality.py
│   ├── eligibility.py
│   ├── periods.py
│   ├── pipeline.py
│   ├── ranking.py
│   ├── schema.py
│   ├── sheets_source.py
│   ├── sheets_writer.py
│   └── target_master.py
├── tests/
├── check_target_master.py            # Local TARGET_MASTER CSV check (overall Phase 5)
├── run_phase1.py                     # Legacy Phase 1 command name; overall Phase 4 work
├── smoke_test.py
├── pyproject.toml
└── requirements.txt
```

`data/output/`, credentials, and local decision CSVs are ignored by Git. The project setup, implementation, tests, and initial docs are recorded in four purpose-based commits; the Phase 5 validator and this status refresh are recorded in follow-up feature and documentation commits. Generated reports and local decisions remain ignored; see [the progress log](PROGRESS_LOG.md).

## Current components

| Module or file | Responsibility |
|---|---|
| `config.py` | Loads settings from environment and `.env`; validates required Sheet and credential settings without printing secrets. |
| `schema.py` | Defines the required raw headers, checks header uniqueness, preserves cells as strings, and maps source names to internal names. |
| `sheets_source.py` | Loads service-account credentials, opens the configured spreadsheet, and reads the configured raw worksheet. Uses a read-only OAuth scope by default. |
| `data_quality.py` | Validates row values, collapses exact duplicates, fingerprints duplicate conflicts, applies recorded human decisions, and detects model IDs mapped to multiple shop/item identities. |
| `periods.py` | Detects latest month and M-1, enforces the Manila-timezone freshness check, and builds period summaries. |
| `eligibility.py` | Loads keyword rules and local model-level decisions, then classifies listings. |
| `ranking.py` | Aggregates model GMV, selects a single distinct MTD price or flags a conflict, ranks eligible active models, and prepares review/excluded lists. |
| `pipeline.py` | Orchestrates read-independent processing, diagnostics, local files, conflict blocking, and the optional sheet-write path. |
| `sheets_writer.py` | Enforces the `P1_*` tab allowlist, prevents `raw` from being a destination, preserves the remote decision tab, replaces approved outputs, and appends run logs. |
| `run_phase1.py` | CLI entry point. Local output is the default; `--write-sheets` opts into the writer. |
| `smoke_test.py` | Read-only check for spreadsheet access, raw headers, and string-valued sample cells. |
| `target_master.py` | Holds the proposed `TARGET_MASTER` column definitions and identity key. Validates worksheet-shaped values (header row first) for headers, required fields, formats, dates, and duplicate `target_id` or configuration identity, keeping exact strings. Uses no Google access. |
| `check_target_master.py` | Local CLI that validates a CSV export. Writes a local error report and, only on a full pass, a validated copy. Prints counts only. |

The `TARGET_MASTER` validator runs only on local CSV exports; no module reads the worksheet's configuration rows. The schema is a proposal pending business approval; see [TARGET_MASTER_SCHEMA.md](TARGET_MASTER_SCHEMA.md). No retailer-adapter, exact-matching, comparison-history, or workflow module exists.

## Data-processing flow

1. `read_raw_values()` uses the configured spreadsheet ID and raw tab name, then calls `get_all_values()`; it does not call any write API.
2. `normalize_raw_values()` checks the required headers, pads short rows, converts cells to strings, assigns source row numbers, and creates the internal model/shop/item keys.
3. `validate_rows()` rejects rows with invalid dates, IDs, required names, prices, or GMV. Invalid rows are reported in `validation_errors.csv` and do not enter aggregation.
4. `collapse_duplicates()` groups by `(period_month, shop_id, item_id, model_id)`. Identical rows retain one copy. Conflicts require a fingerprint-matched decision from `config/duplicate_decisions.csv`; the redacted review template is local-only.
5. The pipeline detects the latest valid month and its preceding calendar month, then checks that the latest month is current in Manila time unless freshness checking is disabled.
6. Model identity conflicts are excluded. The remaining rows are classified using `eligibility_rules.csv` and `eligibility_decisions.csv`.
7. `aggregate_models()` calculates M-1 and MTD GMV by model, sums them for ranking GMV, selects one MTD price only when exactly one distinct value exists, and marks eligibility disagreements as `REVIEW`.
8. `rank_eligible_models()` retains `ELIGIBLE` models present in MTD and orders them deterministically. `top_100()` selects at most 100.
9. The pipeline saves local files and a run summary. In the CLI path, any unresolved duplicate or model-identity conflict then blocks a successful run before optional Google Sheets writes.

## Google Sheets integration and authentication

`Settings.from_env()` reads `.env` without overriding environment variables. It accepts either a service-account JSON file path (`GOOGLE_SERVICE_ACCOUNT_FILE`) or JSON text in an environment variable (`GOOGLE_SERVICE_ACCOUNT_JSON`); use the external file path for the documented local setup.

The `raw` read uses `https://www.googleapis.com/auth/spreadsheets.readonly`. Only the explicit writer path reconnects with `https://www.googleapis.com/auth/spreadsheets`. The service account also needs to be shared on the spreadsheet with the appropriate spreadsheet-level permission. Credentials remain outside the repository; `.env`, JSON keys, and common key formats are ignored by `.gitignore`.

The writer accepts only these tabs: `P1_TOP_100_TARGETS`, `P1_ELIGIBLE_RANKING`, `P1_EXCLUDED_LISTINGS`, `P1_ELIGIBILITY_REVIEW`, `P1_ELIGIBILITY_DECISIONS`, `P1_CONFLICTING_DUPLICATES`, `P1_VALIDATION_ERRORS`, `P1_PERIOD_SUMMARY`, and `P1_RUN_LOG`. It refuses the `raw` tab even with case or whitespace variations. There is no arbitrary test-tab writer.

Current read-only checks on 2026-09-28 found `TARGET_MASTER` but no rows or header, and found no existing `P1_*` output tabs. No output tab was written during documentation work.

On 2026-09-29, the user reported that all nine approved `P1_*` tabs had since been created in the intended spreadsheet with verified headers and blank second rows. The Python writer integration remains untested; the production tabs are not to be used for its first test.

## Local execution and outputs

From the repository root in WSL, `python smoke_test.py` checks connectivity and raw headers. `python run_phase1.py --dry-run` reads the source and saves local CSV/JSON files under `data/output/`; running without a write flag has the same local-only write behavior. The reports can contain business data and are ignored by Git.

`duplicate_review.csv` is a redacted local decision template. The other output reports can include model and shop identifiers, names, prices, and GMV, so they should be handled as private business data.

## Error and failure boundaries

- Missing settings, inaccessible spreadsheet, missing raw worksheet, or invalid headers stop the run with an actionable error.
- Invalid rows are excluded and recorded. More than `MAX_VALIDATION_ERRORS` stops processing after the validation report is saved.
- An empty valid period set, stale latest month, invalid decision schema, stale duplicate fingerprint, or unresolved duplicate/model-identity record prevents publication.
- Source-read failures never fall back to a write-capable source scope.
- The write path is only reached after the local pipeline returns successfully and the caller set `--write-sheets`.
- `raw` is outside the writer allowlist and has a separate explicit safety guard.

## Planned GitHub Actions flow

There is no workflow in the repository today. Once the controlled pilot passes, a future scheduled workflow can install the pinned project dependencies, obtain credentials through an approved secret mechanism, run validation and generation, publish only approved outputs under explicit configuration, and retain logs or alert an operator on failure. Schedule, retention, secret rotation, approvals, and notification ownership remain to be decided during Phases 10-12.

## Planned retailer adapter boundary

Retailer adapters are future components. The intended boundary is:

1. Read only validated laptop configurations from `TARGET_MASTER`, meaning rows that pass `validate_target_master` under the approved schema.
2. Query or browse retailer-specific sources without changing the source sheet.
3. Return normalized offer observations with retailer, URL, retrieval time, raw configuration text, price, and availability evidence.
4. Pass normalized observations to a shared exact-configuration matcher. Ambiguous or partial matches must be review cases, not silent matches.
5. Send confirmed comparisons to the future weekly comparison and price-history outputs.

The current repository contains none of these adapters or contracts; the list describes the planned connection point, not existing behavior.
