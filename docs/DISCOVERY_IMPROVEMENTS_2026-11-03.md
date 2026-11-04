# Discovery: distinct outcomes, routes, leads and targeted recovery (2026-11-03)

This builds on [DISCOVERY_IMPROVEMENTS_2026-10-22.md](DISCOVERY_IMPROVEMENTS_2026-10-22.md). Nothing
here changed matching, the catalog, the history store, Google Sheets, or the shared cooldowns,
`Retry-After` handling, request budgets, persistent progress or cache freshness. No retailer request
was made while writing it: every check below ran offline, against fakes or the saved evidence of the
2026-11-03 weekly runs.

## 1. What the saved 2026-11-03 run showed

Run `20261103T062304Z` (200 jobs, 311 requests, 4 candidate-step errors):

- **A crash lost the PC Central candidates of four targets.** `select_and_inspect` indexed a
  `VariantEvaluation` dataclass as a tuple (`variant_evaluation(...)[:2]`), raising `TypeError` for a
  single-variant page with named options (PC Central, Octagon). `STEP_FAILED` rows in
  `product_inspections.csv` name TGT-000289, TGT-000095, TGT-000293 and TGT-000027 (PC Central). The
  pages had been fetched and saved; their candidates never reached `candidates.csv`. Two of them are
  owner-listed misses (ASUS X1504VA-BQ4301WSM, Acer AG14-71M-58JD). Fixed, and a failed step now
  keeps its candidates (decision `STEP_FAILED`).
- **`QUERY_PLAN_EXHAUSTED` hid seven different endings.** A saved-data reclassification (the same
  rule the command now applies; relevance approximated from the saved categories) of the 177 jobs:

  | New terminal outcome | Jobs |
  |---|---:|
  | `SEARCH_INCOMPLETE_RESULT_CAP` (predictive search returned its ten and stopped) | 89 |
  | `RESULTS_ALL_REJECTED` | 30 |
  | `NO_RESULTS_RETURNED` | 29 |
  | `PLAUSIBLE_CANDIDATE_NOT_COLLECTED` (relevant, over the per-retailer limit) | 14 |
  | `PAGE_FETCHED_IDENTITY_UNRESOLVED` | 12 (+1 from a stop at a plausible candidate) |
  | `DISCOVERY_STEP_FAILED` | 3 (+1) |

  The 21 `SUITABLE_OFFER_FOUND` jobs split into 15 `SUITABLE_OFFER_FOUND` and 6
  `SUITABLE_OFFER_EXCLUDED_BY_STOCK` (identity confirmed, every confirmed offer out of stock).
- **Non-laptops reached the candidate step.** Of 738 distinct saved results, 43 were components,
  memory, SSDs, graphics cards, phones, a tablet, a power bank or a chair that ranked as
  `UNRELATED` or even `DIFFERENT_MODEL_CODE` (a motherboard whose "(AM4)" cut its title head before
  the noun). A Laptop Factory `Lenovo Yoga Tab` and a DataBlitz motherboard were *collected* in
  earlier runs.
- **Predictive search is capped and ordered.** Across the saved runs, 33 of 669 distinct
  predictive queries mixed in-stock and unavailable products; in 28 the unavailable ones came
  strictly last. With a ten-result cap a sold-out product can fall out of the answer. That is
  consistent with (not proof of) the PC Express MacBook Neo and DataBlitz MacBook Air misses:
  "Apple MacBook Neo" returned ten unrelated "Neo" laptops.
- **Inspected pages were downloaded twice.** The 24 product pages discovery fetched while
  inspecting candidates were fetched again by the collector (`requests_reused: {}` in both rounds).

## 2. The discovery sequence (audit)

| Step | Existed | Added 2026-11-03 |
|---|---|---|
| 1. Refresh known URLs | `--seed-store`/`--seed-urls` (EXACT results), `--supplied-urls`, catalog leads; each re-fetched and re-matched | Every lead keeps its variant (section 3); `candidates.csv` gets `lead_status` and `lead_variants`; a known URL is a lead, never a match |
| 2. Exact model/part codes | complete code, verified part number, spaced spelling, distinctive segment | `compact` (`15fc0933AU`) and, for a spaced catalog value, `hyphenated` spellings; en dash and other dash characters count as punctuation; `--max-identifier-variant-queries` default 3 |
| 3. Controlled family/name queries | code+specs, brand+series, family+specs, brand+CPU model, name, name+specs | `2_PRODUCT_NAME_CPU` (name and processor, no capacities: titles often omit RAM, and a narrow query stays under the cap) |
| 4. Another public route | catalog-index fallback (`--max-fallback-requests-per-retailer`) | Shopify product-only storefront search (`--max-storefront-search-requests`, default 0, off); retailer-restricted web search as a plan only (`--web-search-plan`) |
| 5. Pagination | Laptop Factory `--max-search-pages` | Reported per request (`completeness`), per job, and as a terminal outcome; storefront route paginates inside the same limit |

No specification is added to every query: capacities appear only in the later fallback tiers.

## 3. Variants are never folded together

`lead_url_key(url)` is the de-duplication identity of a lead: host case, trailing slash, fragment
and tracking parameters (`utm_*`, `srsltid`, `_pos`, ...) are presentation; every other parameter,
`variant` above all, is identity. `strip_variant` stays the product-page identity (one inspection
serves every variant). Applied to:

- historical seeds and supplied URLs (`RankedCandidate.lead_urls`; two variants of one product are
  two leads of one candidate, and a multi-variant page collects each lead variant it still lists;
  a variant it no longer lists is noted, nothing is assumed);
- the collection manifest (`add_manifest_rows`: a variant URL is its own request, the same URL with
  other tracking is one);
- search hits (`SearchHit.variant_id`, `unique_hits`).

A lead is revalidated through inspect, collect, match like any candidate. `lead_status` is
`LEAD_REVALIDATE_REQUIRED` for seeds and supplied URLs; the expected target ID in the manifest is a
hint the collector checks (`expected_target_check`).

## 4. Shopify routes

DataBlitz, PC Express, PC Central and Octagon are Shopify (product pages at `/products/<handle>`);
Laptop Factory is WooCommerce.

- **Predictive search** (`/search/suggest.json`): at most ten products, no pages, no variants. It is
  never read as exhaustive: ten results are `INCOMPLETE_RESULT_CAP`, fewer are
  `COMPLETE_FOR_QUERY` (a relevance list, not a catalog scan). Requests now send
  `resources[options][unavailable_products]=last` explicitly (the observed default: unavailable
  products after the available ones); `--unavailable-products show` keeps them in relevance order.
  Responses saved before the option existed still answer the `last` search and say so; `show` does
  not use them. The cap and the ordering are why unavailable products drop out; neither option
  removes the cap.
- **Stock is classified apart from relevance.** An unavailable result keeps its category (and the
  flag `SEARCH_SAYS_UNAVAILABLE`), is collected like any candidate, and its stock comes from the
  page's adapter. Stock never decides identity.
- **Storefront search page** (`/search?q=..&type=product`): opt-in second route, run after every
  native tier found nothing plausible, within `--max-storefront-search-requests` (also counted in
  `--max-search-requests`; its next pages count too). DataBlitz renders that page in the browser
  (no result links): the response is `UNSUPPORTED_LAYOUT`, never "no results".
- **Retailer-restricted web search**: not sent. `--web-search-plan` writes `web_search_plan.csv`
  (`site:<host> "<code>"` and its spellings, or the name with its processor) for a person; URLs
  found go into a supplied-URL file and are revalidated.

## 5. Non-laptop and accessory results

Rejected in `rank_hit` (category `NOT_A_LAPTOP` or `ACCESSORY`, decision `REJECTED_PRODUCT_TYPE` or
`REJECTED_ACCESSORY`), before ranking order, plausibility and any page request, so they take no
candidate slot and cannot stop a search:

- a retailer product type naming a component, memory, storage, graphics, phone, tablet, desktop,
  monitor, router, console, printer or chair category (authoritative unless it names a laptop);
- title phrases only such products use (`Motherboard`, `Graphics Card`, `Solid State Drive`,
  `microSDXC`, `Yoga Tab`, `Galaxy A..`, `GDDR`, ...) anywhere in the title;
- a `DIMM`/`SODIMM` listing that states no processor or screen (a laptop title may mention RAM);
- a URL handle naming a component (a laptop handle ending `-backpack` is untouched);
- power banks join the accessory nouns.

Tests use real titles from `candidates.csv`; a saved-data test checks that no earlier rejection is
lost and that only the Yoga Tab and the motherboard were collected before.

## 6. Terminal outcomes (final vocabulary)

`search_jobs.csv` (and `discovery_summary.json` `job_outcomes`, one entry per target x retailer)
carry `terminal_outcome`, `outcome_class`, `outcome_detail`, `result_classes` (what the results
were, counted by category), `rejection_reasons` (counted), `search_completeness` and
`search_routes`. `discovery_summary.json` also has `terminal_outcomes.by_terminal_outcome` and
`.by_outcome_class`. The column `discovery_outcome` keeps the earlier vocabulary unchanged
(`QUERY_PLAN_EXHAUSTED` is the umbrella that `terminal_outcome` refines); a diagnostics script
should group by `terminal_outcome` or `outcome_class`.

| # | Class (`outcome_class`) | `terminal_outcome` | Meaning |
|---|---|---|---|
| - | `0_SUITABLE_OFFER` | `SUITABLE_OFFER_FOUND` (existing) | Identity confirmed on a page in stock on ordinary terms |
| 1 | `1_NO_RESULTS_RETURNED` | `NO_RESULTS_RETURNED` | Every attempted query was answered, none listed a product |
| 2 | `2_RESULTS_REJECTED` | `RESULTS_ALL_REJECTED` | Results found, all rejected (`rejection_reasons`: `NOT_A_LAPTOP`, `ACCESSORY`, `DIFFERENT_MODEL_CODE`, `UNRELATED`, `TOO_MANY_TITLE_DIFFERENCES`, `PAGE_<other configuration>`, ...) |
| 3 | `3_SEARCH_INCOMPLETE` | `SEARCH_INCOMPLETE_RESULT_CAP`, `SEARCH_INCOMPLETE_PAGES_UNREAD`, `BUDGET_EXHAUSTED` (existing), `PROVIDER_FAILURE` (existing), `SEARCH_INCOMPLETE_REPLAY_ONLY` (existing) | The answer cannot be read as complete: capped list, result pages not read, a budget or limit, a provider error, an offline replay without a saved response |
| 4 | `4_CANDIDATE_NOT_COLLECTED` | `PLAUSIBLE_CANDIDATE_NOT_COLLECTED` (existing) | A relevant candidate was found and not collected (per-retailer limit, held for review, variants unreadable) |
| 5 | `5_FETCH_DEFERRED_OR_FAILED` | `FETCH_DEFERRED`, `FETCH_FAILED` | A candidate page was not fetched yet (budget, rate limit, replay) or could not be |
| 6 | `6_PAGE_IDENTITY_UNRESOLVED` | `PAGE_FETCHED_IDENTITY_UNRESOLVED` | The page was fetched and read; its identity or specifications did not resolve (missing model, conflict, review, no offer read) |
| 7 | `7_OFFER_EXCLUDED_BY_STOCK_OR_TERMS` | `SUITABLE_OFFER_EXCLUDED_BY_STOCK`, `SUITABLE_OFFER_EXCLUDED_BY_PURCHASE_CONDITIONS` | Confirmed, but not buyable as listed: out of stock/preorder/unknown, or terms that block the price (`offer_terms.terms_price_effect`) |
| - | `8_NOT_EVALUATED_OR_ERROR` | `NOT_EVALUATED_SEARCH_ONLY` (existing), `DISCOVERY_STEP_FAILED` | A search-only run that selected pages but did not collect; or the candidate step raised an error and its results were kept |

Order when several apply (nearest to a price first): confirmed offer, fetched page unresolved,
fetch failed/deferred, relevant candidate not collected, search incomplete, all rejected, no results.
Class 7 is about what a customer could buy: the weekly price policy still shows an out-of-stock
price with a note, and that decision stays in the price report.

## 7. Reuse and requests avoided

Holds today and is now counted. The same search URL is one request for every target and round
(`answered`, keyed by URL); a saved response of `--search-cache` replaces a request; a product page
inspected in the run is inspected once for all targets; a manifest URL (variant included) is
collected once. New: the collector is given the pages this run itself fetched while inspecting
(`--resume-from collection_rounds/fresh_inspections`), so it answers the identical request from
them. Pages replayed from `--search-cache` are excluded: a collection price always comes from a
page retrieved in this run. A variant URL is a different request and is never answered by the bare
product page. `discovery_summary.json` `request_reuse` reports `searches_reused_same_run`,
`searches_reused_saved_cache`, `product_inspections_reused_*`, `collection_requests_deduplicated`,
`collection_pages_reused_from_inspection` and `requests_avoided_total`.

## 8. Targeted recovery of the known misses

`config/recovery_leads_2026-11-03.csv` (supplied-URL format, provenance in `notes`) and
`config/recovery_scope_2026-11-03.csv` (the six targets).

| Lead | Retailer | Target | Provenance | Saved evidence |
|---|---|---|---|---|
| REC-pcc-0001 | PC Central | TGT-000293 ASUS X1504VA-BQ4301WSM, Core 3 100U, 8GB/512GB | Search hit and catalog lead of run `20261103T062304Z` | Page saved 2026-11-03T06:36Z: 8GB, 512GB, in stock, 36,999 (regular); lost to the crash. Offline replay: `REVIEW_REQUIRED` (`MISSING_CORE_FIELDS`, `MULTI_VARIANT_SHARED_DESCRIPTION`, `TITLE_MODEL_SCOPE_UNPROVEN`): model, screen and CPU are not extracted |
| REC-pcc-0002 | PC Central | TGT-000095 Acer AG14-71M-58JD, Core Ultra 5 115U, 16GB/512GB | Search hit in runs 20261022 and 20261103, catalog lead | Page saved 2026-11-03T06:35Z: 16GB, 512GB, in stock, 50,999; same `REVIEW_REQUIRED` reasons |
| REC-pcx-0001 | PC Express | TGT-000003 MacBook Neo A18 Pro 8GB/256GB | Product sitemap entry (title "256GB (MHFA4PP/A) ... (Silver)", lastmod 2026-11-03) | No page saved; RAM, stock and price unknown. Search missed it (cap reached; capacities query empty) |
| REC-pcx-0002 | PC Express | TGT-000002 MacBook Neo A18 Pro 8GB/512GB | Product sitemap entry (title "512GB (MHFC4PP/A) ... (Silver)") | As above |
| REC-datablitz-0001 | DataBlitz | TGT-000001 MacBook Air M5 16GB/512GB | Audit lead L001 (`tests/scraping_fixtures/discovery_benchmark/leads_v1.csv`) and a page fetched 2026-10-25 (`docs/benchmarks/variant-page-comparison-2026-10-25.csv`), added 2026-11-04 | That fetch ended `REVIEW_REQUIRED` (`TITLE_SPEC_CONFLICT_SCREEN_SIZE_IN`; `MISSING_MODEL`). The title/specification screen pair is the one the reviewed MacBook Air M5 entry resolves when the page states 13.6 (the page itself was not saved, so this is not verified here). Audit price (72,990) and "sold out" are unverified claims: no current offer, price or stock |

**DataBlitz MacBook Air M5 16GB/1TB (TGT-000006): no product URL exists in any saved evidence.** The
only audit lead for it (L006) is a collection page, and the two DataBlitz M5 Air 24GB/1TB pages
(silver `mdh94pp-a`, midnight `mdhg4pp-a`) are other targets (24GB). The first version of this
document said no DataBlitz M5 Air URL existed at all; the 2026-10-25 benchmark files, which hold the
TGT-000001 lead above, were missed. Inventing a URL for TGT-000006 would be wrong. Recovery is by
search (`Apple MacBook Air M5`, the `2_PRODUCT_NAME_CPU` tier, 13-inch targets) and by hand through
`web_search_plan.csv` (`site:ecommerce.datablitz.com.ph "Apple MacBook Air M5"`); a URL found goes
into the leads file. The storefront route cannot help (DataBlitz renders its search page in the
browser).

Command (do not run until approved; live requests, writes observations to the history store after
backing it up, as the weekly collection does). The owner-named pairs are 6 targets x 3 retailers = 18
jobs; the other 12 pairs (for example TGT-000293 at PC Express) only add cached or cheap searches.

```bash
R=data/output/weekly_runs/20261103T062304Z
python discover_products.py --live --collect \
  --scope-file config/recovery_scope_2026-11-03.csv --retailers pcc,pcx,datablitz \
  --target-master $R/inputs/target_master_validated.csv \
  --selected-list $R/shopee_variations/context_selected_list.csv \
  --contributions $R/shopee_variations/context_contributions.csv \
  --supplied-urls config/recovery_leads_2026-11-03.csv \
  --search-cache $R/discovery --cache-max-age-hours 48 \
  --max-total-requests 80 --max-search-requests 45 --max-product-fetches 25 \
  --max-storefront-search-requests 6 --max-discovery-rounds 2 --web-search-plan \
  --store data/history/price_history.sqlite3 \
  --output-dir data/output/recovery_2026-11-03/discovery
```

Replace `--live --collect` with `--preview` to see the plan without any request (worst case 111
searches, capped at 45 by `--max-search-requests`; 72 page fetches, capped at 25). Leads go through
the existing inspect, collect, match pipeline: search-index and sitemap evidence alone establishes no
current offer, no price, no RAM and no stock. Group the result by `terminal_outcome` in
`search_jobs.csv`.

Verified offline (nothing sent): `--preview` with these inputs; and `--live --replay-only --collect`
with the saved folder as `--search-cache` and `--replay-collection`, plus a scratch cache holding the
two saved PC Central pages. Result: both PC Central leads now reach collection by their variant
URL and end as `PAGE_FETCHED_IDENTITY_UNRESOLVED`; both PC Express leads end as `FETCH_DEFERRED`
(no saved page); the DataBlitz jobs end as `SEARCH_INCOMPLETE_REPLAY_ONLY` (the new name queries
are not in the saved cache).

## 9. Tests

`tests/test_discovery_outcomes.py` (terminal states, columns, step failure), `tests/test_discovery_category_filter.py`
(real titles), `tests/test_discovery_routes.py` (spellings, cap, unavailable products, storefront
route and cap, pagination budget, web-search plan), `tests/test_discovery_leads.py` (variant-preserving
identity, lead revalidation, reuse accounting, the PC Central crash with the saved pages, recovery
leads). Existing assertions in `test_product_discovery.py` and `test_identifier_variants.py` changed only to
include the new compact spelling tier.

## 10. Limitations and owner decisions

- **PC Central identity (resolved in extraction, not yet live).** After the crash fix the two PC
  Central pages were collected but stayed in review: extraction reported
  `MULTI_VARIANT_SHARED_DESCRIPTION` and no model for a single-variant page with named options.
  `adapters/pcc.py` (pcc-7) now proves the sole variant, and the saved X1504VA and AG14-71M pages
  extract model, screen, CPU, RAM and storage
  ([EXTRACTION_FIXES_2026-11-03.md](EXTRACTION_FIXES_2026-11-03.md)). The offline replay figures in
  section 8 (`PAGE_FETCHED_IDENTITY_UNRESOLVED`) predate that fix; a live recovery run is still
  needed to show a priced or alternative outcome for them.
- **Estimates.** The 177-job breakdown reclassifies saved facts offline; relevance is approximated
  from saved categories. No recall or price improvement is claimed: no live run was made.
- **Defaults changed:** `--max-identifier-variant-queries` 3 (was 2); predictive searches send the
  `unavailable_products` option; the `2_PRODUCT_NAME_CPU` query adds one search per name target and
  retailer when earlier tiers found nothing plausible (bounded by `--max-queries-per-target-retailer`).
  Say if any should stay off.
- **Purchase conditions** use the price policy's own rule on the collected observation
  (`terms_price_effect`); an out-of-stock offer is classed 7 here although the weekly price report
  may still show its price with a note.
- **Catalog leads** (`discover_catalog_leads.py`) assign sitemap URLs only to code targets; the
  name targets (MacBook Neo, MacBook Air) get no automatic leads, hence the manual recovery file.
- **Storefront route** is unverified live (Shopify markup varies by theme); the parser is tested on
  synthetic pages that follow the standard link and `rel="next"` structure. PC Express and Octagon
  external terms are RESTRICTED (see RETAILER_DATA_USE_REVIEW.md): an extra request type needs the
  same owner decision as the sitemap reads.
