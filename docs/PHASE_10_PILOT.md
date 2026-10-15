# Phase 10 pilot operation

Status: **IN_PROGRESS**. First live cycle completed on 2026-10-12; broader pilot acceptance remains open.

## Current scope and evidence

Seven verified Top 100 configurations, all collected from Laptop Factory:
TGT-000017 (rank 8), TGT-000059 (16), TGT-000031 (34), TGT-000030 (36),
TGT-000226 (39), TGT-000034 (50), TGT-000258 (65).
The owner authorized 5-19 configurations; seven were chosen because reviewed URLs and complete
saved matches already existed. This is a subset of the verified list, not its first seven ranks.

- Fresh Sheet validators and verified-list audit passed.
- Preview then live: seven successful first-attempt requests, seven observations, seven EXACT
  matches to the expected selected targets, zero match-review rows.
- Captured HTML, visible prices/stock, five core fields, URLs, hashes and weekly selections were
  reconciled by Codex. Owner acceptance is still required after the broader pilot.
- Three in-stock comparison prices; four out-of-stock observations retained with blank comparison
  prices. The four price-review rows are stock exclusions, not matching failures.
- Main history: nine prior observations preserved plus seven new observations, total 16.
- Duplicate CSV import: zero new observations; six report CSVs byte-identical at the same cutoff.

Private artifacts (Git-ignored):

- `data/collection/phase10_2026-10-12/manifest.csv`, `pilot_scope.csv`, `pilot_plan.json`.
- `data/collection/phase10_2026-10-12/inputs/`: frozen catalog, selected list, ledger and sales ranking.
- `data/collection/phase10_2026-10-12/before_cycle_01.sqlite3`: pre-collection backup.
- `data/collection/phase10_2026-10-12/cycle_01_html/`: original response bodies and request metadata.
- `data/output/phase10_2026-10-12/cycle_01/PILOT_REPORT.md`: results and open work.
- Same directory: `pilot_reconciliation.csv`, seven-row `pilot_weekly_comparison.csv`, collection,
  complete 100-row price reports and duplicate reimport check.
- `data/output/phase10_2026-10-12/cycle_01/with_retailer_links/`: regenerated reports at the original
  cutoff, with retailer product URLs beside comparison prices. The seven-row pilot view includes
  links for all seven offers, including the four out-of-stock offers. Earlier reports are preserved.

## Following cycles

The next weekly cycle is due in the week starting **2026-10-19**. No schedule was created.

1. Read and validate the three live catalog tabs into a new snapshot directory using
   `check_target_tables.py --from-sheets --output-dir <new-input-directory>`.
2. Audit the verified list against the fresh snapshots. If identities/mappings changed, investigate
   and refresh the list through the existing approved process before collection.
3. Review the manifest URLs and expected targets. Retain the seven configurations for repeat
   observations. A new retailer/extra configurations must have trustworthy labeled evidence;
   keep the overall pilot scope within the authorized 5-19 distinct configurations.
4. Make a database backup through SQLite's backup API, then run `collect_and_match.py --preview`
   with the fresh `--target-master`, selected list and ledger. Preserve the request plan.
5. Run the same manifest with `--live` into a new run directory. Use the main history store;
   retain response captures and reconcile each extracted field, price and availability.
6. Run `track_prices.py` with the same catalog, working list and ledger, explicit Monday
   `--week-start` and offset-aware `--as-of`, into a new report directory. Reports include all
   100 configurations; separately filter the pilot subset for scope-level metrics.
7. Review stock changes, price changes, URL changes, missing data and match conflicts. Never
   carry an old price into the new week. Inspect failures and retry only failed manifest rows
   after addressing their cause, preserving both original and retry reports.
8. A duplicate-import replay at the same cutoff should add zero observations and leave report
   CSVs unchanged. Later real retrievals are new observations even if price/stock is unchanged.
9. After review, publish the cycle's pilot scope with `publish_prices.py` (preview first), as
   described below. A new reporting week replaces the pilot tabs with that week's reviewed
   reports; earlier weeks stay in the history tab, which is built from the cumulative store.

## Acceptance gates before Phase 11

- Several supervised cycles, including at least two distinct weekly reporting windows.
- A second retailer with trustworthy configuration evidence; document unresolved coverage.
- Reconcile every pilot offer and resulting comparison. Record issues and reviewer decisions.
- Demonstrate repeatability and document request recovery. The first cycle observed no failures;
  live failure-retry recovery remains unobserved.
- Owner accepts match quality, missing-price behavior, residual risks and operating effort.

The known PC Central MSI page has RAM variants and shared specifications. Keep that observation
out of automatic configuration comparisons unless evidence/policy is reviewed; the current
matcher must not be weakened to increase coverage. *(Superseded on 2026-10-12: the earlier
statement here that no Sheet price publishing exists. The manual publisher below was added and
authorized.)*

## Publishing a reviewed cycle to the tracker (manual)

Publish only after the cycle's reports have been reviewed and reconciled. Never publish the full
100-row report; publish the authorized pilot scope.

1. Confirm the reviewed report folder (for example `.../cycle_NN/with_retailer_links/`) and the
   cycle's `pilot_scope.csv`.
2. Preview:
   `python publish_prices.py --preview --report-dir <reviewed reports> --pilot-scope <scope>`.
   Check the reporting week, cutoff, Shopee label, scope count, outcome counts, and that each of
   the four tabs is new or already holds this publisher's header.
3. Publish: the same command with `--write-sheets`. It backs up existing pilot tabs locally,
   replaces `PILOT_PRICE_COMPARISON`, `PILOT_PRICE_HISTORY`, and `PILOT_PRICE_REVIEW`, verifies
   every cell by readback, and records `SUCCESS` in `PILOT_PRICE_RUN_LOG` only after readback
   passes.
4. If the status is `PARTIAL_FAILURE` or `READBACK_FAILED`, read
   `data/output/sheet_publications/<time>_write/publication_report.json`, fix the cause, and
   rerun the same command.
5. Record the outcome in the progress log.

The publisher reads saved reports only; it never collects, rematches, imports, or opens the
history store. Details: [SETUP_AND_OPERATIONS.md](SETUP_AND_OPERATIONS.md#publishing-pilot-results-to-google-sheets-phase-10-manual).

**First publication (2026-10-12).** Cycle 01 (`with_retailer_links`, week 2026-10-12, cutoff
2026-10-12T09:02:54Z, `IN_PROGRESS` week) was published for the seven pilot configurations:
- `PILOT_PRICE_COMPARISON`: 7 rows, 3 comparable Laptop Factory prices, and 4 blank out-of-stock
  prices with their observed-offer links.
- `PILOT_PRICE_HISTORY`: 14 rows (the 2026-10-08 and 2026-10-12 observations of the seven
  targets).
- `PILOT_PRICE_REVIEW`: 4 stock exclusions and 0 matching problems.
- `PILOT_PRICE_RUN_LOG`: one row, `SUCCESS`, readback `PASSED`.

The first attempt stalled on the network before its first write and was stopped. It created
the four empty tabs and wrote no data. A per-request HTTP timeout was then added, and the retry
succeeded. An independent read-only readback found 0 issues.

## Expansion to all five retailers (2026-10-13)

The owner requested a pilot at all retailers. Twelve offers for the same seven configurations
were collected: LF 7, DBZ 2, PCX 1, PCC 1 selected variant, OCT 1. All twelve requests produced
valid observations. Seven LF offers matched EXACT; the other five require review. LF still
supplies three comparable prices. This is a small sample across all retailers, not complete
configuration coverage or another weekly window.

Fresh catalog validation and the verified-list audit passed. Captured response provenance for
all twelve observations and the seven confirmed LF configurations were reconciled. The sixteen
prior store observations are preserved; the store now holds twenty-eight observations.

The reviewed result was published and separately read back: 7 comparison rows, 26 history rows,
13 review rows (8 dated stock exclusions and 5 matching review items), and 2 run-log rows.
Unassigned observations remain in history with blank target IDs. Manifest/offer identity scopes
history without establishing a match. Missing confirmed offers are labeled explicitly.

*(Addressed offline on 2026-10-13; see the next section.)* The next extraction work is
concrete: DBZ has explicit Model/Size labels absent from the map
(the Swift Size is unit-less); PCX has no trusted model field; PCC shares specifications across
RAM variants; OCT specification lines need safe layout parsing and supporting evidence review.
Preserve these failures rather than forcing target IDs. Replay saved evidence after changes
before collecting again. The second weekly window still begins 2026-10-19.

Private report: `data/output/phase10_2026-10-13_all_retailers/PILOT_REPORT.md`.
Response captures and frozen inputs: `data/collection/phase10_2026-10-13_all_retailers/`.

## Extraction replay and candidate discovery (2026-10-13)

The extraction work above was done offline from the saved evidence; no retailer was contacted,
and nothing was imported, published, mapped, or promoted. Adapters are now `dbz-2`, `pcx-2`,
`pcc-2`, and `oct-2` (`lf-1` unchanged); rules are in [MATCHING.md](MATCHING.md).

**Replay result.** Both replay paths give 7 `EXACT` (the same seven Laptop Factory targets and
prices; three comparable, four out of stock) and 5 `REVIEW_REQUIRED`. The five review cases are
better explained, not matched:

| Case | Now extracted | Still blocking |
|---|---|---|
| DBZ Aspire | screen `14`, CPU, RAM, storage | model `A14-11M-X3BP OPI` (`UNRESOLVED`) |
| DBZ Swift Go | model `SFG14-75-55MB`, CPU, RAM, storage | screen: `Size: 14.0` has no unit; 14-inch only in prose |
| PCX Vivobook | screen, CPU `Core 5`, RAM, storage (unchanged) | no trusted model field |
| PCC MSI 16GB | RAM `16`, storage `512` (bound selected variant) | model; CPU/screen only in the shared description |
| OCT Nitro | screen `15.6`, CPU `Ryzen 5`, RAM `16` | model; storage `1x 512` has no unit |

Re-parsed records keep their observation identity with new content, so the store refuses them
(checked on a throwaway copy; the live store was unchanged). They are written as
`reparsed_observations_NOT_FOR_IMPORT.csv` only. The next live cycle stores fresh `-2`
observations normally.

**Runbook after an extractor or adapter change** (before the next collection):

1. Replay into a new folder; it must not exist yet:
   ```bash
   python replay_extraction.py \
     --observations <run>/collection/observations.csv \
     --target-master <inputs>/target_master_validated.csv \
     --html-dir <collection>/html \
     --before-results <run>/collection/match_results.csv \
     --cases <run>/extraction_review_package/cases.json \
     --conflict-check-store data/history/price_history.sqlite3 \
     --output-dir <run>/extraction_replay_<date>
   ```
   Check `before_after_comparison.csv` (every `EXACT` unchanged, review reasons explained) and
   `html_replay/reparse_differences.csv` (only expected columns changed).
2. Optionally plan research for configurations without a confirmed offer:
   ```bash
   python discover_candidates.py \
     --references <inputs>/top_100_verified_configurations.csv \
     --target-master <inputs>/target_master_validated.csv \
     --scope-file <collection>/pilot_scope.csv \
     --observations <run>/collection/observations.csv \
     --observations <run>/extraction_replay_<date>/html_replay/reparsed_observations_NOT_FOR_IMPORT.csv \
     --output-dir <run>/candidate_discovery_<date>
   ```
   Categories are research priorities. A candidate enters a manifest only after a person reviews
   its page; `DISCOVERED_NOT_FETCHED` variants need their own supervised collection first.

**Owner decisions at the first replay** (historical; decisions below supersede items 1 and 3):

1. **Description prose as a screen-size source** (DBZ Swift Go: unit-less `Size: 14.0` beside a
   14-inch description and JSON-LD description). Today: supporting only.
2. **`OPI` after a model code** (DBZ Aspire `A14-11M-X3BP OPI`; Octagon title `ANV15-42-R2UH
   OPI`). Is it the same configuration as the catalog code? The Acer store slug in TGT-000226's
   `source_url` contains `a14-11m-x3bp-opi`, a lead but not proof. Today: unresolved, never
   stripped.
3. **Shared-description inheritance on multi-variant pages** (PC Central MSI: may CPU and display
   from a description shared by all RAM variants be trusted for the bound selected variant?).
   Today: supporting only.
4. **Trusted model evidence** for PC Express (title only; `90NB1672-M00DD0` role unverified), PC
   Central MSI (title only), and Octagon Nitro (title only), and the role of DataBlitz `Part
   Number` `NX.JP3SP.001`.
5. **Storage unit** for Octagon `1x 512 PCIe Gen4`: needs an explicit unit from the page or
   another trusted source.

### Owner policy follow-up (2026-10-13)

- Approved: explicit product screen diagonals in its own visible/JSON-LD description, with
  provenance and no specification conflicts. Bare numbers alone remain insufficient.
- Approved conditionally: shared labeled CPU family/screen with confirmed variant selection
  and a complete RAM/storage-only definition matrix; preserve `SHARED_PRODUCT` scope, selected
  RAM/storage, and conflicting shared memory evidence. Missing matrices stay unproven.
- Kept unresolved: OPI suffix equivalence. Kept restricted: titles/URLs/product names as
  discovery, retailer SKUs as inventory identifiers, uncorroborated part numbers as evidence
  only. Kept unknown: Octagon bare storage 512 and throughput 16 Gb/s.

Offline replay of the same twelve HTML pages: **8 EXACT, 4 REVIEW_REQUIRED, 0 parse failures**.
Swift Go is the new exact match but its bundle excludes its price. PCC MSI recovers Core i5,
15.6, selected 16/512; missing trusted model still blocks an exact match. Original CSV remains
7/5 because its records lack the newly captured scoped evidence. No reparsed record was
imported, no retailer requested and no Sheet changed. No unit test suite was run for this
follow-up. Phase 10 stays IN_PROGRESS; a comparable second retailer and another weekly cycle
remain outstanding. See `owner_policy_replay_2026-10-13/` under the saved all-retailer run.

### Manufacturer research and software validation (2026-10-13)

**Research archived.** The supplied manufacturer-identifier research report is archived, with
a per-source register, in the ignored
`data/output/phase10_2026-10-13_all_retailers/manufacturer_research_2026-10-13/` (`README.md`,
`research_sources.csv`). Each source records its URL or file, locator, retrieval and document
dates, evidence scope, conflicts, and verification status.

| Case | Research result (as supplied) | What it establishes | What it does not |
|---|---|---|---|
| PCC MSI Thin 15 (TGT-000258) | PARTIALLY_RESOLVED | MSI's model-specific PDF names `B13UC-3474PH` with a factory Core i5 / 15.6" / 8GB / 512GB. The saved selected variant `50886253871271` is retailer-offered 16GB / 512GB. | A binding of that 16GB retailer variant to the manufacturer model. The factory 8GB never replaces the selected 16GB. |
| DBZ Acer Aspire (TGT-000226) | PARTIALLY_RESOLVED | Acer's official store sells `A14-11M-X3BP OPI` under Ref. `NX.JP3SP.001`, matching DataBlitz's labeled rows. | What OPI means, or that the unsuffixed code is the same product. |
| PCX ASUS Vivobook (TGT-000031) | UNRESOLVED | ASUS's page confirms the `S3607VA` family only. | The `-RP181WSM` suffix or the `90NB1672-M00DD0` pairing. |
| OCT Acer Nitro (TGT-000059) | UNRESOLVED | GameXtreme lists the same barcode `4711474944863` with `512GB` (a discovery lead). | A trusted model, OPI equivalence, or Octagon's storage unit. One retailer's specification is never borrowed for another. |

**Supplied vs verified.**
- **Not re-fetched.** No manufacturer or other-retailer source was re-fetched.
- **Research copies missing.** The six HTML/PDF/image copies listed in the report were not
  supplied and are not on this machine, so their hashes are recorded as supplied, unverified.
- **Saved snapshots verified offline.** The four cited saved pilot snapshots were verified:
  each SHA-256 matches, and every quoted identifier and value is present.
- **Catalog note (no change made).** TGT-000226's catalog `source_url` is the same Acer OPI
  store page, while its catalog `model` is unsuffixed. This is relevant to the OPI decision.

**Software validation.** `tests/test_owner_policy_rules.py` adds 45 focused tests for:
- product-description screen sizes;
- shared CPU/screen inheritance;
- the research boundaries above.

The approved rules need the display noun right after the size: "14-inch display" is read, but
"14-inch WUXGA display" is not and stays for review. Results:
- full suite 1318 passed; Ruff, compileall and `git diff --check` passed;
- offline replay of the same twelve offers into
  `research_validation_replay_2026-10-13/` gave original CSV **7 `EXACT` / 5
  `REVIEW_REQUIRED`** and saved HTML **8 `EXACT` / 4 `REVIEW_REQUIRED`**, 0 re-parse failures;
- Swift Go is `EXACT` (TGT-000030) with its price excluded as `BUNDLE_OFFER`;
- PC Central MSI has four core fields and is still blocked by `MISSING_MODEL`;
- the throwaway-copy import refused the five changed records, and the live store hash was
  unchanged.

**Evidence still needed** (the five-field identity is unchanged; upgrade installer, factory
origin, and warranty are not matching requirements):
- **PCC MSI:** written PC Central confirmation that variant `50886253871271` is `B13UC-3474PH`
  with 16GB installed, or another trusted model field for that variant.
- **DBZ Aspire / OCT Nitro:** an Acer Philippines definition of OPI and a suffixed/unsuffixed
  crosswalk, answered per family.
- **OCT Nitro:** a trusted model field, an explicit installed SSD capacity, and the actual
  processor for the selected product.
- **PCX Vivobook:** ASUS Philippines SKU documentation or a product/carton label showing
  `S3607VA-RP181WSM` and `90NB1672-M00DD0`, tied to this item.

### Owner equivalence/capacity follow-up (2026-10-13)

After research validation, the owner approved DataBlitz Aspire OPI equivalence, explicitly
approved Octagon OPI equivalence, then approved GB for Octagon's bare 512 drive amount.
Listing/variant-scoped approvals and independent core guards are recorded in the policy CSV.
The processor-number discrepancy and missing explicit page storage unit remain in evidence;
OPI meaning is not asserted to be manufacturer-verified.

Offline replay: saved HTML 10 EXACT / 2 REVIEW_REQUIRED; original CSV 9 / 3. The HTML path has
five comparable saved offers (LF three, DBZ one, OCT one), four out-of-stock LF offers, one
bundled Swift Go and two unresolved model bindings (PCX ASUS/PCC MSI). Outputs are in
`owner_equivalence_replay_2026-10-13/` under the all-retailer output folder. These are saved
October 13 prices, not a fresh collection. No store import, Sheet publication or commit.
Ruff and whitespace checks are used for this follow-up; the full suite has not been rerun
since the scoped owner approvals were added.

Before another live pilot: validate the new scoped decisions, review the candidate manifest
with exact variant URLs, collect fresh pages, import fresh-timestamp observations, match under
the new fingerprint, generate reports, review exclusions/provenance, preview and publish.
The pilot can run with ASUS/MSI still in review. Better offline discovery rankings/query plans
have not yet demonstrated improved coverage in a new live collection. Phase 10 stays IN_PROGRESS.

## Fresh all-retailer pilot and stock policy update (2026-10-13)

Fresh catalog checks and verified-list audit passed. Twelve fresh HTTP 200 observations across
five retailers for the same seven configurations give ten EXACT expected matches and two
REVIEW_REQUIRED (PCX ASUS/PCC MSI model binding). Original saved records remain unchanged;
store observations increased from 28 to 40 and prior contexts/results were preserved.

The owner requested OUT_OF_STOCK displayed prices remain comparable during this run. Current
reports select nine prices (LF 7, DBZ 1, OCT 1), covering all seven configurations, with four
LF prices clearly labeled OUT OF STOCK. Swift Go's bundle remains excluded. History/review keep
availability and owner-approval/CPU/color evidence. Report cutoff is 2026-10-13T08:03:19.735677Z,
within the October 12 reporting week; Shopee's historical August/September labels are unchanged.

Tests: 1,338 passed, including 19 new scoped-owner-approval checks and the stock-policy coverage.
Ruff, compileall and whitespace checks pass. Fresh saved HTML reproduces all twelve records
identically and the 10/2 matches. A throwaway duplicate import adds zero, without changing the
main store. Preview then publication PUB-1ccbd431abba8a07 succeeded with zero readback issues:
7 comparison rows, 38 history rows, 6 review rows and 4 historical run-log entries. An earlier
publication from this collection used the previous stock rule and is preserved in the log;
the latest publication supersedes it. Local backups of the four pilot tabs were saved.

Artifacts: `data/output/phase10_2026-10-13_policy_pilot_20261013T075714Z/PILOT_REPORT.md` and `reports_out_of_stock/`;
inputs/captures: `data/collection/phase10_2026-10-13_policy_pilot_20261013T075714Z/`. No catalog/mapping changes, commits or scheduling.
Phase 10 stays IN_PROGRESS; a second weekly window begins 2026-10-19 and owner acceptance
remains open. The two unresolved model bindings do not prevent further supervised collection.

Separate live readback for the latest stock-policy publication: zero issues; all four OUT OF STOCK
labels, prices, links and PHP formats verified. Live rows are 7 / 38 / 6 / 4.

## Automatic product discovery pilot (2026-10-13)

`discover_products.py` now searches all five retailers for verified configurations, ranks the
results, writes a `collect_and_match.py` manifest, and optionally collects and matches it. Usage is
in [SETUP_AND_OPERATIONS.md](SETUP_AND_OPERATIONS.md#automatic-product-discovery-phase-10-manual-runs).

**Search mechanisms**, inspected live before implementation (14 requests; evidence in the ignored
`data/collection/search_inspection_2026-10-13/`):
- **Laptop Factory:** WooCommerce HTML search with pagination.
- **The four Shopify stores:** predictive search JSON, at most 10 results, no pagination.

robots.txt allows both paths. DataBlitz's HTML search is client-rendered, so it is not used.

**Bounded live pilot.** Preview first, then one configuration (TGT-000059) across five
retailers, then all seven.
- **One configuration:** 12 requests; 2 EXACT. It exposed a defect: two-drive and mistyped PC
  Central option titles were read as matches, so nothing was chosen (fail-safe). This was fixed
  and verified offline on the saved page.
- **All seven:** 84 of 95 requests; 14 pages collected, 7 EXACT and 7 review. A second defect
  left the PC Express pages uncollected (duplicate JSON-LD blocks were counted twice as
  variants). It was fixed, followed by a PC Express-only run: 18 requests, 4 collected, all
  `MISSING_MODEL`.
- **Errors:** none (no search error, unsupported layout, or failed fetch).
- **Store:** 40 → 60 observations (20 fresh retrievals); a backup was taken before each import.

**Coverage against the earlier manual manifest:**
- **Found by search:** 8 of the 12 earlier URLs.
- **Not found:** four Laptop Factory URLs (TUF A15, Vivobook S16, Swift 14, MSI Thin 15). They
  were out of stock, and that store's search appears to omit out-of-stock products.
- **New URLs:** 10 newly discovered URLs were collected:
  - Laptop Factory Swift Go bundle: EXACT, price excluded as a bundle.
  - DataBlitz TUF, Vivobook, and Swift 14.
  - Octagon TUF and Swift 14.
  - PC Central Nitro (16GB/512GB variant).
  - PC Express TUF, Swift Go, and MSI.

  All but the bundle need review: missing model evidence or extraction gaps on new layouts.
- **Not collected, kept for review:** DataBlitz MSI Thin 15, whose title states 8GB.
- **Prices:** comparable prices for the seven configurations are unchanged (LF 7, DBZ 1, OCT 1).
  **Discovery added no new comparable price yet.** Old known URLs that matched again are not
  counted as a discovery gain.

**Still manual:**
- **Laptop Factory:** out-of-stock products are not searchable. Keep earlier known URLs as a
  manual supplement.
- **Search-dependent gaps:** broad Shopify family queries can be truncated at 10. DataBlitz and
  PC Express multi-variant pages cannot be collected.

**Next evidence work:** extraction review of the new DataBlitz and Octagon layouts and the PC
Central Nitro shared description, and trusted model evidence for PC Express. Summary:
`data/output/phase10_2026-10-13_auto_discovery/DISCOVERY_REPORT.md`. No Sheets publication,
catalog or mapping change, schedule, or commit. Phase 10 stays **IN_PROGRESS**.
