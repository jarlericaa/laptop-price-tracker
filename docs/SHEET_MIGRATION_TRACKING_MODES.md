# Sheet Migration: Tracking Modes (2026-10-08)

These instructions are for an assistant (ChatGPT) helping the project owner edit the master Google Sheet. They add the approved `tracking_mode` columns to `TARGET_MASTER` and `MATCH_REVIEW`. The repository's validators already accept both the old and the new headers. Nothing in this repository edits the Sheet.

**Status (2026-10-08):** Applied by the owner. `MATCH_REVIEW` is now read by header name, so its live column order may differ from the order below; the column set must still match exactly. The read-only checks passed on 2026-10-08 (see [PROGRESS_LOG.md](PROGRESS_LOG.md)). The counts in section 4 are from before the migration.

## Rules for this migration

- Work on the master spreadsheet only after making a backup copy of the whole file (File → Make a copy) and noting its name and date.
- Only **append** columns to the right of `notes`. Do not rename, reorder, insert between, or delete any existing column.
- Do not change any existing cell value, `target_id`, `status`, or `SOURCE_MODEL_MAP` row. Do not delete rows.
- Do not touch the `raw` tab or `P1_TOP_100_TARGETS`.
- Do not classify any existing row as `LISTING_ONLY` during the migration. Leaving the new cells blank keeps every existing row a `CONFIGURATION` row.
- Never type `Generic`, `Unbranded`, `No Brand`, `OEM`, `N/A`, `None`, `Unknown`, `No model`, or `-` into `model`. The validator rejects these.
- Do not classify a product by its country of manufacture, and do not treat a blank `model` as proof that a product is `LISTING_ONLY`.

## 1. `TARGET_MASTER`

The current last column is `AH` (`notes`). Add these six headers in row 1, in this order:

| Column | Header |
|---|---|
| AI | `tracking_mode` |
| AJ | `shop_id` |
| AK | `item_id` |
| AL | `source_model_id` |
| AM | `tracking_mode_reason` |
| AN | `classification_evidence` |

The complete header row afterwards must be exactly (comma-separated, no spaces):

```text
target_id,status,brand,model,screen_size_in,resolution,display_type,refresh_rate_hz,cpu_brand,cpu_series,cpu_generation,cpu_cores,ram_capacity_gb,ram_type,storage_capacity_gb,storage_type,storage_layout,gpu_type,gpu_brand,gpu_model,vram,os,battery_capacity,build_material,manufacturer_model_code,series,cpu_model,color,variant_notes,manufacturer_part_number,source_url,reviewer,reviewed_at,notes,tracking_mode,shop_id,item_id,source_model_id,tracking_mode_reason,classification_evidence
```

Then:

1. Select columns `AJ:AL` (`shop_id`, `item_id`, `source_model_id`) and set **Format → Number → Plain text** *before* any value is entered. Shopee IDs must stay exact text: leading zeroes kept, no scientific notation.
2. Select `AI2:AI` and add **Data → Data validation** with a dropdown:
   - Criteria: dropdown with the two items `CONFIGURATION` and `LISTING_ONLY` (exact uppercase).
   - Invalid data: **Reject the input**.
   - Leave blank cells allowed; a blank means `CONFIGURATION`.
3. Leave `AI:AN` blank on all 168 existing rows. (Optionally, the owner may set `AI` to `CONFIGURATION` on existing rows; this is equivalent to blank. Do not fill `AJ:AN` on them: a `CONFIGURATION` target with listing IDs fails validation.)

## 2. `MATCH_REVIEW`

The current last column is `AL` (`notes`). `MATCH_REVIEW` already has `source_model_id` in column `C`, so add only these five headers in row 1, in this order:

| Column | Header |
|---|---|
| AM | `tracking_mode` |
| AN | `shop_id` |
| AO | `item_id` |
| AP | `tracking_mode_reason` |
| AQ | `classification_evidence` |

The complete header row afterwards must be exactly:

```text
review_id,status,source_model_id,candidate_name,brand,model,screen_size_in,resolution,display_type,refresh_rate_hz,cpu_brand,cpu_series,cpu_generation,cpu_cores,ram_capacity_gb,ram_type,storage_capacity_gb,storage_type,storage_layout,gpu_type,gpu_brand,gpu_model,vram,os,battery_capacity,build_material,manufacturer_model_code,series,cpu_model,color,variant_notes,manufacturer_part_number,source_url,review_reason,prepared_by,reviewer,reviewed_at,notes,tracking_mode,shop_id,item_id,tracking_mode_reason,classification_evidence
```

Then:

1. Set `AN:AO` (`shop_id`, `item_id`) to **Plain text** before entering values. Column `C` (`source_model_id`) should already be plain text; check that it is.
2. Add the same `tracking_mode` dropdown to `AM2:AM` (`CONFIGURATION`, `LISTING_ONLY`; reject invalid input; blanks allowed).
3. Keep the existing `status` dropdown (`NEW`, `INCOMPLETE`, `COMPLETE`, `REVIEW_REQUIRED`, `REJECTED`) unchanged.
4. Leave `AM:AQ` blank on all 5,154 existing rows. The 326 `COMPLETE` rows stay valid as `CONFIGURATION` candidates. `shop_id`/`item_id` may be filled later on any candidate as trace evidence.

## 3. `SOURCE_MODEL_MAP`

No change. The header stays exactly:

```text
source_model_id,target_id,reviewer,reviewed_at,notes
```

## 4. Verify

After editing, the project owner runs (read-only):

```bash
python check_target_tables.py --from-sheets
python audit_top_100_coverage.py --from-sheets
```

Expected: all three tabs pass with unchanged row counts (`TARGET_MASTER` 168, `SOURCE_MODEL_MAP` 326, `MATCH_REVIEW` 5,154 as of 2026-10-06, unless rows were added since). The coverage audit should still show 63 `MAPPED_CONFIGURATION`, 0 `MAPPED_LISTING_ONLY`, and the same unresolved candidates. If a header error appears, compare row 1 character by character with the lines above. The usual causes are a trailing space, a missing underscore, or a column inserted in the wrong place.

## 5. Recording a `LISTING_ONLY` product later (after migration)

Use this only when research **confirms** that the product has no verifiable product model name or code:

1. In `MATCH_REVIEW`, on the candidate's row:
   - Set `tracking_mode` = `LISTING_ONLY`.
   - Fill `shop_id` and `item_id` from the accepted Top 100 / Shopee listing, exactly as text. `source_model_id` is the Shopee variation `model_id` and is already in column `C`.
   - Fill `tracking_mode_reason` (why no model exists) and `classification_evidence` (what was checked: title, variation names, seller specs, packaging or label photos, manufacturer search).
   - Keep `model` and `manufacturer_model_code` blank. Record other specifications only if they are evidenced.
   - Set `status` = `COMPLETE` only when all of the above are filled. If evidence conflicts, use `REVIEW_REQUIRED`.
2. Promote it by adding a `TARGET_MASTER` row:
   - A new `target_id` chosen by the owner, `status` `ACTIVE`, and `tracking_mode` `LISTING_ONLY`.
   - The same `shop_id`, `item_id`, `source_model_id`, reason, and evidence.
   - Blank `model` and `manufacturer_model_code`.
3. Add one `SOURCE_MODEL_MAP` row linking that same `source_model_id` to the new `target_id`. No other source ID may link to a `LISTING_ONLY` target.
4. Rerun the two verification commands.

`LISTING_ONLY` targets are tracked as that individual listing only. The retailer matcher never uses them for cross-retailer matching.
