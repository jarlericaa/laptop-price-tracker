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
| Comparable | `EXACT` match (the five-field rule and its secondary-conflict safeguard unchanged) to a selected target, `IN_STOCK` or `OUT_OF_STOCK`, checked `bundle_flag` (`TRUE` or `FALSE`), currency PHP, price type known, retailer one of the five codes. Confirmed bundles participate with a `BUNDLE_OFFER` note (owner approval, 2026-10-15). A blank `bundle_flag` (not checked) is not comparable. |
| Not comparable | Preorder, unknown availability, unknown bundle status, other currencies, unknown price type, `UNMATCHED`, `AMBIGUOUS`, and `REVIEW_REQUIRED` observations stay in the store and in history with their status and reason codes. They never supply a target price. |
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

### Retailer links for rechecking

Each weekly comparison price is followed by its retailer product link (`DBZ_Offer_URL`,
`LF_Offer_URL`, `PCX_Offer_URL`, `PCC_Offer_URL`, `OCT_Offer_URL`). If a price is selected,
its link points to the exact offer that supplied that price. If no price is comparable but
an exact matched offer was observed in that report week, the link points to the latest such
offer. Out-of-stock displayed prices are now comparable under the owner policy below;
excluded offers retain a blank comparison price and their existing status.
No link is assigned from an uncertain candidate or carried forward from an older week.
Full URLs, including selected retailer variant parameters, are preserved. Historical/review
files retain their original `offer_url` columns. The CSV stores literal URLs for Sheets import;
no formula is needed to recheck the product page.

### Pilot Sheets tabs (`publish_prices.py`, Phase 10, manual)

Published from reviewed reports for the authorized pilot scope only. Every publication replaces
the tabs' content. Text is literal (never a formula), IDs are text, prices are numbers formatted
as PHP, and links are cell links on the full URL.

| Tab | One row per | Columns |
|---|---|---|
| `PILOT_PRICE_COMPARISON` | Pilot configuration (verified-list order) | Rank, target ID, brand, series, model, screen (in), CPU family, RAM (GB), storage (GB). For each retailer (DataBlitz, Laptop Factory, PC Express, PC Central, Octagon): price (PHP), product link, status, observed (Asia/Singapore). Then the lowest observed price and retailer(s), the lowest in-stock price and retailer(s), comparable retailers, Shopee sales snapshot extract date, reporting week, week status, and report cutoff (Asia/Singapore). Publisher `pilot-sheets-2` (2026-10-15); a tab still carrying the `pilot-sheets-1` header is recognized as its own. `pilot-sheets-5` (2026-10-20) adds the five Shopee snapshot columns after the storage column and names the minimums *retailer* minimums ("Lowest retailer price (PHP)", "Lowest in-stock retailer price (PHP)", …); the `pilot-sheets-2` to `-4` headers are recognized and migrated. |
| `PILOT_PRICE_SUMMARY` | Pilot configuration (the comparison's order and scope) | Exactly seven visible columns: `Laptop` (brand and model · screen · CPU family · installed RAM/storage, extended only where needed to tell configurations apart), `Shopee`, `DataBlitz`, `Laptop Factory`, `PC Express`, `PC Central`, `Octagon`. Every price is the comparison's own cell (Shopee: the snapshot price; retailers: the selected price), as a PHP number; a blank stays blank. Context is in cell notes; listed-spec prices are italic. Publisher `pilot-sheets-5` (2026-10-20); see below. |
| `PILOT_PRICE_HISTORY` | Observation of a pilot target, or a non-exact observation naming a pilot candidate | Week, retrieval time (Asia/Singapore and UTC), rank, target ID (exact matches only), candidate target(s) (non-exact only), model, retailer, product link (exact only), observed page (non-exact, unlinked), observed and regular price, price type, availability, bundle flag, match status and reasons, outcome, comparison status and reasons, change against the previous comparable observation, latest-for-offer flag, observation ID. |
| `PILOT_PRICE_REVIEW` | Pilot review item | Category (`Matching problem` or `Matched, excluded from comparison`), review reason, details, week, retrieval time, rank and target ID (exact only) or candidates, retailer, product link or unconfirmed page, observed price, availability, match and comparison statuses and reasons, observation ID. |
| `PILOT_PRICE_RUN_LOG` | Publication (keyed by publication ID; a repeat updates its row and attempt count) | Status, first and last publication time, reporting week and status, report cutoff, collection start and finish, scope and outcome counts, Shopee sales-period label and run ID, tabs written, readback result, notes, report folder, selected-list, catalog, matching-context, input, and published-content fingerprints, publisher version. From `pilot-sheets-5`: summary rows, Shopee snapshot prices and the Shopee snapshot extract date, appended after the publisher version (earlier rows keep them blank). |

Comparison status text:
- `Comparable price` (`SELECTED`); when the match was not plain labeled specifications, the
  basis follows, for example `Comparable price; matched configuration (model from title, other
  specs from the page)`. "Matched configuration" never claims an identical factory SKU.
- `Matched, not comparable: <reasons>` (`NO_COMPARABLE_OFFER`, for example unknown bundle status)
- `Needs match review (no price)` (`REVIEW_REQUIRED_ONLY`)
- `Not observed this week` (`NO_OBSERVATION_IN_WEEK`)

Price cells without a comparable price have no value at all.

### Observation store (`data/history/price_history.sqlite3`, Git-ignored)

- **Identity.** `observation_id` = SHA-256 of `retailer_id`, `offer_url`, `offer_index`, and `retrieved_at` (UTC, microseconds). `offer_key` = SHA-256 of the first three. Importing the same record again is a duplicate and is counted, not stored. A later retrieval is a new observation even when nothing else changed. The same identity with different content (price, availability, bundle flag, page hash, and so on) is a **conflict**: the whole batch is refused and nothing from it is written.
- **Batches.** Each import is validated completely (every row through `observation_from_record`/`OfferObservation`, round-trip checked) and then written in one SQLite transaction; any error rolls it back. `import_batches` and `batch_observations` record every import, including all-duplicate reruns.
- **Stored fields.** Retailer, offer URL, offer index, retailer product ID and SKU, retrieval time, price, currency, price type, regular and sale price, availability, bundle flag, adapter version, page SHA-256, and the full `to_full_record()` JSON (raw specifications, categories, availability and bundle evidence).
- **Matching contexts.** A context is the `TARGET_MASTER` SHA-256 plus the matching-rule version (`five-field-exact/balanced-evidence/2026-10-15`; earlier `five-field-exact/cpu-family/2026-10-12`) and a SHA-256 of the rule modules (`normalization`, `conventions`, `evidence_approvals` with its approval CSV, `extraction`, `matching`). Each observation is matched once per context; results (status, target, candidates, reason codes, extractor, extracted attributes, field evidence) are never replaced. A changed catalog or rule set creates a new context, and earlier results are kept.
- **Direct use by a collector.** `persist_scrape_results(store, scrape_results, imported_at=...)` stores the observations from production `scrape()` results as one batch and returns the number of `ScrapeFailure` records, which are not stored.

### Report files (`data/output/price_tracking/week_<YYYY-MM-DD>/`, Git-ignored)

| File | Rows | Contents |
|---|---|---|
| `weekly_price_comparison.csv` | One per selected configuration, including those with no price | Week fields, `rank`, `target_id`, the requested product fields, Shopee source summary, `Currency`, `DBZ_Price`, `LF_Price`, `PCX_Price`, `PCC_Price`, `OCT_Price`, each followed by its `<code>_Offer_URL`, a `<code>_Price_Status` per retailer, then per retailer `<code>_Stock`, `<code>_Observed_At`, `<code>_Match_Basis`, `<code>_Warnings`, `<code>_Reason` (2026-10-15), `Comparable_Retailer_Count`, `Lowest_Price` (lowest *observed*), `Lowest_Price_Retailers`, `Lowest_Price_Offer_URL`, `Lowest_Price_Observed_At`, `Lowest_Price_Stock`, `Lowest_In_Stock_Price`, `Lowest_In_Stock_Price_Retailers`, `Lowest_In_Stock_Offer_URL`, `Lowest_In_Stock_Observed_At`, `Shopee_Sales_Extract_Date`, matching context, and selected-list SHA-256. |
| `weekly_price_selections.csv` | One per selected configuration and retailer | The chosen observation's price, price type, regular/sale price, offer URL and index, retailer product ID/SKU, retrieval time (UTC and Asia/Singapore), observation ID, offer key, selection reason, comparable/tied/observed offer counts, not-comparable reason counts, review-required count, stale reference, adapter version, and page SHA-256; since 2026-10-15 also `availability`, `match_basis`, `match_warnings`, the lowest comparable in-stock offer (`in_stock_price`, `in_stock_offer_url`, `in_stock_retrieved_at_utc`, `in_stock_observation_id`), and the latest exact offer (`latest_exact_offer_url`, `latest_exact_availability`, `latest_exact_retrieved_at_utc`). |
| `weekly_price_series.csv` | One per selected configuration, retailer, and week in the history range | Weekly price status and price, representative offer, previous week's status and price, change, change status, and `representative_offer_changed`. |
| `price_history.csv` | One per stored observation in the history window | Offer identity, retrieval time, report week, prices, availability, bundle flag, match status and reasons, selected-list membership, comparability status and reasons, `latest_for_offer_in_week`, previous comparable observation and price change, adapter version, page SHA-256, import batch, matching context, catalog SHA-256, and rule version. |
| `price_review_queue.csv` | One per report-week observation that did not give a selected price | `MATCH_<status>`, `EXACT_TARGET_NOT_IN_SELECTED_LIST`, or `NOT_COMPARABLE`, with reasons. |
| `configuration_source_ledger.csv` | One per selected configuration and contributing Shopee source | `model_id`, `shop_id`, `item_id`, names, GMV (USD) per period, whether the source is still mapped to the target, and the Shopee snapshot price (PHP) with its explicit sales-period label. From 2026-10-20 also `shopee_price_role` (`SELECTED`, `TIED_NOT_SHOWN`, `HIGHER_PRICE`, `NOT_ELIGIBLE: <reason>`) and `shopee_listing_url`; every listing keeps its row. |
| `shopee_price_selection.csv` | One per selected configuration (2026-10-20) | `rank`, `target_id`, `shopee_price_status`, the selected snapshot price (PHP), link, variation, item name, `model_id`, `shop_id`, `item_id`, eligible and priced source counts, distinct prices, `tied_model_ids`, excluded sources by reason, the explanation, the sales run, extract date and period label, and the rule. |
| `price_tracking_run_summary.json` | One | Week window, status and label, `as_of`, policy, sales-snapshot labels, input SHA-256 values, selected-list and catalog fingerprints, matching context, store counts before/after, import results, and output counts. |

**Requested fields.** Product fields map to `TARGET_MASTER` columns: `Brand`→`brand`, `Model`→`model`, `Screen Size`→`screen_size_in` (inches), `Resolution`, `Display Type`, `Refresh Rate` (Hz), `CPU_Brand`, `CPU_Series`, `CPU_Generation`, `CPU_Cores`, `RAM_Capacity` (GB), `RAM_Type`, `Storage_Capacity` (GB), `Storage_Type`, `GPU Type`, `GPU_Brand`, `GPU_Model`, `VRAM`, `OS`, `Battery_Capacity`, `Build_Material`. `Series` is added as readable context. Values are the catalog's exact text; blank stays blank.

**Shopee fields.** A configuration can have several Shopee sources, so `Item_ID`, `Model_ID`, `Shop_ID`, and the per-listing `Current_Price` are not collapsed into one listing in the ledger. From 2026-10-20 the comparison also shows one dated snapshot price per configuration, chosen by a fixed rule (see "Shopee snapshot prices" below). The comparison row carries `Shopee_Source_Count`, all `Shopee_Source_Model_IDs`, and `Shopee_Total_GMV_USD` (M-1 + MTD of the contributing sources). Each listing's IDs, GMV, and snapshot price are in `configuration_source_ledger.csv`. These values come from the Phase 4 sales snapshot run `20260928T112328+0800`: M-1 2026-08 (full month) and MTD 2026-09 as extracted on 2026-09-28. They are historical and are not current October marketplace prices.

### Unassigned pilot observations (2026-10-13)

Pilot publication retains non-exact observations whose collection manifest expectation belongs
to the authorized scope, including prior records for that retailer/full URL/offer-index identity.
This scopes evidence only: target IDs remain blank until an exact match exists. Candidate-based
review scoping also remains supported. Unknown matches cannot supply comparison prices or
confirmed product links. The comparison status identifies absence of a confirmed matched offer,
while history, review and run-log counts retain the actual collected review observations.

## Out-of-stock displayed-price comparison (owner approval, 2026-10-13)

Matched OUT_OF_STOCK offers with a valid displayed PHP price now participate in price selection,
lowest-price calculations, history and price-change calculations. Exact identity, scope,
checked bundle status and known price-type rules still apply. Confirmed bundles became eligible
on 2026-10-15 (below). PREORDER/UNKNOWN availability stay excluded.
A price is never invented when absent, and an earlier week's price is never carried forward.

The comparison status reads `Comparable price (OUT OF STOCK)` for the selected sold-out offer;
history retains `Availability = OUT_OF_STOCK` and `Matched, comparable (OUT OF STOCK)`.
Availability is looked up using the selected observation ID, so an in-stock alternative cannot
hide the sold-out status of a cheaper selected price. The CSV schemas and pilot tab headers stay
compatible. Run summaries record `displayed-price/in-and-out-of-stock/2026-10-13` as the comparison
policy version. Earlier report folders preserve the prior in-stock-only policy.

## Lowest observed vs lowest in-stock, and explained blanks (2026-10-15)

- `Lowest_Price` is unchanged in value and is labeled **lowest observed price**: the minimum of
  the per-retailer selections, out-of-stock included. Its retailer(s), link(s), observation time(s)
  and stock state(s) are reported beside it; ties keep every tied retailer.
- `Lowest_In_Stock_Price` is calculated from the same comparable observations, week, cutoff and
  latest-per-offer rule, filtered to `IN_STOCK`, with the same tie-break. It is blank when no
  eligible in-stock offer exists; an out-of-stock price is never relabeled as in stock. A retailer
  whose cheapest comparable offer is sold out can still supply the in-stock minimum through
  another in-stock offer.
- `<code>_Reason` explains each cell: `PRICE_SHOWN_WHILE_OUT_OF_STOCK`, `EXACT_PRICE_EXCLUDED:
  <reason counts>` (for example `BUNDLE_STATUS_UNKNOWN:1`), `MATCH_REVIEW_REQUIRED: <n> observation(s)`,
  `NO_EXACT_OFFER_THIS_WEEK`, optionally with the last comparable price and its real date
  (never carried forward). A missing price is blank, never zero.
- `<code>_Match_Basis` and `<code>_Warnings` come from the stored reason codes
  (`TITLE_MODEL_CORROBORATED`, `OWNER_APPROVED_INTERPRETATION`, `SELECTED_VARIANT`,
  `SHARED_PRODUCT`; warnings such as `CPU_NUMBER_TITLE_DIFFERS`, `MPN_REPEATS_RETAILER_SKU`).
- Comparison cells only see matched observations. Discovery and collection stages (search not
  executed, no candidate, candidate not fetched, fetch failed, identity conflict, different
  configuration) are reported per configuration and retailer by `coverage_report.py`
  (`coverage.csv`, `coverage_offers.csv`); without search logs the status is
  `UNKNOWN_COVERAGE`, and a product not found is never reported as out of stock.
- Readers of saved reports (`publish_prices.py`) require only the pre-2026-10-15 columns, so
  earlier report folders still load; their added cells are blank.
- Confirmed bundles participate with a `BUNDLE_OFFER` note (owner approval below); an
  unchecked package stays excluded as `BUNDLE_STATUS_UNKNOWN`. Included Office/software and warranties are not
  bundle triggers. The Shopee GMV and snapshot price aggregation is unchanged and remains the
  historical 2026-09-28 extract shown in `Shopee_Sales_Extract_Date`; it is never mixed into
  retailer minimums.

## Bundle prices included with a note (2026-10-15)

Owner-approved confirmed bundles (`bundle_flag = TRUE`) participate at the displayed package
price, including lowest observed, lowest in-stock and historical price changes. Accessory value
is not inferred or deducted. Bundle detection and original evidence remain intact. Unchecked
bundle status remains excluded (`BUNDLE_STATUS_UNKNOWN`).

- `<code>_Warnings` adds `BUNDLE_OFFER` as a price note alongside any identity warnings.
- Selections add `price_notes` and `in_stock_price_notes`; `match_warnings` remains identity-only.
- `Lowest_Price_Notes` and `Lowest_In_Stock_Notes` identify bundled winning retailers as
  `<code>:BUNDLE_OFFER`, preserving all tied retailers and the actual in-stock alternative.
- The Sheet's retailer status includes "bundle offer". A bundled winning retailer is labeled
  "(bundle offer)" beside the corresponding minimum, including an in-stock winner different
  from that retailer's lowest observed offer. The tab headers remain compatible.
- History keeps `bundle_flag`; its comparable outcome also states "bundle offer".
- Coverage offer details add `bundle_flag` and `price_notes`; `best_offer_warnings` includes the
  bundle note. Bundle status alone no longer produces `EXACT_PRICE_EXCLUDED`.
- Regenerated summaries record `displayed-price/stock-and-bundles/2026-10-15`; existing report
  folders retain their earlier policy. Rebuild price/coverage reports before preview/publication;
  publishing alone does not recalculate prices.

## Purchase terms, unconfirmed candidates, and the coverage ledger (2026-10-19)

**Purchase terms** (`offer_terms.py`; `track_prices.py --offer-terms-policy`). Bundle keywords
(`FREE`, `BUNDLE`, `BACKPACK`, `Office`) no longer decide eligibility on their own. The offer's own
wording (title, price text, inclusion/promo specification rows, description, adapter bundle
evidence) is classified:

| Category | Default policy `purchase-terms/2026-10-19` | `bundle-flag/2026-10-15` |
|---|---|---|
| `STANDARD_OFFER` | comparable | comparable |
| `INCLUDED_ITEMS` (stated free/included Office, warranty, backpack, mouse, ...) | comparable, note `INCLUDED_ITEMS_OFFER` | comparable, `BUNDLE_OFFER` if flagged |
| `PAID_PACKAGE` (`+ 24" Monitor`, non-ordinary bundle components) | excluded `PAID_PACKAGE_PRICE` | comparable, `BUNDLE_OFFER` |
| `CONDITIONAL_PURCHASE` (the price needs another purchase) | excluded `PURCHASE_CONDITION_REQUIRED` | by bundle flag |
| `RESTRICTED_PRICE` (financing/installment-only, card, trade-in, member) | excluded `RESTRICTED_PRICE` | by bundle flag |
| `PURCHASE_TERMS_UNKNOWN` (bundle wording without stated terms) | excluded `PURCHASE_TERMS_UNKNOWN` | comparable, `BUNDLE_OFFER` |
| `BUNDLE_NOT_CHECKED` (adapter did not check) | excluded `BUNDLE_STATUS_UNKNOWN` | excluded `BUNDLE_STATUS_UNKNOWN` |

A freebie that needs another purchase ("free sleeve with purchase of Microsoft 365") does not
restrict the laptop price; it is recorded as a conditional extra and is not an inclusion.
Payment options mentioned in prose ("0% installment available") do not restrict a price. Stock
eligibility and the separate lowest-observed / lowest-in-stock minimums are unchanged. History
and review rows add `offer_terms_category`, `offer_terms_evidence` (the deciding wording),
`price_notes`, and `search_context_target_ids`. The run summary's `offer_terms_effect` lists,
for this week's exact matches with identity held fixed, the comparable count under each policy
and every observation whose eligibility depends on the policy: the commercial-terms effect,
reported separately from matching changes.

**Unconfirmed candidates beside a blank price.** `track_prices.py --search-context
<collection>/match_results.csv` (repeatable) links observations to the configuration they were
collected for. A cell with no exact offer but a related offer (search context or a near-match
candidate) has status `CANDIDATE_NOT_CONFIRMED` and new columns `<code>_Candidate_URL`,
`<code>_Candidate_Status` (`MODEL_UNVERIFIED`, `EXTRACTION_INCOMPLETE`, `IDENTITY_CONFLICT`,
`SAME_MODEL_DIFFERENT_CONFIGURATION`, `MATCHED_OTHER_CONFIGURATION`, ...) and
`<code>_Candidate_Detail` (blocking codes and differing values, e.g. `confirmed as TGT-000219
(outside the selected list); ram_capacity_gb: offer 8 vs target 16`). Selections add
`candidate_offer_url`, `candidate_status`, `candidate_detail`, `candidate_observation_id`. The
candidate's price is never shown in the comparison and never enters a minimum.

**Coverage ledger** (`coverage_report.py`). Statuses add `SAME_MODEL_DIFFERENT_CONFIGURATION`,
`SEARCH_FAILED` and `CONFIRMED_OUTSIDE_SCOPE`; searches that were never sent (limits, budget)
are `DISCOVERY_NOT_ATTEMPTED`, not `NO_CANDIDATE_FOUND`. New columns: `in_reporting_scope`,
`current_reason`, `candidate_url`, `candidate_outcome`, `differing_fields`, `confirmed_price`,
`unconfirmed_candidate_price`, `searches_failed`, `searches_not_sent`, `search_stop_reason`,
`current_offers`, `historical_observations`. `best_offer_price` now holds a confirmed price only.
Offer details add `is_current`, `price_role` (`CONFIRMED` / `UNCONFIRMED_NOT_COMPARED`),
`differing_fields`, `offer_terms_category`, and relation `SEARCHED_FOR` beside `MATCHED`,
`CANDIDATE` and `COLLECTED_FOR` (search context is never an assignment).

## Matching basis and listed-spec prices (2026-10-20)

Decision record: [DECISIONS.md](DECISIONS.md#listed-spec-comparison-for-broad-family-targets-2026-10-20).

- `weekly_price_comparison.csv`:
  - per retailer, `<code>_Match_Basis` is `LISTED_SPEC` for a listed-spec price, and the new
    `<code>_Offer_Details` holds the chosen offer's own model, processor, GPU, panel and
    resolution;
  - per configuration, `Comparison_Basis`, `Lowest_Price_Basis` and `Lowest_In_Stock_Basis`
    are `EXACT_PRODUCT`, `LISTED_SPEC` or `MIXED`;
  - `Lowest_Price_Label` and `Lowest_In_Stock_Label` say "... matching listed specs" whenever a
    listed-spec price is among those compared.
- `weekly_price_selections.csv`: `comparison_basis`, `offer_details`.
- `price_history.csv`: `listed_spec_status`, `listed_spec_target_ids`,
  `listed_spec_ambiguous_target_ids`, `listed_spec_comparability`,
  `listed_spec_comparability_reasons` (the stored match columns are unchanged).
- `price_review_queue.csv`: `LISTED_SPEC_MATCH <target> (...)` rows show each listed-spec
  association with the offer's details; `LISTED_SPEC_AMBIGUOUS (...)` rows show offers that fit
  several targets equally.
- Blank cells whose only offer is a listed-spec match with an excluded price read
  `LISTED_SPEC_PRICE_EXCLUDED: <reasons>`.
- `coverage.csv`: `LISTED_SPEC_PRICE_AVAILABLE`, `LISTED_SPEC_PRICE_EXCLUDED`, `price_basis`.
- The matching-rules version is `five-field-exact+listed-spec-fallback/2026-10-20`, and
  `listed_spec.py` is part of the rules fingerprint.

## Shopee snapshot prices and `PILOT_PRICE_SUMMARY` (2026-10-20)

Decision record: [DECISIONS.md](DECISIONS.md#shopee-snapshot-prices-and-the-price-summary-tab-2026-10-20).

**Selection rule** (`shopee_snapshot.py`): the *lowest eligible mapped-listing snapshot price*.

- Eligible listings are the configuration's contributing Shopee sources (the selected list's
  contribution ledger) that `SOURCE_MODEL_MAP` still maps to the target
  (`currently_mapped_to_target` is `True`).
- A valid price has price status `PRICE_OK` and is a finite PHP amount greater than zero.
  `PRICE_CONFLICT` (several month-to-date prices), blank, zero, negative or unreadable prices,
  and a missing sales ranking are never used, guessed or averaged.
- The lowest valid price is shown. On a tie the listing with the higher ranking GMV (USD) is
  shown, then the lower `model_id`; all tied `model_id`s are recorded.
- Price, link, variation and IDs come from that one ledger row. The link is
  `https://shopee.ph/product/<shop_id>/<item_id>`, built only from numeric IDs. It opens the
  product page and does **not** select the variation, so the variation and `model_id` are shown
  separately.
- Configuration statuses: `SNAPSHOT_PRICE`, `NO_MAPPED_SOURCE`, `NO_VALID_SNAPSHOT_PRICE`,
  `SALES_RANKING_NOT_SUPPLIED`. In the comparison, `NO_CONTRIBUTING_SOURCE` and
  `SHOPEE_SNAPSHOT_NOT_SUPPLIED` also appear. A blank price always has a status and an
  explanation.

**`weekly_price_comparison.csv`** appends `Shopee_Price` (PHP, two decimals),
`Shopee_Listing_URL`, `Shopee_Variation`, `Shopee_Item_Name`, `Shopee_Model_ID`,
`Shopee_Shop_ID`, `Shopee_Item_ID`, `Shopee_Price_Status`, `Shopee_Price_Detail`,
`Shopee_Snapshot_Date` (the extract date, e.g. `2026-09-28`) and `Shopee_Snapshot_Label` (sales
run and period).

- The Shopee price is a historical snapshot. It is never a current price, its stock is not
  verified, and it is never part of `Lowest_Price`, `Lowest_In_Stock_Price` or
  `Comparable_Retailer_Count`, which stay retailer-only.
- `price_tracking_run_summary.json` has a `shopee_snapshot_prices` section: the rule, extract
  date, run ID, counts by status, ties, several-price configurations and listing roles.

**Publisher (`pilot-sheets-5`).**

- **`PILOT_PRICE_COMPARISON`.** After "Storage (GB)" come "Shopee price (PHP, snapshot)" (PHP
  number), "Shopee listing link" (cell link), "Shopee variation" (name and `model_id`), "Shopee
  price status" (rule or reason, plus "not a current price; stock not verified; the link opens
  the product page, not the variation"), and "Shopee snapshot date" ("2026-09-28 (historical
  Shopee sales extract; sales run …)").
  - The minimum headers read "Lowest retailer price …" and "Lowest in-stock retailer price …".
  - A report written before 2026-10-20 still publishes. Its Shopee cells say "Shopee snapshot
    not in this report (generated before 2026-10-20)".
  - Validation recomputes the selection from the report's `configuration_source_ledger.csv`
    (fingerprinted) and refuses a price, `model_id`, link or date that disagrees.
- **`PILOT_PRICE_SUMMARY`.** A minimal grid with seven columns: Laptop, Shopee, DataBlitz,
  Laptop Factory, PC Express, PC Central, Octagon.
  - **Rows.** One per configuration, in the comparison's order and scope.
  - **Prices.** Every cell is the comparison's own cell. `build_plan` refuses the publication
    if any summary price differs from the comparison in value, kind or format. Nothing is
    reselected or rematched. No cheapest-price column mixes the Shopee snapshot with retailer
    observations.
  - **Cell notes.**
    - Shopee: "HISTORICAL SHOPEE SNAPSHOT — not a current price; stock not verified", the
      extract date and sales run, the rule and any tie, the listing, the variation with
      `model_id`, `shop_id` and `item_id`, and the link.
    - Retailer price: exact product or "LISTED-SPEC MATCH — not an exact product match" with
      the offer's own details, stock (out-of-stock prices kept under the owner policy), bundle
      offers, disagreement warnings, other warnings in words, observation time and offer link.
    - Blank: the comparison's explanation and the first recheck link (unconfirmed, not a
      price) with "+N more in PILOT_PRICE_REVIEW".
  - **Header notes.** They explain the Shopee snapshot basis and the italic listed-spec
    convention, and point to PILOT_PRICE_COMPARISON and PILOT_PRICE_REVIEW for the evidence.
  - **Layout.**
    - The header row and the Laptop column are frozen.
    - The Laptop column is 340 px and wraps; the six price columns are 125 px and right-aligned.
    - The header is shaded, and data rows have subtle banding (every second row).
    - The layout is reset on the whole tab at each publication, so a shorter publication
      leaves no stale bands.
  - **Notes on this tab only.** Notes and italics are written, cleared and read back on this
    tab only. The other tabs keep notes that people add.
- **Run log.** "Summary rows", "Shopee snapshot prices" and "Shopee snapshot extract date" are
  appended. A run log with the `pilot-sheets-4` header is still the publisher's.
- **Plan files.** Preview and write folders hold `plan_PILOT_PRICE_SUMMARY.csv` (values) and
  `plan_PILOT_PRICE_SUMMARY_notes.csv` (cell, column, row label, value, italic flag and note).
  The publication report counts `planned_cell_notes`.
