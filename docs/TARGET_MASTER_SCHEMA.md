# TARGET_MASTER Schema (Proposed)

**Status:** PROPOSED, pending business approval. The local validator (`src/laptop_tracker/target_master.py`) and check command (`check_target_master.py`) implement this proposal so it can be tested with synthetic data. No real configurations have been defined or populated, and the real `TARGET_MASTER` worksheet is still empty and unchanged.

## What the existing docs already establish

These points come from [the PRD](PRD.md), [the architecture](ARCHITECTURE.md), [the phase plan](IMPLEMENTATION_PHASES.md), and [the decision log](DECISIONS.md). They are not new choices.

- `TARGET_MASTER` holds validated laptop configurations that later retailer comparisons use as target identity. Later matching must use reviewed configurations, not unstructured listing titles.
- Phase 5 validates required fields, brand, model/series, CPU, memory, storage, display, and relevant variants "as agreed", and runs duplicate-configuration checks.
- Retailer matching must be exact. Similar names can hide CPU, memory, storage, or other differences, and ambiguous or partial matches must go to review rather than being accepted.
- Downstream code must be able to use the table without fuzzy identity assumptions.
- The project keeps identifiers and text as exact strings, with no numeric coercion.
- Sheets are read-only by default, and conflicts are reported for human review rather than resolved automatically.

The docs do **not** set column names, which fields are mandatory, units, allowed values, or the uniqueness key. Everything below is therefore a proposal.

## Proposed columns

The header order matches `config/target_master_template.csv`. A header row must include every required column, and headers are exact and case-sensitive. Optional columns can be left out and are then treated as blank. Unknown, blank, or repeated headers stop the check, so a misspelled column is caught instead of being silently ignored.

| Column | Required | Proposed format | In identity key |
|---|---|---|---|
| `target_id` | Yes | 1–64 characters from letters, digits, `.`, `_`, `-`; starts with a letter or digit. Must be unique ignoring case. | No (own uniqueness rule) |
| `status` | Yes | `ACTIVE` or `INACTIVE` (uppercase) | No |
| `brand` | Yes | Text | Yes |
| `series` | No | Text | Yes |
| `model` | Yes | Text | Yes |
| `cpu` | Yes | Text, as the manufacturer names it | Yes |
| `ram_gb` | Yes | Positive integer without leading zeros (for example, `16`) | Yes |
| `storage_gb` | Yes | Positive integer without leading zeros (total storage) | Yes |
| `storage_type` | No | Text (for example, `SSD`) | Yes |
| `display_size_in` | Yes | Positive number with up to 2 decimals (`14`, `15.6`), using a `.` separator | Yes |
| `display_resolution` | No | `WIDTHxHEIGHT` with a lowercase `x` (for example, `1920x1080`) | Yes |
| `gpu` | No | Text | Yes |
| `os` | No | Text | No |
| `color` | No | Text | No |
| `variant_notes` | No | Free text | No |
| `manufacturer_part_number` | No | Text | No |
| `source_model_id` | No | Digits only; the Shopee `model_id` from the reviewed Phase 4 targets | No |
| `reviewer` | Yes | Text | No |
| `reviewed_at` | Yes | Valid `YYYY-MM-DD` date | No |
| `notes` | No | Free text | No |

## Validation behavior (as implemented)

- **Exact strings.** Cells are read with Python's `csv` module, so no type inference happens. Values such as `0007`, `000012345678901234`, `1.10`, and `1E+05` keep their exact text. Nothing is trimmed or rewritten, and the validated copy contains the original strings.
- **Missing values.** A blank required field is reported as `MISSING_VALUE`. A blank optional field is allowed. A populated optional field is still format-checked.
- **Malformed values.** Values are checked with `INVALID_FORMAT` (pattern), `INVALID_VALUE` (allowed set), and `INVALID_DATE`. Leading or trailing whitespace outside free-text columns is reported as `SURROUNDING_WHITESPACE` instead of being trimmed silently.
- **Row shape.** Fully blank rows are ignored. Non-blank cells past the last header are reported as `EXTRA_CELLS`. A table with headers but no rows is reported as `NO_DATA_ROWS` and does not pass.
- **Duplicate `target_id`.** Matching ignores case. Every row in the group is flagged `DUPLICATE_TARGET_ID`.
- **Duplicate configuration.** Rows are compared on the proposed identity key above. The comparison ignores case and collapses whitespace, but only for the comparison; stored values are unchanged. A blank optional key value counts as its own value, so a blank `storage_type` does not match `SSD`. Every row in the group is flagged `DUPLICATE_CONFIGURATION`, and no row is picked over another. `ACTIVE` and `INACTIVE` rows are compared together.
- **Any error excludes the row.** A row with any error is left out of `valid_rows`. The whole check passes only when there are no errors and at least one row.
- **Configurable identity key.** The identity key can be changed through `identity_columns`. This makes an approved change a one-line edit, and a test covers it.

## Local check path

```bash
python check_target_master.py path/to/target_master_export.csv
```

- The command reads a local CSV only. It uses no Google credentials and makes no network calls.
- It always writes `data/output/target_master_validation_errors.csv`, which is ignored by Git and can contain cell values.
- Only when every row passes, it writes `data/output/target_master_validated.csv` with exact strings and source row numbers. It deletes an older validated copy first, so a failed check never leaves stale "valid" output behind.
- The console shows only counts and error codes, never row values.
- Exit code `0` means the check passed; `1` means it failed or the headers were invalid.

The validator takes worksheet-shaped values with the header row first. The same function could later validate a read-only `get_all_values()` result, but that sheet-reading step has not been built. Pipeline consumption of `TARGET_MASTER` is also not built yet.

## Schema decisions needing business approval

1. **Identity key.** Is `(brand, series, model, cpu, ram_gb, storage_gb, storage_type, display_size_in, display_resolution, gpu)` the correct definition of one exact configuration? Should `color`, `os`, `manufacturer_part_number`, or refresh rate or panel type be part of identity?
2. **Mandatory fields.** Should `series`, `storage_type`, `display_resolution`, or `gpu` be required? Should `model` stay required when a series name already identifies the product?
3. **`target_id` convention.** Who assigns it, what format it uses (the pattern above is permissive), and whether IDs may ever be reused.
4. **Link to Phase 4.** Should `source_model_id` be required? May one Shopee `model_id` map to more than one target configuration, or one configuration to several model IDs? No uniqueness rule is enforced on it today.
5. **Units and conventions.** Is 1 TB recorded as `1024` or `1000` in `storage_gb`? How are dual-drive or unified-memory configurations recorded? How exact must CPU strings be (for example, suffixes and generation), and is a CPU normalization list needed?
6. **Allowed values.** Should `storage_type`, `os`, `brand`, or `gpu` be limited to controlled vocabularies? Should `status` have values beyond `ACTIVE` and `INACTIVE`?
7. **Uniqueness scope.** Should `INACTIVE` rows take part in identity uniqueness? Currently they do.
8. **Case sensitivity.** Is case-insensitive duplicate detection the correct choice for both `target_id` and configuration identity?
9. **Review fields.** Are `reviewer` and `reviewed_at` required for every row, as proposed to match the duplicate-decision convention?
10. **Source of truth and workflow.** Is the worksheet the master copy, with local CSV exports used for checking, or the other way around? Who approves additions, and how are rows removed?
