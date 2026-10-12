# `TARGET_MASTER` Schema and Review Workflow

**Policy updated:** On 2026-10-05, the user set a five-field core identity and authorized rule-based promotion of candidates with those fields complete. This supersedes the ten-field identity proposal recorded on 2026-09-29. On 2026-10-06 the local Python validators were synchronized with this policy; the CSV header templates already contained every column and are unchanged. The user reported on 2026-10-06 that the live tabs pass the read-only validator and that the `MATCH_REVIEW` status dropdown allows `COMPLETE`; this repository has never written to the Sheet.

**Tracking modes (approved 2026-10-08):** `TARGET_MASTER` rows are now either `CONFIGURATION` (the five-field identity above, unchanged) or `LISTING_ONLY` (an individual Shopee listing with no verifiable product model). The local templates and validators implement this. The live Sheet headers have not been migrated yet; see [SHEET_MIGRATION_TRACKING_MODES.md](SHEET_MIGRATION_TRACKING_MODES.md). Rows and snapshots without the new columns stay valid as `CONFIGURATION`.

## Purpose and authority

`TARGET_MASTER` is the persistent catalog of laptop configurations the tracker intends to compare. It is not rebuilt when the weekly Top 100 changes. The Google Sheet is the master copy; local CSV exports are validation snapshots. Candidates with incomplete core specifications remain in `MATCH_REVIEW`.

The **core identity key** is `model`, `screen_size_in`, `cpu_series`, `ram_capacity_gb`, and `storage_capacity_gb`. A candidate is core-complete when all five have unambiguous values. `resolution`, `display_type`, `refresh_rate_hz`, `gpu_model`, and `os` are lower-priority secondary details: retain them when available, but their absence does not block core completion, promotion, or a core match. Known secondary details can increase confidence; explicit contradictions must be recorded and surfaced for review rather than silently overwritten. They do not create a second target row by themselves.

`brand` is useful context and should be retained when known, but it is not one of the five core fields. It may be blank when a genuine product model/code is verified. Values must be based on available evidence; do not invent missing specifications.

## Tracking modes

| | `CONFIGURATION` | `LISTING_ONLY` |
|---|---|---|
| When | A genuine product model name/code is verified. | The product is **confirmed** to have no verifiable product model name/code. |
| Identity | `model`, `screen_size_in`, `cpu_series`, `ram_capacity_gb`, `storage_capacity_gb` | `shop_id` + `item_id` + `source_model_id` (the Shopee variation `model_id`), exact text |
| Required before promotion | All five core fields populated and unambiguous | `shop_id`, `item_id`, `source_model_id`, `tracking_mode_reason`, `classification_evidence` |
| Must be blank | `shop_id`, `item_id`, `source_model_id` in `TARGET_MASTER` (listings link through `SOURCE_MODEL_MAP`) | `model`, `manufacturer_model_code`; unavailable specifications stay blank |
| Cross-retailer exact matching | Yes | Never: no exact match, near-match suggestion, or ambiguity |
| `SOURCE_MODEL_MAP` | Any number of source IDs may link to it | Only its own `source_model_id` may link to it |

Rules that apply to both modes:

- A blank `tracking_mode` means `CONFIGURATION`. Every row made before 2026-10-08 is therefore a `CONFIGURATION` row with its existing `target_id` and values unchanged.
- A blank `model` alone never establishes `LISTING_ONLY`; it may mean research is unfinished. A blank-model row without `LISTING_ONLY` and its evidence fails as a `CONFIGURATION` row with `MISSING_VALUE`.
- Do not classify a product by country of manufacture. Never invent a placeholder model such as `Generic`, `Unbranded`, `No Brand`, `OEM`, `N/A`, `None`, or `Unknown`. The validator rejects these as `PLACEHOLDER_MODEL`.
- `target_id` uniqueness and the `ACTIVE`/`INACTIVE` rule are unchanged and apply across both modes.
- Contradictory records are rejected (`CONTRADICTORY_TRACKING_MODE`): a `LISTING_ONLY` row with a model or manufacturer model code, or a `CONFIGURATION` target carrying listing identifiers. So are unknown modes (`INVALID_VALUE`; values are exact and uppercase).
- `tracking_mode_reason` says why no verifiable model exists. `classification_evidence` says what was checked, for example the listing title, variation names, seller specifications, packaging photos, or a manufacturer search. Neither may be invented.

## `TARGET_MASTER` columns under the updated policy

The existing header order is still defined by `config/target_master_template.csv`. Keep the Google Sheet headers unchanged during data updates. The required/optional designations below record the 2026-10-05 policy and are what the local validator enforces.

"Yes" below means required for `CONFIGURATION` rows. For `LISTING_ONLY` rows, see [Tracking modes](#tracking-modes): the five core fields are not required (and `model` must be blank), while the listing identity, reason, and evidence are.

| Column | Required by updated policy | Meaning and format | Matching role |
|---|---:|---|---:|
| `target_id` | Yes | Stable ID exactly like `TGT-000001`. Never derive it from specs or reuse it. | No; unique ID |
| `status` | Yes | `ACTIVE` or `INACTIVE` (uppercase). Retire configurations by setting `INACTIVE`; do not delete rows. | No |
| `brand` | No | Manufacturer brand, when known; useful context. | Context |
| `model` | Yes (`CONFIGURATION`) | The verified manufacturer model code when one exists (for example `ANV15-42-R2UH`); otherwise the verified model name. See [conventions](#model-and-cpu-series-conventions-2026-10-08). Must be blank for `LISTING_ONLY`; never a placeholder. | Core |
| `screen_size_in` | Yes (`CONFIGURATION`) | Positive diagonal display size in inches, up to two decimal places. | Core |
| `resolution` | No | `WIDTHxHEIGHT` with lowercase `x`, for example `1920x1080`. | Secondary |
| `display_type` | No | Exact display type, such as the manufacturer-stated panel technology. | Secondary |
| `refresh_rate_hz` | No | Positive integer in Hz. | Secondary |
| `cpu_brand` | No | CPU manufacturer/brand. | No |
| `cpu_series` | Yes | Canonical CPU-series form, for example `Core i5`, `Ryzen 7`, `Apple M5`, `Intel N150`. See [conventions](#model-and-cpu-series-conventions-2026-10-08). | Core |
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
| `manufacturer_model_code` | No | Verified manufacturer model code, kept as the evidence for `model` (equal to `model` when a code exists). Never a part number or a name. | No |
| `series` | No | Product-family name (for example `Nitro`, `Vivobook 14`), including the former `model` wording when a code becomes the model. Descriptive, not identity. | No |
| `cpu_model` | No | Full processor wording, including the model number and suffix, as evidenced. Never shortened to the series. | No |
| `color` | No | Descriptive color variant. | No |
| `variant_notes` | No | Descriptive variant details. | No |
| `manufacturer_part_number` | No | Manufacturer part number or SKU; evidence only, not a stand-alone identity rule. | No |
| `source_url` | No | Helpful verification link, when available. Do not invent one. | Evidence |
| `reviewer` | No | Approval source. For rule-based promotion, use `Owner-approved five-core-field rule`; a personal name is not required. | Audit |
| `reviewed_at` | No | Date the row was promoted, in `YYYY-MM-DD` format, if recorded. | Audit |
| `notes` | No | Descriptive free text. | No |
| `tracking_mode` | No (blank = `CONFIGURATION`) | `CONFIGURATION` or `LISTING_ONLY`, exact uppercase. | Selects identity |
| `shop_id` | `LISTING_ONLY` only | Shopee shop ID, digits, exact text. Blank for `CONFIGURATION`. | Listing identity |
| `item_id` | `LISTING_ONLY` only | Shopee item ID, digits, exact text. Blank for `CONFIGURATION`. | Listing identity |
| `source_model_id` | `LISTING_ONLY` only | Shopee variation `model_id`, digits, exact text (leading zeroes kept). Blank for `CONFIGURATION`. | Listing identity |
| `tracking_mode_reason` | `LISTING_ONLY` only | Why no verifiable product model name/code exists. | Audit |
| `classification_evidence` | `LISTING_ONLY` only | What was checked to support the classification. | Audit |

The six tracking-mode columns come after `notes`, so existing Sheet columns keep their positions.

The `CONFIGURATION` uniqueness key is exactly the five core fields: `model`, `screen_size_in`, `cpu_series`, `ram_capacity_gb`, and `storage_capacity_gb`. Normalize only harmless formatting such as case, surrounding whitespace, and numeric units before comparing. Do not use fuzzy model matching. A missing core value means the row is not core-complete; blanks never match one another.

For duplicate detection, the validator uses the Phase 8 matcher's comparison rules ([MATCHING.md](MATCHING.md)): text is compared after Unicode NFC, trademark-mark removal, whitespace collapsing, and case-folding, and `screen_size_in` is compared by numeric value (`14` equals `14.0`). It does not rewrite the stored text. `ACTIVE` and `INACTIVE` rows both participate in uniqueness checks. Keep inactive rows so retired `target_id` values cannot be reused. Only `CONFIGURATION` rows are grouped by the five-field key; `LISTING_ONLY` rows never form configuration duplicates, even when their available specifications agree.

`LISTING_ONLY` uniqueness is per listing variation. Two `LISTING_ONLY` rows with the same `source_model_id` are `DUPLICATE_LISTING` when their shop and item also agree, and `CONFLICTING_LISTING_IDENTITY` when they do not. IDs are compared as exact text, so `0042` and `42` differ.

Specification fields have no controlled vocabulary beyond the formats above. Preserve source wording. Store RAM and storage capacities as integer GB, using decimal units (`1 TB = 1000 GB`). Keep full CPU model, RAM/storage types, and storage layout as additional evidence where available; these are not part of the core identity key.

Rows that share the same five-field core key share one `target_id`, even if secondary details are missing. If secondary values disagree, do not overwrite known values silently; record the conflict in notes and flag it for review. Missing secondary values alone do not create a conflict or prevent promotion.

## Model and CPU-series conventions (2026-10-08)

Approved on 2026-10-08. The five-field `CONFIGURATION` identity is unchanged. These rules fix how
two of its fields are written so that the catalog and retailer extraction produce identical
values. They are implemented once in `src/laptop_tracker/conventions.py`.

**`model`**

- If a genuine manufacturer model code is verified, the code is the `model`, written exactly as
  the manufacturer states it: `ANV15-42-R2UH`, `S3607VA-RP181WSM`, `14-ep0445TU`. No suffix is
  stripped. Keep the same code in `manufacturer_model_code` as its evidence.
- Move the product-family name (the former `model`, for example `Nitro`) to `series`. Record the
  change in `notes`.
- If no code is verified, keep the verified model name and report it as unresolved. Never guess a
  code from a listing title, and never use a part number (`NH.QUCSP.001`, `C4FW3PA`) or a
  machine-type number as the code.
- A model code has this shape: two or more ASCII letter/digit segments joined by ASCII hyphens,
  containing at least one letter, at least one digit, and at least six letters and digits in
  total. The following are never codes:
  - processor, memory, and graphics numbers (`i5-13420H`, `R5-5500U`, `X1-26-100`, `DDR5-4800`,
    `GTX1650-…`, `RTX-4050`);
  - segments that are a quantity with a unit (`512GB`, `120Hz`, `16in`).
- `LISTING_ONLY` targets are unaffected: `model` and `manufacturer_model_code` stay blank.

**`cpu_series`**

| Processor line | Canonical form | Example evidence |
|---|---|---|
| Intel Core, Core Ultra (tiered) | `Core i3/i5/i7/i9`, `Core 3/5/7/9`, `Core Ultra 5/7/9` | `Intel Core i5-13420H` or a complete number such as `Raptor Lake i5-13420H` → `Core i5` |
| AMD Ryzen (tiered) | `Ryzen 3/5/7/9`, `Ryzen AI 5/7/9` | `AMD Ryzen 7 170` → `Ryzen 7` |
| Qualcomm Snapdragon X | `Snapdragon X`, `Snapdragon X Elite`, `Snapdragon X Plus` | `Snapdragon X X1-26-100` → `Snapdragon X` |
| Apple M-series | `Apple M<n>` with `Pro`/`Max`/`Ultra` when stated | `Apple M5`, `Apple M4 Max` |
| Apple A-series | `Apple A<nn>` with `Pro` when stated | `Apple A18 Pro` |
| Intel N-series (no tier digit) | `Intel N<number>` | `Intel® Processor N150` → `Intel N150` |
| Intel Celeron (no tier digit) | `Celeron N<number>` | `Intel Celeron N4020` → `Celeron N4020` |

Tiered lines use the family form, and matching is by that family (owner policy, 2026-10-12): `i5-13420H` and `i5-1335U` are both `Core i5`, and the full processor wording stays in `cpu_model` and in extraction evidence. Untiered Intel lines keep the processor number, so distinct
processors (`N100`/`N150`, `N4020`/`N4500`) are never merged. Apple chips keep the `Apple` prefix.
The full processor wording stays in `cpu_model`. Other lines are not yet supported: Pentium,
Athlon, and incomplete Intel numbers (a bare `i5`, `i3-N305`) stay in review until evidence
supports an explicit rule.

**Applying the conventions.** `propose_catalog_conventions.py` reads a validated snapshot and
writes a per-`target_id` proposal (`READY`, `NEEDS_CONFIRMATION`, `NO_CHANGE`, or `UNRESOLVED`)
with evidence, the wording to preserve, and the core-key duplicates the proposals would create.
The owner applies approved proposals in the Sheet using
[SHEET_MIGRATION_MODEL_CPU_CONVENTIONS.md](SHEET_MIGRATION_MODEL_CPU_CONVENTIONS.md).

## Shopee source mapping

Shopee `source_model_id` values are kept outside `TARGET_MASTER`, in a separate `SOURCE_MODEL_MAP` table. Its header template is `config/source_model_map_template.csv`:

```text
source_model_id,target_id,reviewer,reviewed_at,notes
```

Each row records one `source_model_id` to `target_id` link. A `source_model_id` can map to at most one target; multiple source model IDs may map to the same `CONFIGURATION` target. A mapping's `target_id` must refer to a valid `TARGET_MASTER` record of either mode. Add a mapping when a candidate is promoted. IDs remain text, including any leading zeroes.

A `LISTING_ONLY` target represents exactly one source listing. Only a mapping whose `source_model_id` equals the target's own `source_model_id` may point to it (`LISTING_TARGET_MISMATCH` otherwise). That source ID may not point to any other target (`LISTING_SOURCE_MAPPED_ELSEWHERE`). The check reports `links_to_listing_only_targets` and `listing_only_targets_without_mapping` for information.

For the user's rule-based batch promotion, record `Owner-approved five-core-field rule` in `reviewer` and the actual promotion date in `reviewed_at`; a personal name is not required. Do not use this rule for a candidate with missing or ambiguous core fields.

## `MATCH_REVIEW` workflow

Use `MATCH_REVIEW` for new, incomplete, ambiguous, complete-but-not-yet-transferred, or rejected candidates. Its initial header template is `config/match_review_template.csv`. The desired statuses are:

- `NEW`: captured, not yet assessed.
- `INCOMPLETE`: one or more required fields for the candidate's mode are missing or ambiguous, or its mode is not yet determined.
- `COMPLETE`: the candidate satisfies its tracking mode's promotion requirements and is eligible for rule-based promotion. For `CONFIGURATION` (or a blank mode), the five core fields are populated and unambiguous. For `LISTING_ONLY`, `shop_id`, `item_id`, `source_model_id`, `tracking_mode_reason`, and `classification_evidence` are filled, and `model`/`manufacturer_model_code` are blank; no product model is needed. Missing secondary fields and source URL do not block this status.
- `REVIEW_REQUIRED`: a core identity conflict, a classification conflict (for example, evidence for and against a verifiable model), or another unresolved ambiguity prevents safe promotion. Contradictory values may stay on these rows while the conflict is open.

`MATCH_REVIEW` has the same five tracking columns as `TARGET_MASTER`, after `notes`: `tracking_mode`, `shop_id`, `item_id`, `tracking_mode_reason`, `classification_evidence` (the existing `source_model_id` column is the listing variation). A `CONFIGURATION` candidate may keep its `shop_id`/`item_id` as trace evidence. A blank mode on a `COMPLETE` row means `CONFIGURATION`, which keeps the 326 existing `COMPLETE` rows valid. Promote a `COMPLETE` `LISTING_ONLY` candidate by copying its listing identity, reason, and evidence into a new `LISTING_ONLY` `TARGET_MASTER` row and mapping its own `source_model_id` to that target.
- `REJECTED`: a designated manager or category owner decided the candidate should not be tracked.

The candidate template allows missing specifications so they can be gathered during review. Under the user's 2026-10-05 standing rule, any non-rejected candidate with all five core fields filled and no core ambiguity is approved for promotion. Copy one `TARGET_MASTER` row per unique core key, assign or reuse a stable `target_id`, and map each associated `source_model_id` in `SOURCE_MODEL_MAP`. Keep its `MATCH_REVIEW` row with status `COMPLETE` as the review trail. Rejected, incomplete, and core-ambiguous candidates remain in `MATCH_REVIEW`.

Secondary details should be copied when known. Their absence never blocks promotion. If known secondary values conflict among candidates with the same core key, retain the conflict in notes and flag it; do not overwrite an existing master value silently or create a second target solely because a secondary field differs.

Promotion and mapping are spreadsheet/manual operations, and they deliberately remain so: no command in this repository moves, copies, assigns target IDs, or maps review rows. As of the user-reported 2026-10-06 sheet work, promoted rows and mappings exist in the live tabs (168 targets, 326 mappings, 326 `COMPLETE` review rows kept as the audit trail). Use the [Top 100 coverage audit](#top-100-coverage-audit) to see which accepted Top 100 IDs still need review.

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

Synchronized with the 2026-10-05 policy on 2026-10-06 and with the tracking-mode policy on 2026-10-08. `TARGET_MASTER` requires `target_id`, `status`, and the five core fields as header columns. A `CONFIGURATION` row (including a blank, legacy mode) needs the five core values. A `LISTING_ONLY` row needs the listing identity, reason, and evidence instead. Secondary fields, `brand`, `source_url`, `reviewer`, `reviewed_at`, and the six tracking-mode columns may be omitted from older snapshots. Filled values are always format-checked. `MATCH_REVIEW` accepts `COMPLETE`, judged against the row's tracking mode. The validators do not assign target IDs, promote rows, or create mappings.

`src/laptop_tracker/target_master.py` validates the approved `TARGET_MASTER` schema using exact strings:

- IDs, numeric specifications, dates, status values, and absolute HTTP(S) source URLs (when present) are format-checked; values are never coerced or silently trimmed.
- Missing required fields produce `MISSING_VALUE`; invalid formats, values, URLs, and dates produce errors. A row with any error is excluded from the validated output.
- Duplicate `target_id` values are rejected case-insensitively across both modes. Every row in a duplicate configuration or listing group is flagged; the validator never chooses one row over another.
- `CONFIGURATION` rows missing any of the five core fields fail validation and are skipped by duplicate-identity grouping. Missing specifications are not treated as matching values.
- `tracking_mode` must be blank, `CONFIGURATION`, or `LISTING_ONLY` (`INVALID_VALUE` otherwise). Listing IDs must be digits. Mode contradictions are `CONTRADICTORY_TRACKING_MODE`, placeholder models are `PLACEHOLDER_MODEL`, and listing collisions are `DUPLICATE_LISTING` or `CONFLICTING_LISTING_IDENTITY` (see [Tracking modes](#tracking-modes)).
- `TARGET_MASTER` needs at least one data row to pass validation. A header-only file returns `NO_DATA_ROWS`.

`src/laptop_tracker/supporting_tables.py` validates the two supporting tables with the same exact-string, no-trim conventions and the same error-report columns. Unlike `TARGET_MASTER`, whose optional columns may be omitted, `SOURCE_MODEL_MAP`'s header row must match its `config/` template exactly, including column order. `MATCH_REVIEW` is read by header name (since 2026-10-08, because the live tab's column order differs from the template): its header must contain exactly the template's columns, or exactly the pre-2026-10-08 columns without the five tracking columns, in any order. Missing, duplicate, blank, and unknown headers, and a partial set of tracking columns, are rejected. Legacy rows are read with blank tracking columns, and validated output always uses template order.

**`SOURCE_MODEL_MAP`**

- `source_model_id` is required and must be digits only, matching Shopee model IDs in `raw`. It stays text: `0099` and `99` are different IDs.
- A `source_model_id` may appear on only one row. Every row of a repeated ID is flagged `DUPLICATE_SOURCE_MODEL_ID`; none is chosen. Several source IDs may share one `target_id`.
- `target_id` is required and uses the `TGT-000001` format. It must exist in `TARGET_MASTER` (`UNKNOWN_TARGET_ID`), and the referenced `TARGET_MASTER` row must itself pass validation (`TARGET_NOT_VALID`). A mapping is therefore always checked together with a `TARGET_MASTER` snapshot.
- A link to a `LISTING_ONLY` target must come from that target's own `source_model_id` (`LISTING_TARGET_MISMATCH`). A `LISTING_ONLY` target's `source_model_id` may not link to another target (`LISTING_SOURCE_MAPPED_ELSEWHERE`).
- `reviewer` and `reviewed_at` are required, following the existing decision-file convention; `reviewed_at` must be a valid `YYYY-MM-DD` date. `notes` is optional free text.
- A header-only mapping table passes, because having no reviewed links yet is a valid state.

**`MATCH_REVIEW`**

- `status` is required and must be exactly `NEW`, `INCOMPLETE`, `COMPLETE`, `REVIEW_REQUIRED`, or `REJECTED`.
- A `COMPLETE` row must satisfy its tracking mode. `CONFIGURATION` or blank needs all five core fields and a non-placeholder model. `LISTING_ONLY` needs `shop_id`, `item_id`, `source_model_id`, `tracking_mode_reason`, and `classification_evidence`, with `model` and `manufacturer_model_code` blank. Failures are `MISSING_VALUE`, `CONTRADICTORY_TRACKING_MODE`, or `PLACEHOLDER_MODEL`. Secondary fields and `source_url` may stay blank. Rows with other statuses may hold unresolved classification conflicts. The validator cannot judge whether values are unambiguous; that remains the reviewer's call.
- `tracking_mode` must be blank, `CONFIGURATION`, or `LISTING_ONLY` on every row.
- Identity specifications and `source_url` may be blank while research is under way. A nonblank value must meet the same format as its `TARGET_MASTER` column (for example `16` rather than `16GB`, `1000` rather than `1TB`, and `1920x1080`), and must not have surrounding whitespace.
- `source_model_id` is optional; if present it must be digits only. It may appear on several review rows, so alternative candidate configurations for one ambiguous source ID can be recorded.
- Blank specifications never match each other: review rows are not grouped, deduplicated, or compared as configurations.
- `reviewer` and `reviewed_at` are optional but must be recorded together, and `reviewed_at` must be a valid date. A `REJECTED` row requires both, because a rejection is a recorded decision.
- A nonblank `review_id` must be unique (case-insensitive); blank IDs are allowed.
- A header-only review table passes.
- The check reports, for information only, how many rows have all five core fields filled in a valid format (`rows_with_complete_core_identity`), how many `COMPLETE` rows are `LISTING_ONLY` (`complete_listing_only_rows`), and how many rows have each status. These counts never promote, approve, or map a row.

## Local validation path

The master spreadsheet is authoritative. Export local CSV snapshots and run either command:

```bash
python check_target_master.py data/review/target_master_export.csv
python check_target_tables.py \
  --target-master data/review/target_master_export.csv \
  --source-model-map data/review/source_model_map_export.csv \
  --match-review data/review/match_review_export.csv
```

Both use no credentials and make no Google calls. `check_target_tables.py` also accepts its own `*_validated.csv` copies (the leading `source_row_number` column is dropped before validation) and pre-2026-10-08 headers without the tracking-mode columns. It accepts any combination of the three tables, except that `--source-model-map` requires `--target-master` so target references can be checked. For each table it always writes `data/output/<table>_validation_errors.csv`, and writes `data/output/<table>_validated.csv` only if that table passes. A previous validated copy is removed first, so a failing check never leaves stale output. A validated `MATCH_REVIEW` copy means only that the rows are well formed; it is not an approval. When both supporting tables are checked, it also prints how many mapped source IDs still have a non-`REJECTED` review row, for information.

The console prints table names, counts, and error codes, not row values. The error reports can contain cell values, so keep real exports and reports local and out of Git. The exit code is `0` only when every requested table passes.

## Read-only Google Sheets path

```bash
python check_target_tables.py --from-sheets
```

This reads the `TARGET_MASTER`, `SOURCE_MODEL_MAP`, and `MATCH_REVIEW` tabs from the configured spreadsheet using the existing `.env` settings, service-account credentials, and the read-only `spreadsheets.readonly` scope. It calls only `get_all_values()` and runs the same validators and local reports as the CSV path. It never requests the write scope and never writes, clears, or creates tabs. A missing tab is reported with its template path and the check fails. The spreadsheet title and cell values are not printed.

`sheets_source.read_target_table_values()` is the reusable reader. The first live read-only check on 2026-09-29 found that `TARGET_MASTER` had no header and both supporting tabs were missing. The user-reported read-only check on 2026-10-06 passed all three tabs: `TARGET_MASTER` 168/168, `SOURCE_MODEL_MAP` 326/326, and `MATCH_REVIEW` 5,154/5,154 (326 `COMPLETE`, 3,422 `INCOMPLETE`, 1,406 `REVIEW_REQUIRED`, 0 `REJECTED`). Its informational count of 326 mapped source IDs still present in `MATCH_REVIEW` is expected, because `COMPLETE` rows are kept as the audit trail; it is not a failure.

## Phase 8 handoff

`TARGET_MASTER` reaches the Phase 8 matcher (`match_offers.py`, see [MATCHING.md](MATCHING.md)) in either of two ways:

```bash
# Read-only from the authoritative sheet tab (TARGET_MASTER only; never raw, never writes):
python match_offers.py --target-master-from-sheets --observations offer_observations.csv

# From a local snapshot: a sheet export or the validated copy written by check_target_tables.py
python check_target_tables.py --from-sheets
python match_offers.py --target-master data/output/target_master_validated.csv \
  --observations offer_observations.csv
```

Both paths validate `TARGET_MASTER` with `validate_target_master` again. If any row fails, or the tab is missing, matching stops before any result file is written and earlier result files are removed. Only `CONFIGURATION` rows are indexed. `LISTING_ONLY` rows are counted and excluded, so they never yield an exact match, a near-match suggestion, or configuration ambiguity. A catalog with no `CONFIGURATION` rows is reported clearly on stderr, and every offer is left unmatched or in review. The matcher does not read `SOURCE_MODEL_MAP` or `MATCH_REVIEW`. Validated copies are accepted because their leading `source_row_number` column is dropped before validation (`read_table_snapshot_csv`).

## Top 100 coverage audit

```bash
python audit_top_100_coverage.py \
  --target-master data/output/target_master_validated.csv \
  --source-model-map data/output/source_model_map_validated.csv \
  --match-review data/output/match_review_validated.csv
python audit_top_100_coverage.py --from-sheets   # the three target tabs, read-only scope
```

- The candidate source is the accepted Phase 4 Top 100 CSV (`--top-100`, default `data/output/top_100_targets.csv`). It needs the same columns as the seeding command, and every `model_id` must be digits. It never reads `raw` or `P1_TOP_100_TARGETS`; that tab is empty and is not the source of truth.
- The three tables are validated with the Phase 5 validators. The audit stops, printing error-code counts, if any table fails or a header or tab is missing.
- IDs are compared as exact text, so leading zeroes matter. A repeated Top 100 `model_id` keeps its first, highest-ranked row.
- Each distinct ID gets one coverage status:
  - `MAPPED_CONFIGURATION`: an ACTIVE `CONFIGURATION` target.
  - `MAPPED_LISTING_ONLY`: an ACTIVE `LISTING_ONLY` target that is this exact listing.
  - `MAPPED_INACTIVE_TARGET`.
  - `MAPPED_INVALID_TARGET`: the target is absent from the `TARGET_MASTER` snapshot.
  - `LISTING_IDENTITY_MISMATCH`: a `LISTING_ONLY` target whose `shop_id`, `item_id`, or `source_model_id` differs from the Top 100 row's `shop_id`, `item_id`, or `model_id`.
  - `UNMAPPED_IN_REVIEW`: unresolved, grouped by its review statuses.
  - `MISSING`: in neither table.
- Each row also reports `target_tracking_mode` and the Top 100 `shop_id`/`item_id`.
- It separately lists `COMPLETE` review rows with:
  - no `source_model_id`;
  - no mapping;
  - a mapping to an inactive or invalid target;
  - a listing identity that differs from the target's;
  - a review mode that differs from the target's mode (`TRACKING_MODE_MISMATCH`).
- It writes `data/output/top_100_coverage.csv` and `data/output/complete_review_without_mapping.csv` (both Git-ignored, removed first on every run) and prints counts only. It never promotes, maps, or assigns IDs, and its exit code is `0` whenever the inputs are valid, whatever the coverage.

On 2026-10-06, run on the accepted Top 100 CSV and the local validated copies from that day's read-only check, the audit reported 100 distinct IDs:

| Coverage | IDs |
|---|---:|
| `MAPPED_CONFIGURATION` (reported as `MAPPED_ACTIVE` before 2026-10-08) | 63 |
| `MAPPED_LISTING_ONLY` | 0 |
| `MAPPED_INACTIVE_TARGET` / `MAPPED_INVALID_TARGET` / `LISTING_IDENTITY_MISMATCH` | 0 / 0 / 0 |
| `UNMAPPED_IN_REVIEW` (`INCOMPLETE` / `REVIEW_REQUIRED`) | 37 (16 / 21) |
| `MISSING` | 0 |
| `COMPLETE` review rows without an ACTIVE mapping | 0 |

Rerun on 2026-10-08 with the tracking-mode code, using the same local legacy snapshots: the counts are unchanged, and every existing row validates as `CONFIGURATION`. The 37 unmapped IDs are pending human review; they have not been resolved. Some may turn out to be `LISTING_ONLY`, but only after the classification evidence is gathered.

## Unspecified behaviors and chosen defaults

The current code uses the defaults below where no policy has been approved:

| Question | Current behavior |
|---|---|
| May a mapping point to an `INACTIVE` target? | Allowed and counted (`links_to_inactive_targets`), not rejected. No policy has been approved. |
| May a mapping point to a `TARGET_MASTER` row that fails validation? | Rejected as `TARGET_NOT_VALID`, so downstream code never consumes a link to an unvalidated configuration. Nothing is deleted. |
| What is the `review_id` scheme? | None is approved. Any text is accepted, nonblank IDs must be unique, and the seeding command leaves the field blank. |
| Who may review a `SOURCE_MODEL_MAP` link? | The user authorized rule-based promotion. The current validator still requires nonblank reviewer/date values; use the policy label above and actual promotion date after synchronization. |
| When a candidate is promoted, is its review row removed, kept, or given a new status? | Updated policy: keep it as `COMPLETE` for traceability. The validator accepts `COMPLETE` since 2026-10-06. |
| Must `status` agree with the evidence? For example, a `NEW` row with complete core specifications. | Updated policy: set `COMPLETE` when the five core fields are complete and unambiguous. The validator rejects `COMPLETE` with a blank core field; it does not force other statuses to `COMPLETE`. |
| Who is `prepared_by` for seeded rows? | Left blank rather than attributed to a person or tool. |
| Which model words count as placeholders? (2026-10-08) | `generic`, `unbranded`, `no brand`, `nobrand`, `non-branded`, `oem`, `none`, `n/a`, `na`, `unknown`, `no model`, and `-`, compared case-insensitively with whitespace collapsed. Extend the list only by decision. |
| May a `CONFIGURATION` target carry `shop_id`/`item_id`/`source_model_id`? (2026-10-08) | No (`CONTRADICTORY_TRACKING_MODE`): configurations link to listings only through `SOURCE_MODEL_MAP`. `MATCH_REVIEW` candidates of either mode may keep them as trace evidence. |
| Which identifiers must a `LISTING_ONLY` row leave blank? (2026-10-08) | `model` and `manufacturer_model_code`, because either one is a verifiable product model/code. `manufacturer_part_number` and available specifications may be recorded when evidenced. |
| Can a `LISTING_ONLY` target be linked from any other source ID? (2026-10-08) | No. It represents one listing variation, so only its own `source_model_id` may link to it. |

## Top 100 verified configurations (2026-10-12)

The owner decided on 2026-10-12 that the tracker's working list is the **Top 100 verified
configurations**. The accepted source-level sales Top 100 from Phase 4 stays unchanged as a
reference. Its unresolved IDs, including the four parked IDs, are historical backlog, not
required members of the working list.

**Selection.** A configuration qualifies when its `target_id` is all of these:

- ACTIVE and `CONFIGURATION`;
- complete on the five core fields (`cpu_series` at family level);
- free of a known unresolved conflict; and
- mapped from at least one contributing source.

A source contributes when all of these hold:

1. It is in the Phase 4 eligible ranking with `eligibility_status` `ELIGIBLE` and
   `present_in_mtd` `True`.
2. Its `model_id` has a validated `SOURCE_MODEL_MAP` row. IDs are exact text, so `0042` and `42`
   are different.
3. It has at least one `COMPLETE` `MATCH_REVIEW` row and no open (`NEW`, `INCOMPLETE`,
   `REVIEW_REQUIRED`) row.
4. Every `COMPLETE` row agrees with the target on the five core fields under the 2026-10-08
   conventions. A target code agrees with a review row recording that code; `M5` agrees with
   `Apple M5`.
5. Its recorded manufacturer code does not differ from the target's.
6. It has no `OPEN` entry in the identity review register.

Missing secondary specifications never block selection.

**Ranking.** Contributions are grouped by `target_id` over the whole qualifying pool, and only
then are the first 100 selected.

- `m_minus_1_gmv_usd` and `mtd_gmv_usd` are summed separately with `Decimal` from the exact Phase
  4 text.
- `ranking_gmv_usd` is their sum.
- Order: `ranking_gmv_usd` descending, `mtd_gmv_usd` descending, `target_id` ascending.
- No marketplace price is summed or averaged.
- If fewer than 100 qualify, the actual count is published and the shortfall reported.

**Exclusion reasons** (ledger and exclusion report):

| Reason | Meaning |
|---|---|
| `SOURCE_NOT_ELIGIBLE` | The source is not `ELIGIBLE` or not present in MTD. |
| `UNMAPPED` | No approved `SOURCE_MODEL_MAP` row. |
| `TARGET_INACTIVE` | The mapped target is inactive. |
| `TARGET_LISTING_ONLY` | The mapped target is `LISTING_ONLY`. |
| `TARGET_CORE_INCOMPLETE` | The mapped target's core identity is incomplete. |
| `TARGET_CPU_MODEL_CONFLICT` | The target's `cpu_model` names another CPU family than its `cpu_series`. |
| `KNOWN_UNRESOLVED_CONFLICT` | An `OPEN` register entry exists for the source or the target. |
| `NO_COMPLETE_REVIEW` | The source has no `COMPLETE` review row. |
| `REVIEW_STATUS_UNRESOLVED` | The source also has open review rows. |
| `REVIEW_CORE_DIFFERS` | The review row and the target differ on a core field. |
| `REVIEW_CODE_DIFFERS` | The review row and the target record different manufacturer codes. |
| `REVIEW_MODE_MISMATCH` | The review row's tracking mode is not `CONFIGURATION`. |

**Identity review register.** `config/identity_review_register.csv` is local and Git-ignored;
its header is `config/identity_review_register_template.csv`. It holds known conflicts that
structured fields cannot show, for example a variation name stating 32GB while the review row
records 16GB.

- Columns: `source_model_id,target_id,field,recorded_value,stated_value,status,note,recorded_at`.
- `status` is `OPEN` or `RESOLVED`.
- An `OPEN` row excludes its source, or its target when only `target_id` is given.

**Outputs** (`data/output/verified_top_100/`; local and Git-ignored, because they contain sales
data):

| File | Columns |
|---|---|
| `verified_configuration_ranking.csv`, `top_100_verified_configurations.csv` | `rank,target_id,brand,series,model,screen_size_in,cpu_series,ram_capacity_gb,storage_capacity_gb,m_minus_1_gmv_usd,mtd_gmv_usd,ranking_gmv_usd,contributing_source_count,contributing_source_model_ids,original_source_top_100_contributors,excluded_mapped_source_count,sales_run_id,sales_extract_date,sales_m_minus_1_month,sales_mtd_month,target_master_sha256` |
| `verified_configuration_contributions.csv` (one row per eligible source) | `source_rank,model_id,shop_id,item_id,item_name,shopee_model_name,m_minus_1_gmv_usd,mtd_gmv_usd,ranking_gmv_usd,in_original_source_top_100,target_id,contribution_status,reason_code,reason_detail` |
| `verified_configuration_exclusions.csv` | `scope,model_id,target_id,reason_code,reason_detail,ranking_gmv_usd,in_original_source_top_100` (`scope` is `SOURCE` or `TARGET`) |
| `verified_configuration_review_priority.csv` | `priority,scope,model_id,target_id,shop_id,item_id,item_name,shopee_model_name,ranking_gmv_usd,mtd_gmv_usd,in_original_source_top_100,review_statuses,review_row_numbers,missing_core_fields,known_register_notes,suggested_action` |
| `verified_configuration_run_summary.json` | Sales snapshot and period labels, input SHA-256 values, counts, and output paths. |

**Review-priority queue.** The queue lists, by source GMV:
- eligible unmapped sources;
- mapped sources excluded for identity reasons; and
- `OPEN` register entries outside the snapshot.

It reports structured facts only: review statuses, row numbers, and core fields blank in every
review row. It never infers specifications, promotes candidates, or creates mappings.

**Audit.** `audit_verified_top_100.py` checks a published run against the eligible ranking and
the catalog:
- target IDs are unique, with ranks 1..n;
- each target is ACTIVE `CONFIGURATION`, with a complete core identity matching TARGET_MASTER;
- every contributor is eligible, present in MTD, mapped to its target, and carries the
  ranking's GMV text;
- totals equal the contributors' sums;
- the order is deterministic, and the top list is the head of the full ranking;
- the catalog has the same SHA-256 the run recorded.

The source-level `audit_top_100_coverage.py` is unchanged.

First run (2026-10-12): the sales snapshot is Phase 4 run `20260928T112328+0800`, with M-1 2026-08 (full month) and MTD 2026-09 (month-to-date, extracted 2026-09-28); it is historical and was not refreshed. The catalog has 204 targets (192 `CONFIGURATION`, 12 `LISTING_ONLY`) and 359 mappings. Results: 1,379 eligible sources; 244 mapped; 221 contributing; 1,158 excluded (1,135 unmapped, 12 `LISTING_ONLY`, 5 on CPU-conflict targets, 6 with mapped codes differing from their target); 127 qualifying unique configurations (39 with several sources); 100 selected; no shortfall; audit passed.

Published run (2026-10-12, after the MSI and register reconciliation): same sales snapshot. The catalog has 205 targets (193 `CONFIGURATION`, 12 `LISTING_ONLY`) and 360 mappings (TARGET_MASTER SHA-256 `fbe29cb5…`). Results: 1,379 eligible sources; 222 contributing; 1,157 excluded (1,134 unmapped, 12 `LISTING_ONLY`, 5 on CPU-conflict targets, 6 with differing codes); 128 qualifying configurations (39 with several sources); 100 selected; no shortfall; 0 `OPEN` register entries; audit passed. The owner published the 100 to the `P1_TOP_100_CONFIGURATIONS` tab (header row 10, data A11:U110). This list is the working scope for Phase 9 comparisons; the full catalog stays the reusable source.

## Remaining work

Completed on 2026-10-06: local validators and templates match the five-field rule; core-key duplicate detection; the live sheet's `COMPLETE` dropdown, target consolidation, promotions, and mappings (user-reported); the Top 100 coverage audit; and the read-only Phase 8 handoff. Completed locally on 2026-10-08: tracking modes in templates, validators, mapping checks, matching, and the coverage audit.

Still open (human decisions and sheet work, not software):

- (Done 2026-10-08: tracking-mode headers migrated; the read-only checks pass.)
- Work the verified-configuration review-priority queue from the highest GMV down. Promote only candidates that become complete and unambiguous under their tracking mode, reusing an existing `target_id` when the five-field key already exists. Classify a candidate `LISTING_ONLY` only with recorded evidence that no verifiable model exists; a blank model is not enough. Record rejections with reviewer and date. The four parked original Top 100 IDs are backlog in this queue, not required members of the working list.
- Resolve the conflicts that currently exclude contributions: the 5 `TARGET_CPU_MODEL_CONFLICT` targets and the 6 `REVIEW_CODE_DIFFERS` sources. (The two register entries were resolved on 2026-10-12.)
- (The READY `model`/`cpu_series` proposals were applied on 2026-10-08.) Decide the unresolved items listed in [SHEET_MIGRATION_MODEL_CPU_CONVENTIONS.md](SHEET_MIGRATION_MODEL_CPU_CONVENTIONS.md). The validator checks formats, not these conventions; `propose_catalog_conventions.py` reports them.
- Decide whether sibling variation IDs outside the Top 100 also need a coverage report; the current audit covers only the accepted Top 100 and `COMPLETE` review rows.
