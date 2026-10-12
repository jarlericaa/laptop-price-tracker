# Sheet Update: `model` and `cpu_series` Conventions (2026-10-08)

These instructions are for an assistant (ChatGPT) helping the project owner edit `TARGET_MASTER`
in the master Google Sheet. They apply the approved conventions in
[TARGET_MASTER_SCHEMA.md](TARGET_MASTER_SCHEMA.md#model-and-cpu-series-conventions-2026-10-08).
Nothing in this repository edits the Sheet. The values to enter come from a local, Git-ignored
report, `data/output/catalog_conventions/SHEET_CORRECTION_REPORT.md`. This document contains no
catalog values.

**Status:** Not applied. The report lists *proposals*, not authoritative values. Apply them only
after the owner approves them.

## 1. Produce the report (owner, read-only)

```bash
python check_target_tables.py --from-sheets
python audit_top_100_coverage.py --from-sheets
python propose_catalog_conventions.py \
  --target-master data/output/target_master_validated.csv \
  --source-model-map data/output/source_model_map_validated.csv \
  --match-review data/output/match_review_validated.csv \
  --top-100-coverage data/output/top_100_coverage.csv \
  --observations data/output/pilot_matching_2026-10-08/observations.csv
```

The first two commands must pass. The third refuses to run on a snapshot that failed validation.
`--observations` is optional; it adds retailer evidence. Give the assistant
`SHEET_CORRECTION_REPORT.md`. The full per-target table is in
`catalog_convention_proposals.csv` in the same folder.

## 2. Rules for the edit

- Make a backup copy of the whole spreadsheet first (File → Make a copy). Note its name and
  date.
- Edit only `TARGET_MASTER`, and only the cells named in report sections 1 and 2, on the row
  whose `target_id` is listed. Find rows by `target_id`, never by row number.
- Do not change `target_id`, `status`, `tracking_mode`, `screen_size_in`, `ram_capacity_gb`,
  `storage_capacity_gb`, `cpu_model`, `manufacturer_model_code`, or any `LISTING_ONLY` row. Do not
  touch `SOURCE_MODEL_MAP`, `MATCH_REVIEW`, `raw`, or `P1_TOP_100_TARGETS`. Do not add or delete
  rows.
- If report section 4 (duplicate core keys) is not "None.", stop. The owner must resolve it first.
- Do not apply anything from sections 3, 5, or 6. Those items need owner decisions (see step 4).

### Section 1: `model` and `series`

For each listed `target_id`:

1. Set `model` to the "set `model` to" value exactly as written. Keep its letter case, every
   hyphen, and every suffix (for example a trailing `WSM` or `W`). Add no brand, family name, or
   color.
2. Set `series` to the "set `series` to" value exactly as written. This preserves the former
   model wording (the product-family name).
3. Append the "append to `notes`" text to the end of the existing `notes`, separated by a space.
   Do not replace existing notes.

Before typing, set the `model` column to **Format → Number → Plain text**. This stops a code such
as `14-ep0445TU` being reinterpreted. After typing, check that each cell shows the code exactly.

### Section 2: `cpu_series`

For each listed `target_id`, set `cpu_series` to the "set `cpu_series` to" value, for example
`Apple M5`. Leave `cpu_model` unchanged; it keeps the original processor wording.

## 3. Verify (owner, read-only)

1. Rerun `check_target_tables.py --from-sheets` and `audit_top_100_coverage.py --from-sheets`.
   Row counts and coverage counts should be unchanged. A `DUPLICATE_*` error means a section-1 or
   section-2 edit collided with another target; restore that row from the backup.
2. Rerun `propose_catalog_conventions.py` (step 1). Every applied item should now be
   `NO_CHANGE`.
3. Replay the saved pilot observations against the corrected catalog:

   ```bash
   python match_offers.py --target-master data/output/target_master_validated.csv \
     --observations data/output/pilot_matching_2026-10-08/observations.csv \
     --output-dir data/output/pilot_matching_2026-10-08/replay_corrected_authoritative
   ```

   Only this replay, against the corrected authoritative catalog, can confirm the pilot outcome.
   Diagnostic replays against the local proposed copy do not.

## 4. Items the owner decides (do not edit from this document)

- **`MAPPED_LISTINGS_RECORD_OTHER_CODES`:** listings recorded under a different manufacturer code
  are mapped to the same target. Under the code convention they are different products. The
  owner decides one of two outcomes:
  - the review record's code is wrong, so correct the review record; or
  - the listing is a different product, so it needs its own target (a new `target_id` chosen by
    the owner) and a `SOURCE_MODEL_MAP` change.

  Until then, the target keeps its current `model`.
- **`MANUFACTURER_MODEL_CODE_IS_NOT_A_CODE`:** the code field holds a name (for example a
  ThinkPad line name). Record a real code only from authoritative evidence. Otherwise leave the
  model name as it is.
- **`CPU_MODEL_DISAGREES_WITH_CPU_SERIES`:** for example `cpu_model` `Intel i7` with
  `cpu_series` `Core i5`. Check the source evidence and correct whichever field is wrong.
- **`RETAILER_CORE_VALUE_DIFFERS`:** a retailer listing with the same code states a different core
  value. This is a prompt to check the catalog's own source. It is not proof that the catalog is
  wrong.
- **`NO_VERIFIED_MODEL_CODE`:** the verified model name stays. Unverified code hints taken from
  listing text must be confirmed against manufacturer or official retailer specifications before
  any code is recorded.
