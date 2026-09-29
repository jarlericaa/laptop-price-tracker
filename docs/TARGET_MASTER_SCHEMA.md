# `TARGET_MASTER` Schema and Review Workflow

**Status:** The user approved the business choices in this document on 2026-09-29. The `TARGET_MASTER` validator and header template reflect them. Local validators for `SOURCE_MODEL_MAP` and `MATCH_REVIEW`, a read-only Google Sheets reader for all three tabs, and a Top 100 candidate seeding command are implemented; Claude reported 129 passing tests on 2026-09-29. No real configurations or mappings have been entered, and no approver is named. A read-only production check on 2026-09-29 found no `TARGET_MASTER` header and found both supporting tabs missing.

## Purpose and authority

`TARGET_MASTER` contains only complete, reviewed laptop configurations that are candidates for future retailer price comparisons. It does not contain unreviewed Shopee titles or partial specifications. The Google Sheet is the master copy; local CSV exports are validation snapshots.

The approved identity is the exact product specification composed of brand, manufacturer model code, exact CPU including suffix, GPU, RAM, total storage, storage type and layout, display size, resolution and refresh rate, and OS edition. Color and notes are descriptive. A manufacturer part number is useful evidence, but it is never the only identity rule.

## Approved `TARGET_MASTER` columns

The header order matches `config/target_master_template.csv`. Headers are exact and case-sensitive. Every required column must be present; optional columns may be omitted or left blank. Unknown, blank, or repeated headers stop validation.

| Column | Required | Meaning and format | Identity field |
|---|---:|---|---:|
| `target_id` | Yes | Stable ID exactly like `TGT-000001`. Never derive it from specs or reuse it. | No; unique ID |
| `status` | Yes | `ACTIVE` or `INACTIVE` (uppercase). Retire configurations by setting `INACTIVE`; do not delete rows. | No |
| `brand` | Yes | Exact manufacturer brand. | Yes |
| `manufacturer_model_code` | Yes | Exact manufacturer model code. | Yes |
| `series` | No | Product series; descriptive, not identity. | No |
| `cpu` | Yes | Exact CPU model, including suffix. | Yes |
| `gpu` | Yes | Exact GPU. Enter an explicit value for integrated graphics or no discrete GPU; do not leave unknown specs blank. | Yes |
| `ram_gb` | Yes | Positive integer in GB. | Yes |
| `storage_gb` | Yes | Total capacity in decimal GB: `1 TB = 1000 GB`. | Yes |
| `storage_type` | Yes | Verified storage type or combination, such as `NVMe SSD` or `SSD+HDD`. | Yes |
| `storage_layout` | Yes | Per-drive layout, for example `1x512GB SSD + 1x1000GB HDD`. | Yes |
| `display_size_in` | Yes | Positive display diagonal in inches, up to two decimal places. | Yes |
| `display_resolution` | Yes | `WIDTHxHEIGHT` with lowercase `x`, for example `1920x1080`. | Yes |
| `refresh_rate_hz` | Yes | Positive integer in Hz. | Yes |
| `os_edition` | Yes | Exact preinstalled OS edition. Use an explicit value such as `No OS`, rather than leaving it blank. | Yes |
| `color` | No | Descriptive color variant. | No |
| `variant_notes` | No | Descriptive variant details. | No |
| `manufacturer_part_number` | No | Manufacturer part number or SKU; evidence only, not a stand-alone identity rule. | No |
| `source_url` | Yes | Absolute HTTP(S) URL a reviewer can use to verify the configuration. | No |
| `reviewer` | Yes | Name of the manager or category owner who approved the row. No approver has been designated. | No |
| `reviewed_at` | Yes | Actual approval date in `YYYY-MM-DD` format. | No |
| `notes` | No | Descriptive free text. | No |

The uniqueness key is the identity fields marked **Yes** above. A missing identity specification is an error; it must not make two incomplete rows appear to match. Incomplete candidates stay in `MATCH_REVIEW` until evidence fills the required fields.

For duplicate detection, the validator compares identity text case-insensitively and collapses whitespace; it does not rewrite the stored text. This retains the existing validator's comparison convention. `ACTIVE` and `INACTIVE` rows both participate in uniqueness checks. Keep inactive rows so retired `target_id` values cannot be reused.

Hardware specification fields have no controlled vocabulary beyond the formats above. Preserve manufacturer wording, especially the exact CPU suffix and OS edition. Record storage capacity as total decimal GB and put the individual drives and capacities in `storage_layout`.

## Shopee source mapping

Shopee `source_model_id` values are kept outside `TARGET_MASTER`, in a separate `SOURCE_MODEL_MAP` table. Its header template is `config/source_model_map_template.csv`:

```text
source_model_id,target_id,reviewer,reviewed_at,notes
```

Each row records one reviewed `source_model_id` to `target_id` link. A `source_model_id` can map to at most one target; multiple source model IDs may map to the same target configuration. A mapping's `target_id` must refer to a `TARGET_MASTER` record. Put ambiguous links in `MATCH_REVIEW` and do not create a mapping until a reviewer resolves them. IDs remain text, including any leading zeroes. Validation rules are listed under [Validation behavior](#validation-behavior).

## `MATCH_REVIEW` workflow

Use `MATCH_REVIEW` for new, incomplete, ambiguous, or rejected candidates. Its initial header template is `config/match_review_template.csv`. The approved statuses are exactly:

- `NEW`: captured, not yet assessed.
- `INCOMPLETE`: one or more required identity specifications or source evidence are missing.
- `REVIEW_REQUIRED`: enough evidence exists to assess the candidate, but a person must resolve its identity or mapping.
- `REJECTED`: a designated manager or category owner decided the candidate should not be tracked.

The candidate template allows missing specifications so they can be gathered during review. A candidate moves into `TARGET_MASTER` only when its identity fields and source URL are complete and a designated manager or category owner has approved it. Record that person's name and approval date in `reviewer` and `reviewed_at`. No approver is assigned by this project documentation. Add its reviewed Shopee link separately to `SOURCE_MODEL_MAP`. Rejected and unresolved candidates remain in `MATCH_REVIEW`, not `TARGET_MASTER`.

Promotion is a manual step. No command in this repository moves, copies, or maps a review row into `TARGET_MASTER` or `SOURCE_MODEL_MAP`.

### Seeding the candidate universe from the Top 100

The initial candidate universe is the full accepted Phase 4 Top 100. Seed a local candidate file with:

```bash
python seed_match_review.py
```

- Reads `data/output/top_100_targets.csv` (override with `--top-100`) and writes `data/review/match_review_candidates.csv` (override with `--output`). Both paths are ignored by Git.
- Creates one `NEW` row per distinct `model_id`, copied as an exact string into `source_model_id`. A repeated `model_id` keeps its first (highest-ranked) row.
- Copies the Shopee listing name into `candidate_name`, and records the run ID, rank, shop ID, item ID, and Shopee model name in `notes` so a reviewer can trace the listing.
- Leaves `review_id`, every identity specification, `source_url`, `prepared_by`, `reviewer`, and `reviewed_at` blank. It does not guess CPU, GPU, RAM, storage, display, OS, URLs, approvals, target IDs, or mappings from titles.
- Never creates `TARGET_MASTER` or `SOURCE_MODEL_MAP` rows, never contacts Google Sheets, and refuses to overwrite an existing output file.
- Stops without writing anything if the Top 100 file is missing, lacks required columns, or has a blank or non-digit `model_id`. The output is checked against the `MATCH_REVIEW` contract before it is written.
- Prints counts only.

On 2026-09-29, seeding the accepted Top 100 produced 100 `NEW` candidates for 100 distinct source model IDs. That is a count of Shopee model IDs, not of configurations: several source IDs may later prove to be the same configuration, and incomplete or ambiguous candidates remain under review. Import the file into the `MATCH_REVIEW` tab manually if desired; this project does not write it to the sheet.

The tracking scope is the full accepted Top 100: every distinct laptop configuration represented by those source IDs should be tracked once its exact specifications and evidence are verified. Multiple source IDs can link to one configuration. Candidates without enough evidence stay in review. The designated owner may approve the reviewed set as a batch, with the approver name and date recorded on each `TARGET_MASTER` row.

## Validation behavior

`src/laptop_tracker/target_master.py` validates the approved `TARGET_MASTER` schema using exact strings:

- IDs, numeric specifications, dates, status values, and absolute HTTP(S) source URLs are format-checked; values are never coerced or silently trimmed.
- Missing required fields produce `MISSING_VALUE`; invalid formats, values, URLs, and dates produce errors. A row with any error is excluded from the validated output.
- Duplicate `target_id` values are rejected case-insensitively. Every row in a duplicate configuration group is flagged; the validator never chooses one row over another.
- Rows missing any identity field fail validation and are skipped by duplicate-identity grouping. Missing specifications are not treated as matching values.
- `TARGET_MASTER` needs at least one data row to pass validation. A header-only file returns `NO_DATA_ROWS`.

`src/laptop_tracker/supporting_tables.py` validates the two supporting tables with the same exact-string, no-trim conventions and the same error-report columns. Unlike `TARGET_MASTER`, whose optional columns may be omitted, each supporting table's header row must match its `config/` template exactly, including column order.

**`SOURCE_MODEL_MAP`**

- `source_model_id` is required and must be digits only, matching Shopee model IDs in `raw`. It stays text: `0099` and `99` are different IDs.
- A `source_model_id` may appear on only one row. Every row of a repeated ID is flagged `DUPLICATE_SOURCE_MODEL_ID`; none is chosen. Several source IDs may share one `target_id`.
- `target_id` is required and uses the `TGT-000001` format. It must exist in `TARGET_MASTER` (`UNKNOWN_TARGET_ID`), and the referenced `TARGET_MASTER` row must itself pass validation (`TARGET_NOT_VALID`). A mapping is therefore always checked together with a `TARGET_MASTER` snapshot.
- `reviewer` and `reviewed_at` are required, following the existing decision-file convention; `reviewed_at` must be a valid `YYYY-MM-DD` date. `notes` is optional free text.
- A header-only mapping table passes, because having no reviewed links yet is a valid state.

**`MATCH_REVIEW`**

- `status` is required and must be exactly `NEW`, `INCOMPLETE`, `REVIEW_REQUIRED`, or `REJECTED`.
- Identity specifications and `source_url` may be blank while research is under way. A nonblank value must meet the same format as its `TARGET_MASTER` column (for example `16` rather than `16GB`, `1000` rather than `1TB`, and `1920x1080`), and must not have surrounding whitespace.
- `source_model_id` is optional; if present it must be digits only. It may appear on several review rows, so alternative candidate configurations for one ambiguous source ID can be recorded.
- Blank specifications never match each other: review rows are not grouped, deduplicated, or compared as configurations.
- `reviewer` and `reviewed_at` are optional but must be recorded together, and `reviewed_at` must be a valid date. A `REJECTED` row requires both, because a rejection is a recorded decision.
- A nonblank `review_id` must be unique (case-insensitive); blank IDs are allowed.
- A header-only review table passes.
- The check reports, for information only, how many rows have every identity field and `source_url` filled in a valid format, and how many rows have each status. These counts never promote, approve, or map a row.

## Local validation path

The master spreadsheet is authoritative. Export local CSV snapshots and run either command:

```bash
python check_target_master.py data/review/target_master_export.csv
python check_target_tables.py \
  --target-master data/review/target_master_export.csv \
  --source-model-map data/review/source_model_map_export.csv \
  --match-review data/review/match_review_export.csv
```

Both use no credentials and make no Google calls. `check_target_tables.py` accepts any combination of the three tables, except that `--source-model-map` requires `--target-master` so target references can be checked. For each table it always writes `data/output/<table>_validation_errors.csv`, and writes `data/output/<table>_validated.csv` only if that table passes. A previous validated copy is removed first, so a failing check never leaves stale output. A validated `MATCH_REVIEW` copy means only that the rows are well formed; it is not an approval. When both supporting tables are checked, it also prints how many mapped source IDs still have a non-`REJECTED` review row, for information.

The console prints table names, counts, and error codes, not row values. The error reports can contain cell values, so keep real exports and reports local and out of Git. The exit code is `0` only when every requested table passes.

## Read-only Google Sheets path

```bash
python check_target_tables.py --from-sheets
```

This reads the `TARGET_MASTER`, `SOURCE_MODEL_MAP`, and `MATCH_REVIEW` tabs from the configured spreadsheet using the existing `.env` settings, service-account credentials, and the read-only `spreadsheets.readonly` scope. It calls only `get_all_values()` and runs the same validators and local reports as the CSV path. It never requests the write scope and never writes, clears, or creates tabs. A missing tab is reported with its template path and the check fails. The spreadsheet title and cell values are not printed.

`sheets_source.read_target_table_values()` is the reusable reader. The later exact-matching phase should consume only rows that pass these validators. The first live read-only check on 2026-09-29 found that `TARGET_MASTER` has no header and both supporting tabs are missing. Exact retailer matching remains a later phase.

## Unspecified behaviors and chosen defaults

These behaviors were not covered by the approved decisions. The implementation uses the least destructive option, and each should be confirmed or changed by the designated manager or category owner once named:

| Question | Current behavior |
|---|---|
| May a mapping point to an `INACTIVE` target? | Allowed and counted (`links_to_inactive_targets`), not rejected. No policy has been approved. |
| May a mapping point to a `TARGET_MASTER` row that fails validation? | Rejected as `TARGET_NOT_VALID`, so downstream code never consumes a link to an unvalidated configuration. Nothing is deleted. |
| What is the `review_id` scheme? | None is approved. Any text is accepted, nonblank IDs must be unique, and the seeding command leaves the field blank. |
| Who may review a `SOURCE_MODEL_MAP` link? | Not specified. A reviewer name and date are required, but names are not checked against an approver list, because no approver has been designated. |
| When a candidate is promoted, is its review row removed, kept, or given a new status? | Not specified. No row is removed automatically; the overlap count shows mapped IDs whose review rows remain open. |
| Must `status` agree with the evidence? For example, a `NEW` row with complete specifications. | Not enforced. Status is a human judgement; only the allowed values are checked. |
| Who is `prepared_by` for seeded rows? | Left blank rather than attributed to a person or tool. |

## Decisions still to operationalize

- Identify the designated manager or category owner who approves additions and retirements. This document deliberately does not assign one.
- Prepare actual product configuration rows from authoritative evidence; the accepted Phase 4 Shopee model list alone does not verify CPU, GPU, RAM, storage, display, or OS variants. The seeded candidate file is the starting worklist.
- Add the approved header to `TARGET_MASTER` and create `SOURCE_MODEL_MAP` and `MATCH_REVIEW` in the master spreadsheet from the templates, then rerun the read-only `--from-sheets` check.
- Confirm or change the defaults in the table above.
- Define the downstream interface that the later exact-matching phase will consume from the validated tables.
- Add a coverage check showing which of the 100 accepted Top 100 source model IDs have reviewed mappings and which remain unresolved in `MATCH_REVIEW`.
