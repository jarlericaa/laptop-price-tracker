# Discovery, verification and price-tab improvements (2026-10-22)

This covers the work requested on 2026-10-22: diagnosing the 450 blank retailer cells of the
2026-10-20 Top 100 pilot, improving discovery without loosening matching, and making
`PILOT_PRICE_OVERVIEW` publisher-managed. Nothing here changed the live Google Sheet, the
catalog, mappings, approvals, the history store, or the approved matching and price policy.

## 1. Diagnosis

### Observed facts

All counts are reproduced offline from saved files with `diagnose_blank_cells.py` and
`measure_discovery.py` (inputs: `data/output/top100_listed_spec_20261020T072033Z/` and the
published report `data/output/shopee_summary_2026-10-20/top100/reports/`).

- **500 cells, 50 comparable prices, 450 blanks.** Blanks by retailer: DataBlitz 86, Laptop
  Factory 88, PC Express 82, PC Central 97, Octagon 97. This matches the live publication.
- **Where the blanks stopped:**

  | Stage | DBZ | LF | PCX | PCC | OCT | Total |
  |---|---:|---:|---:|---:|---:|---:|
  | `NO_RELEVANT_CANDIDATE` (results, none relevant) | 60 | 29 | 57 | 77 | 85 | 308 |
  | `ZERO_RESULTS_AFTER_BOUNDED_SEARCH` | 0 | 44 | 0 | 3 | 1 | 48 |
  | `CANDIDATE_DISCOVERED_NOT_FETCHED` | 12 | 0 | 11 | 14 | 4 | 41 |
  | `CONFIGURATION_MISMATCH` | 6 | 6 | 10 | 0 | 3 | 25 |
  | `MATCH_REVIEW_REQUIRED` | 7 | 9 | 2 | 1 | 4 | 23 |
  | `CATALOG_IDENTITY_UNRESOLVED` | 1 | 0 | 2 | 0 | 0 | 3 |
  | `VARIANT_UNRESOLVED` | 0 | 0 | 0 | 2 | 0 | 2 |
  | `COMPARABLE_PUBLISHED` | 14 | 12 | 18 | 3 | 3 | 50 |

  No cell was `NOT_SEARCHED`, `PAGE_FETCH_ERROR`, `PAGE_PARSED_NO_PRICE` or
  `PRICE_KNOWN_NOT_COMPARABLE` in that run.
- **Search caps.** 329 of 500 jobs hit `RESULT_CAP_REACHED` (Shopify predictive search returns at
  most 10 unpaginated results: DataBlitz 181, PC Express 159, Octagon 119, PC Central 80 capped
  requests).
- **Identity kinds.** 285 of 500 jobs are for broad family-name targets (`IdeaPad`,
  `HP Laptop 15`), 105 for product names (MacBook) and only 110 for manufacturer model codes.
- **Hyphenated codes returned nothing.** On Laptop Factory and PC Express the complete code of
  listed products returned zero results (`B12UC-1432PH`, `E1504FA-NJ1527W`,
  `X1404VA-EB2264WSM`, `14-ep0445TU`, `G614PM-TS179WSM`), while their URL handles and titles show
  the code. 73 code jobs returned no result for the complete code
  (`retailer_search.search_queries`, `discover_products.SearchJob`).
- **The candidate limit dropped relevant pages.** Laptop Factory family listings for
  TGT-000015/16 (`hp-laptop-15-fc0826au`, `hp-laptop-15-fc0756au`) were
  `FAMILY_LISTED_SPEC_CANDIDATE` / `NOT_SELECTED_LIMIT` and never fetched
  (`discover_products.select_and_inspect`). 52 such skipped candidates sit in configurations
  without a suitable offer (LF 34, PCX 18).
- **Audit leads (2026-10-21), traced through the run's own evidence:**
  - 23 direct-page "exact" claims: 6 were returned by any search; 17 never appeared in a
    search result.
  - 3 more "exact" claims are category pages (no product-detail verification).
  - TGT-000017 at PC Express, claimed exact, is `REVIEW_REQUIRED` with `MODEL_CLAIM_CONFLICT` on
    the page the tracker saved.
  - None of the 12 audit review items was accepted by the matcher.
  - The Laptop Factory `hp-laptop-14-ep0445tu` page is claimed for both TGT-000013
    ("HP Laptop 14", family) and TGT-000233 (`14-ep0445TU`). The PC Express page of the same
    model priced TGT-000233 (₱36,990, in stock on 2026-10-20); the audit saw it as preorder.
  - The 333 `NO_VERIFIED_LISTING_FOUND` rows are not verified negatives: 286 of them are
    `NO_RELEVANT_CANDIDATE` cells whose searches hit caps or returned other models.
- **Matching has no fuzzy score.** `matching.ExactMatcher` uses the exact five-field key plus a
  severity table; titles and URLs are discovery hints only.
- **The live price tabs carry hand-applied styling** (read-only inspection): navy headers with
  white text, tinted price columns, hidden gridlines, frozen columns, 16 to 34
  conditional-format rules. `PILOT_PRICE_OVERVIEW` was 12 `FILTER`/`INDEX` formulas over the
  comparison. The pilot-sheets-5 summary layout reset backgrounds on every write, which would
  have left the white header text on a light header.

### Hypotheses (not verified)

- Laptop Factory's search omits out-of-stock products and tokenizes hyphenated codes badly.
- Shopify predictive search on PC Express treats a hyphenated code differently from its
  spaced form.
- The retailers' sitemaps list the product pages the search misses, including sold-out ones.

Only a bounded live run can confirm these. The new mechanisms are designed so that a wrong
hypothesis costs a few requests, never a wrong price.

## 2. Changes (by phase)

**Phase 0: reproducible diagnostics.**
- `src/laptop_tracker/cell_diagnostics.py` and `diagnose_blank_cells.py` assign one stage per
  cell. Each row also records queries with outcomes, tiers, stop reason, budget flags, best
  candidate, request outcome, observation ID and price-selection reason.
- `observed_listing_price` and `owner_policy_comparable_price` are separate columns.
- `offer_also_prices_targets` flags shared offers.
- `--audit-leads` overlays external claims and checks them: URL kind, found by search,
  collected, code in URL, URL shared by several targets.

**Phase 1: discovery.**
- **Adaptive identifier spellings.** Two tiers, `1_FULL_MODEL_CODE_SPACED` and
  `3_MODEL_CODE_SEGMENT`, run only when the complete code returned no result
  (`--max-identifier-variant-queries`, default 2). They are search text only.
- **No-series fallback.** `5_BRAND_CPU_MODEL_WITH_SPECS`: brand, recorded processor model and
  capacities, for code targets without a recorded series.
- **Opt-in second-pass collection** (`--max-second-pass-candidates-per-target-retailer`).
  - It collects relevant candidates the per-round limit skipped.
  - It runs only for configurations without a suitable offer, and only when no title clue
    states another configuration.
  - It stays within the same fetch budget and rounds. `search_jobs.csv` records it in
    `second_pass`, and an unrun pass is reported explicitly.
- **Opt-in sitemap catalog leads.** `src/laptop_tracker/catalog_leads.py` and
  `discover_catalog_leads.py`:
  - Robots.txt is checked first, with longest-match and wildcard rules.
  - The script reads the advertised `wp-sitemap.xml` (Laptop Factory) or `sitemap.xml`
    (Shopify), within a hard request ceiling, and saves the evidence. It never fetches a
    product page.
  - Leads are written in the supplied-URL format, only for MODEL_CODE targets whose complete
    code is in the URL handle or the Shopify sitemap title. Discovery's existing bounded
    fallback re-fetches and re-matches them.
  - `--from-saved` replays a run offline.

**Phase 2: verification safety.**
- **Opt-in matcher override.** `ExactMatcher(..., severity_overrides=STRICT_CPU_MODEL)` makes a
  verified processor-number conflict blocking. The default is unchanged and the override is
  not wired into any weekly command.
- **Shared offers are reported.** The publisher lists offers that price several targets
  (`offers_pricing_several_targets`, preview line "Review: one offer prices N
  configurations").
- **Named regression tests** cover the traps:
  - the FA607NUQ title contradicted by an FA607NUG detail table stays in review;
  - PC Central MacBook Neo option titles never select a variant or its price;
  - the CPU gate, default and strict;
  - shared-offer detection.

**Phase 3: price tabs (`pilot-sheets-6`).**
- **`PILOT_PRICE_OVERVIEW` is publisher-managed.**
  - Twelve columns, one row per target in comparison-rank order.
  - Every rank, specification and price is a numeric copy of the comparison cell; a blank stays
    blank.
  - The target ID is in each Laptop cell's note. Listed-spec prices are italic.
  - `configuration_labels()` distinguishes repeated labels, and `overview_issues()` refuses a
    shifted, merged or differing overview before any Sheets access.
- **Safe adoption of the owner-built overview.**
  - The overview is recognised only by its exact header and only when it has no extra columns.
  - Replacing it requires `--adopt-overview`.
  - The preview writes `current_PILOT_PRICE_OVERVIEW.csv` and `overview_migration.json`.
  - Tabs with any other content are refused.
- **Full snapshots and rollback.**
  - Before each write, `backup_<tab>.json` stores values and formulas, displayed values,
    notes, formats, sizes, panes and the filter.
  - `--rollback <folder>` restores the data tabs, only onto the same sheet IDs, and checks the
    result by readback. The run log is not rewritten.
- **House layout on every write.** The comparison, summary and overview get the owner's look:
  colours, widths, row heights, banding, frozen key columns (comparison 5, summary 1,
  overview 2) and hidden gridlines.
  - Layout requests never touch values, links, bold, italics or notes.
  - Readback also checks frozen panes and column widths.
  - The comparison column positions used by the owner's conditional formats are pinned by a
    test.
- **Run log.** It gains `Overview rows` and `Adopted tabs`; earlier headers stay recognised.

**Phase 4: measurement.**
- `tests/scraping_fixtures/discovery_benchmark/leads_v1.csv`: 53 labelled leads in 11 case
  types. Labels come only from the tracker's own saved evidence; audit claims are never labels.
- `measure_discovery.py` reports recall, accepted-match safety on independent negatives, price
  extraction, coverage, budget and runtime, plus reachability estimates (`--proposed`).

## 3. Verification

- `python -m pytest -q`: **1795 passed** (1729 before; +66 in seven new test modules, plus
  updated discovery and summary tests). `python -m ruff check .`: all checks passed.
  `compileall`: OK.
- **Replay (no requests).** The 2026-10-20 discovery replayed with the new code selects the
  same 96 candidates for collection as the original round 1 (96 of 96 identical).
- **Live read-only preview** (`spreadsheets.readonly` scope, nothing written):
  - 100 rows planned for each price tab.
  - Overview migration: 762 displayed cells compared, 6 differ.
  - The 6 are the Shopee header and 5 labels where the formula repeated the brand ("HP HP
    Laptop 14" becomes "HP Laptop 14").
  - All 600 prices agree.
- **Golden check of the real 100-row plan:**
  - Rank order is identical across the comparison, summary and overview.
  - 0 summary or overview prices differ from the comparison, and 450 blanks stay blank.
  - The overview has 100 distinct visible rows.

## 4. Migration and commands

Preview (read-only; reads the six tabs):

```bash
python publish_prices.py --preview --adopt-overview \
  --report-dir <reviewed report folder> --pilot-scope <scope.csv> \
  --collection-dir <discovery>/collection --selected-list <top_100_verified_configurations.csv> \
  --output-root <run>/publications
```

Production, **only after explicit go-ahead**:

```bash
python publish_prices.py --write-sheets --adopt-overview <same arguments>
python publish_prices.py --rollback <run>/publications/<timestamp>_write --output-root <run>/publications
```

`--adopt-overview` is needed once, for the first `pilot-sheets-6` write. Later cycles regenerate
the overview with the other tabs and need no flag.

### What a write touches

Each tab's sheet ID is guarded; no other tab can be addressed.

| Tab | Cells written (100-configuration plan) | Also |
|---|---|---|
| `PILOT_PRICE_COMPARISON` | `A1:BD101` (56 columns) | Whole-tab clear of values/number formats/links/bold; layout on the whole tab; widths A:BD; heights rows 1-101; freeze 1 row + 5 columns; filter `A1:BD101` |
| `PILOT_PRICE_SUMMARY` | `A1:G101` (7 columns) | As above plus notes/italics; freeze 1 + 1; filter `A1:G101` |
| `PILOT_PRICE_OVERVIEW` | `A1:L101` (12 columns) | As above plus notes/italics; freeze 1 + 2; filter `A1:L101`; replaces the 12 formulas and 12 header notes |
| `PILOT_PRICE_HISTORY` | `A1:X394` | Clear, freeze 1 row, filter (no layout change) |
| `PILOT_PRICE_REVIEW` | `A1:R540` | Clear, freeze 1 row, filter (no layout change) |
| `PILOT_PRICE_RUN_LOG` | `A1:AP13` (header + 11 earlier publications + this one) | Freeze 1 row, filter |

### Discovery commands

- **Discovery with the adaptive passes:** add
  `--max-second-pass-candidates-per-target-retailer 2` to the usual arguments. Identifier
  spellings are on by default.
- **Catalog leads:**
  1. `discover_catalog_leads.py --preview ...`, then
     `--live --max-total-requests 35 --max-sitemaps-per-retailer 5`.
  2. Pass `--supplied-urls <run>/catalog_leads/catalog_leads.csv` to `discover_products.py`.
- **Diagnostics:** `diagnose_blank_cells.py --report-dir ... --discovery-dir ... --coverage ...`.
- **Measurement:** `measure_discovery.py --benchmark tests/scraping_fixtures/discovery_benchmark/leads_v1.csv --report-dir ... --discovery-dir ... --proposed --output metrics.json`.

## 5. Before and after

| Metric | Before: 2026-10-20 run (measured) | After: this change (offline) |
|---|---|---|
| Comparable prices / blank cells | 50 / 450 of 500 | Unchanged: no live run was made |
| Known-positive discovery recall (33 audit product-page leads) | 15/33: LF 3/10, PCX 5/11, DBZ 2/6, OCT 3/4, PCC 2/2 | **Not measured.** 18 leads were missed. For 9 of them (LF 5, PCX 4) the new spellings would run and a sitemap lead could be assigned *if* the store's sitemap lists the URL. The 9 others are family/product-name targets the new mechanisms do not reach. |
| Accepted EXACT on independently-negative labels | 0 | 0 (matching unchanged by default) |
| Accepted EXACT on the 12 human review items | 0 | 0 |
| Price extraction on collected pages | 95/95 observations priced; 43/43 exact | Unchanged |
| Candidate selection on saved evidence | 96 manifest rows | 96, identical |
| Review volume | 23 match-review + 25 configuration-mismatch cells; 132 week observations needing review | Unchanged on saved evidence |
| Request budget / runtime | 1,012 requests (831 searches) in 94.9 minutes | At most +140 spelling searches and +4 brand/CPU searches. Optional: second pass, at most 2 fetches per eligible configuration (52 eligible candidates); catalog leads, at most 35 requests. |
| Strict CPU gate impact | — | Would change 0 of the 50 published prices. The 3 CPU-flagged prices have intra-page or listed-spec warnings, a different signal. |

**Limitations.**
- No live retailer request was made, so recall gains are reachability estimates, not measured
  recall. None is claimed as an improvement in prices.
- The sitemap formats are tested on synthetic fixtures that follow the advertised standards;
  the live layouts are unverified.
- The benchmark's positives are audit claims checked against our evidence, not ground truth.
  "Recall" means "the URL appeared in discovery", not "the price was comparable".
- Stock and price change between the audit (2026-10-21) and the run (2026-10-20).

## 6. Outstanding reviews and owner decisions

1. **CPU model identity.**
   - Keep `cpu_model` as a WARNING (approved), or adopt `STRICT_CPU_MODEL` for exact matches
     (0 published prices affected today)?
   - Separately: should an intra-page title-vs-specification processor difference
     (`CPU_NUMBER_TITLE_DIFFERS`) block? It affects 2 published prices: TGT-000017 DataBlitz and
     TGT-000059 Octagon (the latter under the owner-approved OPI interpretation).
2. **PC Central MacBook Neo (TGT-000002/003).**
   - The page has six colour/storage options ("McbookNeo SILVER 512GB") and no RAM or SSD
     wording, so no variant can be bound automatically and no parent "From" price is used.
   - Option: a listing-specific owner approval naming the variant IDs, as for the Octagon OPI
     listing.
3. **Conflict pages:**
   - PC Express FA607NUQ-RL011W (TGT-000023): the detail table says FA607NUG.
   - Octagon FA607NUQ-RL204WSM (TGT-000064): the page states FA607NUG; `MODEL_CLAIM_CONFLICT`.
   - DataBlitz FX608JHR-RV274WSM (TGT-000057): the images show RV179WSM.
   - PC Express FA506NCQ-HN012W (TGT-000017): the audit says exact; the saved page shows a model
     claim conflict.

   All remain in review until a person checks them.
4. **Duplicate URL and target.** TGT-000013 ("HP Laptop 14", family) and TGT-000233
   (14-ep0445TU) share brand, 14", Core i3, 8 GB and 512 GB.
   - Under the listed-spec policy one offer can price both. The publisher now reports this.
   - Decide whether TGT-000013 is a duplicate, or should record the specific code, in
     `TARGET_MASTER` (never merged automatically).
   - Other shared audit URLs: TGT-000040 with 010/011/026; TGT-000029 with 072; TGT-000002 with
     003 (one page, two variants).
5. **Family-name targets (285 of 500 jobs)** are the largest structural gap. Catalog
   reconciliation that records specific codes would let code search, spellings and sitemap leads
   reach them.
6. **Category-only audit claims** (DataBlitz TGT-000006 and TGT-000023, Octagon TGT-000035) need
   product-page verification.
7. **A bounded live verification run** needs go-ahead:
   - `discover_catalog_leads.py --live` (about 35 robots/sitemap requests);
   - discovery with spellings and a second pass.

   Octagon's and PC Express's external terms are RESTRICTED; sitemap reads are a new request
   type under the existing owner decision.
8. **Production Sheet.** Publishing `pilot-sheets-6` with `--adopt-overview` requires explicit
   authorization.
