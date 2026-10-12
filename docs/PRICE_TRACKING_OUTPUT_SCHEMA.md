# Price Tracking Output Fields

**Current status (2026-10-12):** Phase 9 is implemented as an **offline** path (`track_prices.py`, `observation_store.py`, `price_tracking.py`). It is verified on synthetic tests and on the nine saved Laptop Factory observations from 2026-10-08. The implemented policy, storage, and output files are in [Phase 9 implementation](#phase-9-implementation-2026-10-12) below. The earlier sections are the original requirements and are kept as history.

**Original status:** Output fields were recorded on 2026-09-29. On 2026-10-05, the user changed the planned matching priority to five core fields plus lower-priority supporting specifications. This supersedes the earlier ten-field exact-key requirement below. Comparison outputs are not implemented yet. The local target validators were synchronized with the five-field policy on 2026-10-06.

## Requested fields

| Group | Fields | Type hint supplied | Notes |
|---|---|---|---|
| Shopee listing identity | `Item_ID`, `Model_ID`, `Shop_ID` | `longint` | Source listing identifiers. |
| Shopee metrics | `Total_GMV`, `Current_Price` | Not specified | Preserve source values; units and currency still need to be documented. |
| Product identity | `Brand`, `Model` | `str` | `Model` is a core matching field; `Brand` is useful context. |
| Display | `Screen Size`, `Resolution`, `Display Type`, `Refresh Rate` | Not specified | `Screen Size` is core. Resolution, display type, and refresh rate are lower-priority supporting fields. |
| CPU | `CPU_Brand`, `CPU_Series`, `CPU_Generation`, `CPU_Cores` | Not specified | `CPU_Series` is core. Other CPU details are supporting fields. |
| Memory | `RAM_Capacity`, `RAM_Type` | `RAM_Capacity`: `int`; `RAM_Type`: representation to confirm | `RAM_Capacity` is core; RAM type is supporting detail. |
| Storage | `Storage_Capacity`, `Storage_Type` | `Storage_Capacity`: `int`; `Storage_Type`: representation to confirm | `Storage_Capacity` is core; storage type is supporting detail. |
| Graphics | `GPU Type`, `GPU_Brand`, `GPU_Model`, `VRAM` | Not specified | GPU details are lower-priority supporting fields. |
| Other product details | `OS`, `Battery_Capacity`, `Build_Material` | Not specified | OS, battery capacity, and build material are supporting details. |
| Retailer prices | `DBZ_Price`, `LF_Price`, `PCX_Price`, `PCC_Price`, `OCT_Price` | `float` | One price field per retailer code. Define retailer names and currency handling before implementation. |

## Matching priority approved 2026-10-05

The five core fields are:

```text
Model
Screen Size
CPU_Series
RAM_Capacity
Storage_Capacity
```

The master column names are `model`, `screen_size_in`, `cpu_series`, `ram_capacity_gb`, and `storage_capacity_gb`. A candidate is core-complete when all five fields have explicit, unambiguous values. The user's rule authorizes promotion of such candidates to `TARGET_MASTER`; missing `resolution`, `display_type`, `refresh_rate_hz`, `gpu_model`, `os`, or `source_url` does not block promotion. Keep those details when known. `brand` remains useful context but is not part of the five-field core key.

Phase 8 matching must first require agreement on the five core fields after documented normalization. A missing core value is not equal to another missing value and cannot form a core match. Available secondary fields can strengthen confidence. Missing secondary values do not block a core match. Explicit secondary conflicts must be surfaced as lower-confidence/review cases rather than silently overwritten or treated as proof of a different core identity. Do not introduce fuzzy matching for the five core fields.

## Relationship to the Phase 5 target schema

This is a requested downstream output schema, separate from the Phase 6 `OfferObservation` model. The planned Phase 5 target identity uses the five-field core key above. `manufacturer_model_code` and `cpu_model` remain optional evidence fields; they do not determine whether two target rows share a core identity. Keep all requested detail fields in outputs even when they are not part of the core key.

**Implementation status (2026-10-06):** `src/laptop_tracker/target_master.py` and the `MATCH_REVIEW` validator now enforce the five-field key and accept `COMPLETE`. Phase 8 offline matching applies the same key; see [MATCHING.md](MATCHING.md). The Phase 9 comparison outputs above are not implemented. *(Superseded 2026-10-12: implemented offline; see below.)*

Phase 9 must also define retailer-code names, currencies, units, numeric precision, and missing-price representation before generating comparison files. The retailer price fields are requested as floats; the Phase 6 scraper currently stores observed prices as `Decimal`, so conversion should happen only at a deliberately defined output boundary.

## Phase 9 implementation (2026-10-12)

This is an **offline** implementation. It reads saved observations, the validated catalog snapshots, and the published working list. It makes no retailer requests, never reads `raw` or Google Sheets, and never writes Sheets, targets, or mappings. Broader live-pilot acceptance and automation are later work (Phases 10–11).

### Policy (starting rules supplied 2026-10-12)

| Topic | Rule |
|---|---|
| Scope | The selected `target_id` list in `top_100_verified_configurations.csv` (100 of 128 qualifying). Matching still uses the full validated catalog, so EXACT offers for other targets are kept and labelled out of scope. `LISTING_ONLY` targets are never matched across retailers. |
| Currency | PHP. No conversion. Shopee GMV stays USD in its own columns and is never combined with retailer prices. |
| Precision | Prices are stored and written as the observation's exact decimal text (for example `45999.00`). Differences are computed with `Decimal`. The requested `float` type hint applies only if a consumer converts the text at its own boundary. |
| Price basis | The trusted displayed price: the observation's `price` with a `REGULAR` or `SALE` `price_type`. Regular and sale prices are kept beside it. Conditional vouchers and shipping are not observed; they stay unknown and are never added or subtracted. |
| Comparable | `EXACT` match (the five-field rule and its secondary-conflict safeguard unchanged) to a selected target, `IN_STOCK`, `bundle_flag` `FALSE`, currency PHP, price type known, retailer one of the five codes. A blank `bundle_flag` (not checked) is not comparable. |
| Not comparable | Out of stock, preorder, unknown availability, bundles, other currencies, unknown price type, `UNMATCHED`, `AMBIGUOUS`, and `REVIEW_REQUIRED` observations stay in the store and in history with their status and reason codes. They never supply a target price. |
| Missing price | Blank cell plus a status code (`NO_OBSERVATION_IN_WEEK`, `NO_COMPARABLE_OFFER`, `REVIEW_REQUIRED_ONLY`). Never zero. |
| Week | Monday 00:00 to the next Monday 00:00 in `Asia/Singapore` (end exclusive). `--week-start` must be a Monday. A week is `CLOSED_HISTORICAL` when `--as-of` is at or after its end, otherwise `IN_PROGRESS` (provisional). |
| Cut-off | Every run takes an explicit `--as-of` time with a UTC offset. Observations retrieved after it, or after the report week, are ignored by that run, so a rerun with the same dates reproduces the same reports. |
| Representative observation | Each offer (`retailer_id` + `offer_url` + `offer_index`) is represented in a week by its **latest** observation in that week. If that latest observation is not comparable, the offer gives no price that week, even if it was comparable earlier in the week. |
| Selection | For each selected target and retailer: the lowest comparable price among the representative observations. Ties go to the latest retrieval, then offer URL, then offer index, then observation ID. Prices are never averaged. |
| Stale observations | Never carried forward. An earlier week's price is shown only as `stale_last_comparable_*` reference columns in the selections file. |
| Price changes | Per observation: only between consecutive **comparable** observations of the same offer and target (`FIRST_COMPARABLE`, `UNCHANGED`, `INCREASED`, `DECREASED`; others `NOT_CALCULATED`). Per week: only between consecutive weeks that both have a selected price; a week without one is `NO_PRICE`, and the next priced week is `NO_PREVIOUS_WEEK_PRICE`. `representative_offer_changed` shows when the selected offer differs from the previous week's. |
| Retention | The store is append-only (SQLite triggers refuse `UPDATE`/`DELETE`). Observations, import batches, matching contexts, and match results are kept indefinitely; no automatic pruning exists. Reports are regenerated from the store on each run. |

### Retailer codes

| Column prefix | Retailer | `retailer_id` |
|---|---|---|
| `DBZ` | DataBlitz | `datablitz` |
| `LF` | Laptop Factory | `laptop_factory` |
| `PCX` | PC Express | `pcx` |
| `PCC` | PC Central | `pcc` |
| `OCT` | Octagon | `oct` |

### Observation store (`data/history/price_history.sqlite3`, Git-ignored)

- **Identity.** `observation_id` = SHA-256 of `retailer_id`, `offer_url`, `offer_index`, and `retrieved_at` (UTC, microseconds). `offer_key` = SHA-256 of the first three. Importing the same record again is a duplicate and is counted, not stored. A later retrieval is a new observation even when nothing else changed. The same identity with different content (price, availability, bundle flag, page hash, and so on) is a **conflict**: the whole batch is refused and nothing from it is written.
- **Batches.** Each import is validated completely (every row through `observation_from_record`/`OfferObservation`, round-trip checked) and then written in one SQLite transaction; any error rolls it back. `import_batches` and `batch_observations` record every import, including all-duplicate reruns.
- **Stored fields.** Retailer, offer URL, offer index, retailer product ID and SKU, retrieval time, price, currency, price type, regular and sale price, availability, bundle flag, adapter version, page SHA-256, and the full `to_full_record()` JSON (raw specifications, categories, availability and bundle evidence).
- **Matching contexts.** A context is the `TARGET_MASTER` SHA-256 plus the matching-rule version (`five-field-exact/cpu-family/2026-10-12`) and a SHA-256 of the rule modules (`normalization`, `conventions`, `extraction`, `matching`). Each observation is matched once per context; results (status, target, candidates, reason codes, extractor, extracted attributes, field evidence) are never replaced. A changed catalog or rule set creates a new context, and earlier results are kept.
- **Direct use by a collector.** `persist_scrape_results(store, scrape_results, imported_at=...)` stores the observations from production `scrape()` results as one batch and returns the number of `ScrapeFailure` records, which are not stored.

### Report files (`data/output/price_tracking/week_<YYYY-MM-DD>/`, Git-ignored)

| File | Rows | Contents |
|---|---|---|
| `weekly_price_comparison.csv` | One per selected configuration, including those with no price | Week fields, `rank`, `target_id`, the requested product fields, Shopee source summary, `Currency`, `DBZ_Price`, `LF_Price`, `PCX_Price`, `PCC_Price`, `OCT_Price`, a `<code>_Price_Status` per retailer, `Comparable_Retailer_Count`, `Lowest_Price`, `Lowest_Price_Retailers`, matching context, and selected-list SHA-256. |
| `weekly_price_selections.csv` | One per selected configuration and retailer | The chosen observation's price, price type, regular/sale price, offer URL and index, retailer product ID/SKU, retrieval time (UTC and Asia/Singapore), observation ID, offer key, selection reason, comparable/tied/observed offer counts, not-comparable reason counts, review-required count, stale reference, adapter version, and page SHA-256. |
| `weekly_price_series.csv` | One per selected configuration, retailer, and week in the history range | Weekly price status and price, representative offer, previous week's status and price, change, change status, and `representative_offer_changed`. |
| `price_history.csv` | One per stored observation in the history window | Offer identity, retrieval time, report week, prices, availability, bundle flag, match status and reasons, selected-list membership, comparability status and reasons, `latest_for_offer_in_week`, previous comparable observation and price change, adapter version, page SHA-256, import batch, matching context, catalog SHA-256, and rule version. |
| `price_review_queue.csv` | One per report-week observation that did not give a selected price | `MATCH_<status>`, `EXACT_TARGET_NOT_IN_SELECTED_LIST`, or `NOT_COMPARABLE`, with reasons. |
| `configuration_source_ledger.csv` | One per selected configuration and contributing Shopee source | `model_id`, `shop_id`, `item_id`, names, GMV (USD) per period, whether the source is still mapped to the target, and the Shopee snapshot price (PHP) with its explicit sales-period label. |
| `price_tracking_run_summary.json` | One | Week window, status and label, `as_of`, policy, sales-snapshot labels, input SHA-256 values, selected-list and catalog fingerprints, matching context, store counts before/after, import results, and output counts. |

**Requested fields.** Product fields map to `TARGET_MASTER` columns: `Brand`→`brand`, `Model`→`model`, `Screen Size`→`screen_size_in` (inches), `Resolution`, `Display Type`, `Refresh Rate` (Hz), `CPU_Brand`, `CPU_Series`, `CPU_Generation`, `CPU_Cores`, `RAM_Capacity` (GB), `RAM_Type`, `Storage_Capacity` (GB), `Storage_Type`, `GPU Type`, `GPU_Brand`, `GPU_Model`, `VRAM`, `OS`, `Battery_Capacity`, `Build_Material`. `Series` is added as readable context. Values are the catalog's exact text; blank stays blank.

**Shopee fields.** A configuration can have several Shopee sources, so `Item_ID`, `Model_ID`, `Shop_ID`, and the per-listing `Current_Price` are not collapsed into one listing. The comparison row carries `Shopee_Source_Count`, all `Shopee_Source_Model_IDs`, and `Shopee_Total_GMV_USD` (M-1 + MTD of the contributing sources). Each listing's IDs, GMV, and snapshot price are in `configuration_source_ledger.csv`. These values come from the Phase 4 sales snapshot run `20260928T112328+0800`: M-1 2026-08 (full month) and MTD 2026-09 as extracted on 2026-09-28. They are historical and are not current October marketplace prices.
