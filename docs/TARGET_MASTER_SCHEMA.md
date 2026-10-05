# `TARGET_MASTER` Schema and Review Workflow

**Policy updated:** On 2026-10-05, the user set a five-field core identity and authorized rule-based promotion of candidates with those fields complete. This supersedes the ten-field identity proposal recorded on 2026-09-29. The Python validators, CSV templates, and Google Sheet have not yet been synchronized; they still implement the earlier rule. Do not treat this document update as a code or Sheets migration.

## Purpose and authority

`TARGET_MASTER` is the persistent catalog of laptop configurations the tracker intends to compare. It is not rebuilt when the weekly Top 100 changes. The Google Sheet is the master copy; local CSV exports are validation snapshots. Candidates with incomplete core specifications remain in `MATCH_REVIEW`.

The **core identity key** is `model`, `screen_size_in`, `cpu_series`, `ram_capacity_gb`, and `storage_capacity_gb`. A candidate is core-complete when all five have unambiguous values. `resolution`, `display_type`, `refresh_rate_hz`, `gpu_model`, and `os` are lower-priority secondary details: retain them when available, but their absence does not block core completion, promotion, or a core match. Known secondary details can increase confidence; explicit contradictions must be recorded and surfaced for review rather than silently overwritten. They do not create a second target row by themselves.

`brand` is useful context and should be retained when known, but it is not one of the five core fields. Values must be based on available evidence; do not invent missing specifications.

## `TARGET_MASTER` columns under the updated policy

The existing header order is still defined by `config/target_master_template.csv`. Keep the Google Sheet headers unchanged during data updates. The required/optional designations below record the 2026-10-05 policy; the local template and validator have not yet been changed to match it.

| Column | Required by updated policy | Meaning and format | Matching role |
|---|---:|---|---:|
| `target_id` | Yes | Stable ID exactly like `TGT-000001`. Never derive it from specs or reuse it. | No; unique ID |
| `status` | Yes | `ACTIVE` or `INACTIVE` (uppercase). Retire configurations by setting `INACTIVE`; do not delete rows. | No |
| `brand` | No | Manufacturer brand, when known; useful context. | Context |
| `model` | Yes | Laptop model name/code. | Core |
| `screen_size_in` | Yes | Positive diagonal display size in inches, up to two decimal places. | Core |
| `resolution` | No | `WIDTHxHEIGHT` with lowercase `x`, for example `1920x1080`. | Secondary |
| `display_type` | No | Exact display type, such as the manufacturer-stated panel technology. | Secondary |
| `refresh_rate_hz` | No | Positive integer in Hz. | Secondary |
| `cpu_brand` | No | CPU manufacturer/brand. | No |
| `cpu_series` | Yes | Exact CPU series, as stated in available evidence. | Core |
| `cpu_generation` | No | CPU generation, if verified. | No |
| `cpu_cores` | No | Positive integer core count, if verified. | No |
| `ram_capacity_gb` | Yes | Positive integer in GB. | Core |
| `ram_type` | No | Memory type as stated in product evidence. | No |
| `storage_capacity_gb` | Yes | Total capacity in decimal GB: `1 TB = 1000 GB`. | Core |
| `storage_type` | No | Storage technology or combination, as verified. | No |
| `storage_layout` | No | Per-drive layout, for example `1x512GB SSD + 1x1000GB HDD`. | No |
| `gpu_type` | No | Graphics type, if verified. | No |
| `gpu_brand` | No | GPU brand, if verified. | No |
| `gpu_model` | No | Exact GPU model, if known. | Secondary |
| `vram` | No | VRAM detail as stated by the evidence. | No |
| `os` | No | Exact preinstalled OS/edition, if known. | Secondary |
| `battery_capacity` | No | Battery capacity as stated in the evidence. | No |
| `build_material` | No | Build material as stated in the evidence. | No |
| `manufacturer_model_code` | No | Manufacturer model code/SKU; useful evidence, not the identity key. | No |
| `series` | No | Product line or series; descriptive, not identity. | No |
| `cpu_model` | No | Full CPU model string, including suffix, if available. | No |
| `color` | No | Descriptive color variant. | No |
| `variant_notes` | No | Descriptive variant details. | No |
| `manufacturer_part_number` | No | Manufacturer part number or SKU; evidence only, not a stand-alone identity rule. | No |
| `source_url` | No | Helpful verification link, when available. Do not invent one. | Evidence |
| `reviewer` | No | Approval source. For rule-based promotion, use `Owner-approved five-core-field rule`; a personal name is not required. | Audit |
| `reviewed_at` | No | Date the row was promoted, in `YYYY-MM-DD` format, if recorded. | Audit |
| `notes` | No | Descriptive free text. | No |

The uniqueness key is exactly the five core fields: `model`, `screen_size_in`, `cpu_series`, `ram_capacity_gb`, and `storage_capacity_gb`. Normalize only harmless formatting such as case, surrounding whitespace, and numeric units before comparing. Do not use fuzzy model matching. A missing core value means the row is not core-complete; blanks never match one another.

For duplicate detection, the validator compares identity text case-insensitively and collapses whitespace; it does not rewrite the stored text. This retains the existing validator's comparison convention. `ACTIVE` and `INACTIVE` rows both participate in uniqueness checks. Keep inactive rows so retired `target_id` values cannot be reused.

Specification fields have no controlled vocabulary beyond the formats above. Preserve source wording. Store RAM and storage capacities as integer GB, using decimal units (`1 TB = 1000 GB`). Keep full CPU model, RAM/storage types, and storage layout as additional evidence where available; these are not part of the core identity key.

Rows that share the same five-field core key share one `target_id`, even if secondary details are missing. If secondary values disagree, do not overwrite known values silently; record the conflict in notes and flag it for review. Missing secondary values alone do not create a conflict or prevent promotion.

## Shopee source mapping

Shopee `source_model_id` values are kept outside `TARGET_MASTER`, in a separate `SOURCE_MODEL_MAP` table. Its header template is `config/source_model_map_template.csv`:

```text
source_model_id,target_id,reviewer,reviewed_at,notes
```

Each row records one `source_model_id` to `target_id` link. A `source_model_id` can map to at most one target; multiple source model IDs may map to the same target configuration. A mapping's `target_id` must refer to a `TARGET_MASTER` record. Add a mapping when a candidate is promoted under the five-core-field rule. IDs remain text, including any leading zeroes.

For the user's rule-based batch promotion, record `Owner-approved five-core-field rule` in `reviewer` and the actual promotion date in `reviewed_at`; a personal name is not required. Do not use this rule for a candidate with missing or ambiguous core fields.

## `MATCH_REVIEW` workflow

Use `MATCH_REVIEW` for new, incomplete, ambiguous, complete-but-not-yet-transferred, or rejected candidates. Its initial header template is `config/match_review_template.csv`. The desired statuses are:

- `NEW`: captured, not yet assessed.
- `INCOMPLETE`: one or more core fields are missing or ambiguous.
- `COMPLETE`: the five core fields are populated and unambiguous; eligible for rule-based promotion. Missing secondary fields and source URL do not block this status.
- `REVIEW_REQUIRED`: a core identity conflict or unresolved ambiguity prevents safe promotion.
- `REJECTED`: a designated manager or category owner decided the candidate should not be tracked.

The candidate template allows missing specifications so they can be gathered during review. Under the user's 2026-10-05 standing rule, any non-rejected candidate with all five core fields filled and no core ambiguity is approved for promotion. Copy one `TARGET_MASTER` row per unique core key, assign or reuse a stable `target_id`, and map each associated `source_model_id` in `SOURCE_MODEL_MAP`. Keep its `MATCH_REVIEW` row with status `COMPLETE` as the review trail. Rejected, incomplete, and core-ambiguous candidates remain in `MATCH_REVIEW`.

Secondary details should be copied when known. Their absence never blocks promotion. If known secondary values conflict among candidates with the same core key, retain the conflict in notes and flag it; do not overwrite an existing master value silently or create a second target solely because a secondary field differs.

Promotion and mapping are currently spreadsheet/manual operations. No command in this repository yet moves, copies, assigns target IDs, or maps review rows under this policy.

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

The initial tracking scope is the accepted Top 100 plus sibling Shopee variations that the user has explicitly included in `MATCH_REVIEW`. Several `source_model_id` values may represent the same core configuration and link to one target. Incomplete or ambiguous core identities stay in review. The user authorized promotion based on the five core fields on 2026-10-05.

## Validation behavior

### Implementation status

This section describes the validator currently in the repository, which has not been updated for the 2026-10-05 policy. It still requires the previous ten-field identity, requires source URLs and per-row reviewer/date values, and permits only `NEW`, `INCOMPLETE`, `REVIEW_REQUIRED`, and `REJECTED` in `MATCH_REVIEW`. It will reject some rows this policy considers promotable and does not implement automatic target-ID assignment or mapping. Update the templates, validators, and sheet status validation before using local checks or code-driven promotion with this policy.

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

These implementation behaviors are not yet aligned with the 2026-10-05 rule. The current code uses the defaults below until the templates and validators are updated:

| Question | Current behavior |
|---|---|
| May a mapping point to an `INACTIVE` target? | Allowed and counted (`links_to_inactive_targets`), not rejected. No policy has been approved. |
| May a mapping point to a `TARGET_MASTER` row that fails validation? | Rejected as `TARGET_NOT_VALID`, so downstream code never consumes a link to an unvalidated configuration. Nothing is deleted. |
| What is the `review_id` scheme? | None is approved. Any text is accepted, nonblank IDs must be unique, and the seeding command leaves the field blank. |
| Who may review a `SOURCE_MODEL_MAP` link? | The user authorized rule-based promotion. The current validator still requires nonblank reviewer/date values; use the policy label above and actual promotion date after synchronization. |
| When a candidate is promoted, is its review row removed, kept, or given a new status? | Updated policy: keep it as `COMPLETE` for traceability. Current validator does not yet allow that status. |
| Must `status` agree with the evidence? For example, a `NEW` row with complete core specifications. | Updated policy: set `COMPLETE` when the five core fields are complete and unambiguous. Current validator only checks allowed values. |
| Who is `prepared_by` for seeded rows? | Left blank rather than attributed to a person or tool. |

## Implementation work still required

- Synchronize the local templates, `TARGET_MASTER` and `MATCH_REVIEW` validators, and Sheet dropdowns with the five-core-field rule and `COMPLETE` status.
- Implement core-key duplicate grouping, stable target-ID reuse/assignment, and one source-model mapping per eligible Shopee variation.
- Implement Phase 8 matching: require exact agreement on the five core fields; use available secondary values as corroboration; flag known contradictions for review.
- Add a coverage check showing which in-scope Shopee variation IDs are mapped and which remain incomplete or ambiguous.
