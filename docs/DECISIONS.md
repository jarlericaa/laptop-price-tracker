# Decision Log

Dates are recorded as **Date not recorded** when the repository or project context does not provide a reliable decision date. Status describes whether the decision is active and how far implementation has progressed.

| Decision | Date | Reason | Impact | Status |
|---|---|---|---|---|
| Google Sheets is the production input source for Shopee `raw` data. | Date not recorded | The business source is maintained in the shared tracker spreadsheet. | Local processing starts from the configured spreadsheet ID and raw tab. | ACTIVE; read access verified. |
| `raw` is read-only. | Date not recorded | Preserve the source of truth and prevent destructive pipeline writes. | The reader uses read-only scope; the writer rejects `raw` and has an explicit output allowlist. | ACTIVE; enforced in code. |
| Use a service account for unattended access. | Date not recorded | Support local and future scheduled execution without an interactive OAuth session. | A service-account credential source is required for Sheet access. | ACTIVE; local access verified. |
| Keep credential files outside the repository; never commit `.env` or secrets. | Date not recorded | Protect spreadsheet access and prevent credential leakage. | Configure an external service-account file path; Git ignore rules cover `.env` and common key formats. | ACTIVE. |
| Default runs are local-output-only; Google Sheets writes require `--write-sheets`. | Date not recorded | Make source processing safe by default and make publication deliberate. | The write scope and guarded writer are reached only through explicit opt-in. | ACTIVE; code and safety tests present, live output write untested. |
| `TARGET_MASTER` is intended to contain validated laptop configurations. | Date not recorded | Provide a stable target identity for later retailer comparisons. | Later scraping and matching should use reviewed configurations, not unstructured listing titles. | PLANNED; current worksheet was found empty and is not consumed by the code. |
| Ranking GMV is M-1 GMV plus current MTD GMV. | Date not recorded | Combine the previous full calendar month with current-month activity. | Each model's period GMV is summed before ranking. | ACTIVE; implemented and tested. |
| Only eligible models present in MTD may rank. | Date not recorded | Keep the target list current and restrict it to laptop candidates. | M-1-only models remain in the model summary but do not qualify. | ACTIVE; implemented and tested. |
| Rank by ranking GMV descending, then MTD GMV descending, then `model_id` ascending. | Date not recorded | Make ties reproducible. | Repeated runs over the same data use stable order. | ACTIVE; implemented and tested. |
| Collapse exact duplicate rows under `(period_month, shop_id, item_id, model_id)`; quarantine conflicting duplicates. | Date not recorded | Avoid double-counting exact repeats and avoid guessing when values disagree. | Exact copies retain one row; conflicts require fingerprint-matched `EXCLUDE_KEY` or `KEEP_SOURCE_ROW` decisions. Unresolved duplicate or model-identity conflicts block publication. | ACTIVE; implemented; the 21 historical conflicts in the latest run have recorded `EXCLUDE_KEY` decisions. Future unresolved or stale conflicts still block publication. |
| Select an MTD price; never average prices. | Date not recorded | Price averaging can create a value that was never observed. | One distinct MTD price is selected; multiple distinct prices are flagged and left unselected. | ACTIVE; implemented and tested. |
| Eligibility is keyword-based with model-level manual `ELIGIBLE` or `EXCLUDED` overrides. | Date not recorded | Provide explainable initial classification and a path for human corrections. | Rules live in `config/eligibility_rules.csv`; local overrides live in `config/eligibility_decisions.csv`; ambiguous or conflicting evidence enters review. | ACTIVE; implemented. |
| Original `TARGET_MASTER` identity key: brand, manufacturer model code, exact CPU/GPU, RAM, storage capacity/type/layout, display size/resolution/refresh rate, and OS edition. | 2026-09-29 | Initial proposal for a stable, evidence-backed identity. | Superseded by the user's later 1:1 identity-key update below; approval workflow, stable IDs, source evidence, and separate Shopee mappings remain active. | SUPERSEDED; see [TARGET_MASTER_SCHEMA.md](TARGET_MASTER_SCHEMA.md). |
| Revised `TARGET_MASTER` identity key: model, screen size, resolution, display type, refresh rate, CPU series, RAM capacity, storage capacity, GPU model, and OS. Require every key field, required brand context, source URL, stable `TGT-000001` IDs, and actual manager/category-owner approval; keep the remaining requested hardware details as descriptive fields. | 2026-09-29 | Initial identity proposal aligned with the then-requested 1:1 comparison fields. | Superseded by the five-core-field policy below. Shopee model IDs remain separate from target IDs. | SUPERSEDED. |
| `TARGET_MASTER` core identity and promotion rule: use model, screen size, CPU series, RAM capacity, and storage capacity as the core key. Missing resolution, display type, refresh rate, GPU model, OS, or source URL does not block a core-complete status or promotion. Retain known secondary details; surface explicit conflicts for review. Promote a candidate once its five core fields are populated and unambiguous, assign/reuse one stable target ID per core key, and map each source model ID to that target. Record `Owner-approved five-core-field rule` as the approval source; no personal name is required. | 2026-10-05 | The user wants a reusable catalog that survives Top 100 changes and a matching process that works when retailers omit secondary specifications. | `MATCH_REVIEW` uses `COMPLETE` for candidates eligible for promotion. Repeated Shopee IDs with the same core key share one target; different core keys get different targets. Missing secondary data does not block matching. Explicit secondary conflicts are retained and flagged instead of silently overwritten. | ACTIVE as product policy. Code, templates, and Sheet validation were synchronized on 2026-10-06 (live dropdown user-reported). See [TARGET_MASTER_SCHEMA.md](TARGET_MASTER_SCHEMA.md) and [PRICE_TRACKING_OUTPUT_SCHEMA.md](PRICE_TRACKING_OUTPUT_SCHEMA.md). |
| `TARGET_MASTER` tracking modes. `CONFIGURATION` keeps the five-field identity, needs all five fields populated and unambiguous before promotion, is the only mode used in cross-retailer exact matching, and may leave `brand` blank when a genuine model/code is verified. `LISTING_ONLY` is only for products confirmed to have no verifiable product model name/code. Its identity is `shop_id` + `item_id` + `source_model_id` (the Shopee variation `model_id`). `model` and unavailable specifications stay blank, and it is tracked as that one listing, never matched across retailers. A blank model alone is not evidence for `LISTING_ONLY`. Never classify by country of manufacture, and never invent "Generic"/"Unbranded" models. Rows without a mode remain `CONFIGURATION`. | 2026-10-08 | Some catalog products have no verifiable model, so the five-field identity cannot apply, yet they still need tracking. Cross-retailer matching without a model would be guesswork. | Six new `TARGET_MASTER` columns and five new `MATCH_REVIEW` columns, appended after `notes`. Mode-specific validation, duplicates, `COMPLETE` rules, mapping checks, matcher exclusion, and coverage statuses. | ACTIVE; implemented locally on 2026-10-08. Live sheet migration applied, and the read-only checks passed on 2026-10-08. |
| `model` and `cpu_series` conventions. If a genuine manufacturer model code is verified, it is the canonical `model`, written as stated with no suffix stripped. The product-family name goes in `series`, and the original wording is preserved in `notes`. Without a verified code, the verified name stays and is reported. Codes are recognized only by an explicit token rule, never taken from titles, and never taken from part numbers. `cpu_series` keeps the tiered family forms (`Core i5`, `Ryzen 7`). Apple chips are `Apple M5` / `Apple A18 Pro`, and untiered Intel lines keep the processor number (`Intel N150`, `Celeron N4020`). `cpu_model` keeps the full processor. The five-field identity and the `CONFIGURATION`/`LISTING_ONLY` separation are unchanged. | 2026-10-08 | The Laptop Factory pilot showed that retailer model labels carry the verified code while the catalog stored family names, so verified products could not match. | Extraction emits the code from labeled model rows. The proposal report covers 199 targets: 28 `READY` model changes, 24 `READY` `cpu_series` changes, 0 duplicate keys created, and 158 unresolved model items. | ACTIVE; extraction implemented 2026-10-08. The 52 READY catalog corrections were applied in the Sheet by the owner on 2026-10-08; the unresolved items remain review backlog. |
| CPU matching is by canonical CPU family. Different processor numbers in one tiered family (`i5-13420H`, `i5-1335U`) both give `Core i5` and may match the same target. Different families (`Core i5`/`Core i7`, Intel/AMD), and different untiered processors (`Intel N100`/`Intel N150`), remain `AMBIGUOUS` when stated as alternatives. The full processor wording stays in `cpu_model` and in extraction evidence. All other matching conditions still apply. | 2026-10-12 | The owner explicitly accepts family-level CPU identity for the five-field key. | Same-family alternatives in one processor field extract one `cpu_series`; no code change was needed. Documentation that said any two processors are ambiguous was corrected. | ACTIVE. |
| The tracker's working list is the **Top 100 verified configurations**, not the overall sales Top 100. Its members are unique ACTIVE `CONFIGURATION` targets with complete, unconflicted five-field identities (`model`, `screen_size_in`, `cpu_series` at family level, `ram_capacity_gb`, `storage_capacity_gb`). They are ranked by the combined GMV (M-1 + MTD) of their eligible, approved mapped marketplace variations, with each `target_id` counted once. `LISTING_ONLY` targets and unresolved candidates are excluded from the list and stay in the catalog and review backlog. Missing secondary specifications do not block selection. A known unresolved conflict excludes the affected source or target even if review status is `COMPLETE`. The source-level sales Top 100 is kept unchanged as a reference. | 2026-10-12 | The source Top 100 includes listings that cannot be compared across retailers (unmapped, `LISTING_ONLY`, or unresolved), and several variations can be one configuration. A configuration-level list counts each comparable product once. | `build_verified_top_100.py` and `audit_verified_top_100.py` (`verified_top.py`), plus a local identity review register. The unresolved original Top 100 IDs become historical backlog, not mandatory members. Phase 5 completion criteria now refer to this list. | ACTIVE; implemented 2026-10-12. First run (2026-10-12): the sales snapshot is Phase 4 run `20260928T112328+0800`, with M-1 2026-08 (full month) and MTD 2026-09 (month-to-date, extracted 2026-09-28); it is historical and was not refreshed. The catalog has 204 targets (192 `CONFIGURATION`, 12 `LISTING_ONLY`) and 359 mappings. Results: 1,379 eligible sources; 244 mapped; 221 contributing; 1,158 excluded (1,135 unmapped, 12 `LISTING_ONLY`, 5 on CPU-conflict targets, 6 with mapped codes differing from their target); 127 qualifying unique configurations (39 with several sources); 100 selected; no shortfall; audit passed. Rebuilt 2026-10-12 after the MSI and register reconciliation (205 targets, 360 mappings): 222 contributing sources; 128 qualifying; 100 selected; audit passed. The owner published the 100 to `P1_TOP_100_CONFIGURATIONS` the same day. |
| Phase 9 starting rules: PHP only, with no currency conversion and no mixing of USD GMV with PHP prices. Reports are weekly, with explicit period boundaries in Asia/Singapore. The price basis is the trusted displayed regular or sale price; conditional vouchers and shipping are separate information and stay unknown when unavailable. Cheapest-price comparisons include only IN_STOCK, non-bundle offers with a valid comparable PHP price. Out-of-stock, preorder, unknown, and bundle observations stay in history with their statuses. Missing prices are blank with a reason, never zero. Monetary precision is exact decimal. The working scope is the selected `target_id` list, and the reusable full catalog stays intact. | 2026-10-12 | The owner supplied these as the starting rules for comparisons and history. | Implemented offline in `price_tracking.py`, `observation_store.py`, and `track_prices.py`. See [PRICE_TRACKING_OUTPUT_SCHEMA.md](PRICE_TRACKING_OUTPUT_SCHEMA.md#phase-9-implementation-2026-10-12). | ACTIVE as starting rules; review after the Phase 10 pilot. |
| Retailer comparisons require exact configuration matching. | Date not recorded | Similar product names can hide differences in CPU, memory, storage, or other specifications. | Future matcher must reject partial or ambiguous matches instead of treating them as exact. | ACTIVE; implemented offline in Phase 8 (2026-10-06) with the five-field `ExactMatcher`. |
| Playwright, retailer scraping, and GitHub Actions belong to later phases. | Date not recorded | Complete and validate raw-data ranking and configuration identity before automation. | No scraper or scheduled workflow is part of the current implementation. | PLANNED; no such components exist in the repository. |

## Extraction evidence approvals (2026-10-13)

Owner instruction: implement the supplied five-rule decision table.

1. Accept an explicit product screen diagonal from its own description or JSON-LD description
   when it clearly describes the product and is consistent with specifications. Keep source
   text and locator. Bare `14.0` cannot provide units.
2. Keep `A14-11M-X3BP OPI` and Nitro OPI unresolved. A manufacturer URL slug is a research
   lead; do not strip suffixes or create aliases without trusted explanatory documentation.
3. Allow shared labeled CPU family/screen only with confirmed selected variant binding and
   complete definitions varying RAM/storage alone, with no conflicting CPU/display evidence.
   Retain `SHARED_PRODUCT` provenance; use selected 16/512 for saved PCC MSI and retain shared 8.
4. Retain trusted model restrictions. Titles, URLs and repeated product names are discovery
   evidence. `LT-ASUS-000261` is a retailer SKU; `90NB1672-M00DD0` needs manufacturer
   corroboration. Labeled `NX.JP3SP.001` is part-number evidence, not an alias or OPI explanation.
5. Octagon storage remains unknown: bare 512 lacks a capacity unit and 16 Gb/s is throughput.

Implemented in the extractor and scoped adapter evidence. Offline HTML replay is 8 EXACT /
4 REVIEW_REQUIRED; original records remain 7/5. Bundle and price eligibility rules are unchanged.

## Listing-scoped owner equivalence and capacity approvals (2026-10-13)

These explicit chat decisions supersede the unresolved-equivalence/unit treatment above only
for the named retailer listings and variants. Manufacturer meaning and verification remain
separate from owner approval.

- DataBlitz Aspire variant 62589224354163: the owner approves `A14-11M-X3BP OPI` as equivalent
  to `A14-11M-X3BP`. Required page part-number evidence is `NX.JP3SP.001`; independently
  extracted guards are Snapdragon X, 14 inches and 16GB RAM.
- Octagon Nitro variant 49704880308480: the owner explicitly approves OPI equivalence and
  this listing's model binding to `ANV15-42-R2UH`, followed by approving GB for its bare 512
  drive amount. Independently extracted guards are Ryzen 5, 15.6 inches and 16GB RAM.
  7640HS versus 6600H remains an underlying processor discrepancy; the CPU-family policy
  still uses Ryzen 5. The page does not explicitly state the missing capacity unit.

`config/retailer_evidence_approvals.csv` records exact retailer/variant URLs, original values,
approved values, guard fields, owner attribution, date and scope. The extractor records
`OWNER_APPROVED` provenance and retains original source evidence; resulting exact matches keep
owner-approval and underlying discrepancy reason codes. Other listings and variants receive no
inference. Explicit conflicting model/storage values and ambiguous quantities are not overridden.
No target IDs are assigned by an approval; all five core fields still go through the matcher.
The approval file and implementation are included in matching-rule fingerprints.

## Decision review notes

- Remote rows in `P1_ELIGIBILITY_DECISIONS` are preserved and merged when the explicit writer runs, but current-run classification loads the local decisions CSV before that step. Use the local file for decisions that must affect a run; review this boundary before treating the sheet tab as the source of current-run overrides.
- `--write-sheets` replaces the generated approved output tabs, updates the manual-decision tab using merge logic, and appends the run log. A write test must use a dedicated test spreadsheet.
- Some Phase 5 behaviors were not covered by the approved decisions. These include links to `INACTIVE` targets, the `review_id` scheme, who reviews mappings, and review rows after promotion. The implementation uses conservative defaults, listed in [TARGET_MASTER_SCHEMA.md](TARGET_MASTER_SCHEMA.md#unspecified-behaviors-and-chosen-defaults). They are implementation defaults, not approved decisions; record them here only when the designated owner decides.
- The Phase 6 scraping framework (2026-09-29) makes several implementation choices. Prices are `Decimal` values and never floats. Observations carry no `target_id`. The transport is the standard library, and retrieval defaults are conservative: 20 s timeout, 3 attempts, 5/10 s backoff capped at 60 s, 10 s per-retailer interval, and a 5 MB response cap. Each adapter declares a host allowlist. These are engineering defaults, not retailer-confirmed policies; see [SCRAPING_FRAMEWORK.md](SCRAPING_FRAMEWORK.md).
- Project-owner decision (2026-09-29): authorize internal automated price collection from retailer websites used by this project, under the assumption that they are approved to be scraped. This is a project-level authorization, not a finding that retailer terms or written permission allow collection. The Laptop Factory external-policy finding remains UNKNOWN, while its project status is OWNER_APPROVED and `live_use_approved = True`. The default throttle is at least 10 seconds between requests per retailer; no retailer-specific rate has been confirmed. Apply this owner policy when adding retailer adapters, while keeping each adapter's explicit host allowlist and maintenance notes. See [RETAILER_DATA_USE_REVIEW.md](RETAILER_DATA_USE_REVIEW.md).
- Phase 7 framework safeguard: the framework blocks live requests unless an adapter explicitly sets `live_use_approved = True`, and follows redirects only after checking the target host. The safeguard records project authorization; it does not establish retailer permission.
- PC Express (PCX) initial review, 2026-10-01: the retailer's published terms of service prohibit systematic or automated data collection, including scraping, without PC Express' express written consent. That contradicted the earlier owner policy's assumption that retailers are approved to be scraped. At initial implementation, the PCX adapter had `live_use_approved = False` and project data-use status RESTRICTED under the default rule in [RETAILER_DATA_USE_REVIEW.md](RETAILER_DATA_USE_REVIEW.md). This was an implementation default pending an owner decision; the later owner decision below supersedes its live-use setting.
- Project-owner retailer-use decision, 2026-10-01: after the PC Express terms were reported, the owner explicitly directed enabling live PCX scraping for this internal tool and reiterated that all retailer websites in the project are to be assumed approved for scraping. This project authorization applies to future retailer adapters as well, including a retailer with a RESTRICTED external-policy finding; record each finding separately. The PCX project status is OWNER_APPROVED and `live_use_approved = True`. Its external-policy finding remains RESTRICTED, and no PC Express written consent has been recorded. The project decision does not represent retailer permission; see [RETAILER_DATA_USE_REVIEW.md](RETAILER_DATA_USE_REVIEW.md).
- PC Central (PCC), 2026-10-01: the `PCC` code was not defined in the repository, and more than one retailer fits it. The user confirmed that `PCC` is **PC Central**, whose storefront is `pccentralcomputerstore.com`. Under the project-wide owner decision above, the PCC project status is OWNER_APPROVED and `live_use_approved = True`. PC Central's published policy pages contain only warranty text, so its external-policy finding is UNKNOWN. The project decision is not PC Central's consent. See [RETAILER_DATA_USE_REVIEW.md](RETAILER_DATA_USE_REVIEW.md).
- PCC implementation default (not an owner decision): a multi-variant PCC product is parsed only from a `?variant=<id>` URL whose variant the page selected; a bare URL fails with `AMBIGUOUS_VARIANT`. See [adapters/PCC.md](adapters/PCC.md).
- Octagon (OCT), 2026-10-01: the Phase 7 request identified `OCT` as Octagon. Octagon's own pages confirm the storefront `www.octagon.com.ph`, operated by Octagon Computer Superstore (owned by Upson International Corp.); the apex redirects there. Octagon's terms of service prohibit using the site or its content to "spider, crawl, or scrape", so the external-policy finding is RESTRICTED. Under the project-wide owner decision above, which covers future adapters with a RESTRICTED finding, the OCT project status is OWNER_APPROVED and `live_use_approved = True`. No new owner decision was made for OCT, and no Octagon written permission is recorded. The project decision is not Octagon's consent. See [RETAILER_DATA_USE_REVIEW.md](RETAILER_DATA_USE_REVIEW.md).
- OCT implementation defaults (not owner decisions): the brand is never taken from JSON-LD or the analytics vendor, because both name the store. Multi-variant products follow the PCC `?variant=<id>` rule. Bare leading-zero `gtin` numbers in the theme's JSON-LD are quoted before parsing. See [adapters/OCT.md](adapters/OCT.md).
- LF implementation default, 2026-10-01 (not an owner decision): `product_request` accepts only `https://laptopfactory.com.ph/product/<slug>/` with no query or fragment. WooCommerce GET queries such as `?add-to-cart=` change cart state, and Laptop Factory's robots.txt disallows them. See [adapters/LAPTOP_FACTORY.md](adapters/LAPTOP_FACTORY.md).
- The five-core matching and promotion rule is an owner-approved product policy. As of 2026-10-06, the local validators support the five required core fields and the `COMPLETE` review status, and Phase 8 matching runs offline from saved observations and a validated target snapshot. The capacity parser was tightened after review to route repeated or multi-component amounts to review; 763 synthetic tests pass. The user reported on 2026-10-06 that the live tabs pass the read-only validator and that the status dropdown allows `COMPLETE`. No real saved observation has been matched yet, and 37 accepted Top 100 candidates remain in human review. *(Superseded 2026-10-12: nine saved Laptop Factory observations now match 9 `EXACT` against the 205-target catalog, and the working list is the published Top 100 verified configurations.)*
- Phase 9 implementation defaults, 2026-10-12 (not owner decisions; review in the pilot):
  - **Weeks.** Monday 00:00 to Monday 00:00 in Asia/Singapore. Every run needs an explicit `--as-of` cut-off.
  - **Representation and selection.** Each offer is represented by its latest observation in the week. Price ties go to the latest retrieval, then offer URL, offer index, and observation ID.
  - **Fail-closed comparability.** A blank (unchecked) `bundle_flag` or an unknown price type is not comparable.
  - **Staleness.** No carry-forward; earlier prices appear only as stale references.
  - **Identity and storage.** Observation identity is retailer + offer URL + offer index + retrieval time, and conflicting content refuses the batch. The store is append-only with indefinite retention.
  - **Matching contexts.** A context is the catalog SHA-256 plus the rule version and a fingerprint of the rule modules.
  - **Shopee fields.** `Item_ID`/`Model_ID`/`Shop_ID` and the Shopee price are kept per source in a ledger instead of choosing one listing.
- Manual collection implementation defaults, 2026-10-12 (not owner decisions):
  - **Manifest.** Header `request_id,retailer_id,product_url,expected_target_id,enabled`, exact text, `enabled` `TRUE`/`FALSE`. The whole file is refused on any issue, and duplicate pages are refused.
  - **Mode.** `--preview` or `--live` must be chosen; preview never opens the store.
  - **Expected target.** A hint only. Outcomes are labelled `AS_EXPECTED`, `DIFFERENT_TARGET`, or `NOT_EXACT`, and the last two go to review.
  - **Exit codes.** `0` success or preview, `2` partial success, `1` failure. A run with no valid observation stores nothing.
  - **Run directories.** A new directory per run that is never reused. Collected observations go to `data/history/price_history.sqlite3` unless `--store` is given.
- Pilot Sheets publication, 2026-10-12. The owner authorized a manual publisher and the first publication of the seven-configuration pilot to four dedicated tabs. Implementation defaults (not owner decisions):
  - **Destination.** An exact four-tab allowlist; existing tabs with foreign content are refused.
  - **Content.** Full content replacement per publication. Run-log rows are keyed by a publication ID derived from input fingerprints, so a repeat updates its row and attempt count.
  - **Cell encoding.** Literal string values, PHP currency numbers, and cell links on full URL text (no formulas).
  - **Links.** Comparison links come only from the selected offer or the current-week exact offer. History and review show non-exact pages as unlinked text.
  - **Status.** `SUCCESS` only after a full readback.
  - **Backups.** Local backups of existing publisher tabs replace the legacy whole-spreadsheet copy, because the Sheets-only service account cannot copy files.
  - **Timeouts.** HTTP (15 s connect, 90 s read/send) per request.
- New decisions should be added to this log with their actual decision date. Do not infer dates from file timestamps or this documentation date.

## Out-of-stock comparison policy (2026-10-13)

During the fresh pilot, the owner explicitly requested that products with no stock remain
comparable. Valid displayed prices from exactly matched IN_STOCK and OUT_OF_STOCK offers now
participate in retailer and lowest-price comparisons. Preserve actual availability and show
OUT OF STOCK beside any selected sold-out price. No stock-based preference is introduced;
the existing lowest-price selection applies. This supersedes only the OUT_OF_STOCK exclusion
in the Phase 9 starting rules. PREORDER/UNKNOWN, bundles, invalid/missing prices and unresolved
configuration matches remain excluded. Original observations and old report artifacts are kept.

## Balanced matching policy (2026-10-15)

Source: the owner's policy review and implementation instructions supplied on 2026-10-15
("Laptop matching policy review", prepared 15 October 2026). Rules version
`five-field-exact/balanced-evidence/2026-10-15`; adapters `oct-3` and `pcc-4`; publisher
`pilot-sheets-2`. Details are in [MATCHING.md](MATCHING.md#balanced-matching-policy-2026-10-15).

Owner decisions:

- **Five fields stay required.** No similarity score, four-of-five matching, fuzzy model
  correction, or copying values from the target, search query, or sibling pages.
- **Title model corroboration.** One complete model code in the product's own title may supply
  `model` when the other four fields are independently supported outside the title, no page,
  URL, or identifier states a conflicting model, and the selected scope is clear. A title and
  its JSON-LD name are one assertion. Recorded as `TITLE_MODEL_CORROBORATED`.
- **Duplicates and arithmetic.** Equivalent repeated facts are one fact. Explicit installed RAM
  components and installed drives are summed, keeping their breakdown and media; maximums,
  slots, speeds, alternatives, optional and external parts never supply capacity. 1 TB = 1000 GB.
- **CPU and screen.** An isolated `i5` in a labeled CPU field is `Core i5`; Core i5, Core 5, and
  Core Ultra 5 stay different. Explicit units (including a glued `in`, or a unit in the label)
  supply inches; a bare `Size: 14.0` does not.
- **Variants.** Confirmed selected capacity overrides shared defaults with a warning. A full model
  code in a shared description is inherited only under the complete RAM/storage-only matrix
  already required for shared CPU and screen.
- **Secondary severity.** Verified brand, display, GPU/VRAM, memory-type, storage-media,
  model-code, and part-number conflicts block. Color, OS edition, and processor-number differences
  within the accepted family are warnings. Missing values never conflict.
- **Prices.** Out-of-stock prices stay comparable (2026-10-13 decision). The inclusive minimum is
  labeled "lowest observed"; a separate "lowest in-stock" minimum uses the same comparable set.
- **Approvals unchanged.** The three listing-scoped approvals keep their exact URL and guard
  scope; no general OPI alias or unitless-capacity rule.

Implementation defaults (not owner decisions; open to change):

- ASUS part numbers (`90N?????-M?????`) are the only documented part-number shape excluded from
  title model codes. Other regional or package numbers are not assumed equal or different.
- A product URL path naming the same code stem with a different final segment (`anv15-42-r2auh`
  for `ANV15-42-R2UH`) blocks the title route and shared-model inheritance; for a trusted labeled
  model it is a warning only (owner precedent: an incorrect retailer slug is not the displayed
  code).
- A page model row with only a less specific designation (`Thin 15 B13UC`, `FA506NCQ`) does not
  conflict with a full title code whose segment it equals; a different designation (`FA506NC`)
  does.
- Bundle detection is unchanged. Free accessories (backpack, printer, monitor) still exclude the
  price; included Office/software and warranties were never bundle triggers.

## Bundle prices included with a note (owner approval, 2026-10-15)

The owner requested that bundle offers remain comparable, with a note identifying the bundle.
This supersedes the earlier bundle-price exclusion, including the balanced policy default above.
An otherwise eligible exact configuration with `bundle_flag = TRUE` now participates at its
displayed package price in retailer selections, lowest observed, lowest in-stock, history and
price-change calculations. `BUNDLE_OFFER` is a nonblocking price note, not a matching warning or
price-exclusion reason. The program does not estimate or deduct the value of included items.

Keep bundle detection, original flags and evidence. Comparison CSV warnings and selection
`price_notes` identify bundles; the Sheet's retailer status and winning-minimum retailer labels
show "bundle offer". Unknown bundle status remains excluded. Five-field identity, scope,
currency, stock, known price type and valid-price requirements continue to apply. Existing saved
reports retain their historical policy; regenerating them records comparison policy
`displayed-price/stock-and-bundles/2026-10-15`. No retailer recollection is required to apply this
change to stored observations.

## Discovery, evidence scope, URL slugs, coverage and purchase terms (requested 2026-10-19)

Requested revision, implemented locally; owner acceptance after a real-data replay is pending.

- **Discovery.** Search the complete model code first, without specification filters; continue
  through fallbacks (part number, code with RAM/storage, family, family with specifications)
  while the results hold only other configurations or unresolved candidates. Earlier `EXACT`
  offer URLs are re-checked as seeds. Search results stay candidates; target values never fill a
  missing specification.
- **URL slugs.** Once page evidence establishes a consistent model, a differing slug is a warning
  (supersedes the 2026-10-15 implementation default that blocked title and shared-model routes).
  No alias; genuine page-claim conflicts still block.
- **Evidence scope.** Family capabilities, maximums and optional configurations never stand for
  the installed configuration (supersedes the earlier `AMBIGUOUS` result for
  `8GB, expandable to 32GB`). Unexplained suffixes on model-name rows are no longer dropped.
- **Purchase terms.** Supersedes the 2026-10-15 "every confirmed bundle comparable" default:
  ordinary inclusions remain comparable with a note; paid packages, conditional and restricted
  prices, and bundle wording without stated terms are excluded with their reason. The earlier
  rule remains selectable (`--offer-terms-policy bundle-flag/2026-10-15`) and the effect is
  reported separately in `offer_terms_effect`.
- Unchanged: five required fields, no fuzzy/four-of-five matching, no suffix removal, exact screen
  sizes, explicit capacity units, catalog/mappings/approvals untouched, stock eligibility and the
  two minimums.

## Pilot acceptance fixes: lineage, continuation, supplied URLs, DataBlitz (2026-10-19)

Requested in the pilot acceptance review; implemented locally and replayed on the reviewed run's
saved evidence. Owner acceptance is pending.

- **Coverage lineage.** An offer explains a configuration/retailer pair only through recorded
  lineage: matched to it, collected by a manifest request made for it, selected for collection as
  its search candidate (a page evaluated for two configurations keeps both relations), or a
  matcher near-match of the same model. A search result rejected for the configuration
  (`DIFFERENT_MODEL_CODE`, `UNRELATED`, `FAMILY_ONLY`) stays in `coverage_offers.csv` as
  `REJECTED_SEARCH_RESULT` for audit and never explains the cell, even when the same page was
  collected for another configuration. Requested (`expected_target_id`) and confirmed
  (`assigned_target_id`) targets stay in separate columns; offer rows carry the `request_id`.
- **"Same model, different configuration"** is reserved for RAM/storage options of one
  manufacturer model code (or any field of a family-name model such as `MacBook Air`). The same
  code stating another screen size or processor family is an `IDENTITY_CONFLICT` (an unresolved
  specification discrepancy; no tolerance, no catalog change). Another model collected for the
  configuration is `DIFFERENT_MODEL`. Searches that returned only other models are
  `NO_RELEVANT_CANDIDATE`; searches that returned nothing stay `NO_CANDIDATE_FOUND`.
- **Blank cells.** With `track_prices.py --discovery-dir` (a discovery of the report week only),
  a cell with no related offer takes the week's recorded search outcome (`NO_RELEVANT_CANDIDATE`,
  `NO_CANDIDATE_FOUND`, `SEARCH_FAILED`, `FETCH_FAILED`, `CANDIDATE_NOT_FETCHED`,
  `DISCOVERY_NOT_ATTEMPTED`) with its explanation, using the same rule as `coverage.csv`; offer
  outcomes keep `CANDIDATE_NOT_CONFIRMED` (identity conflict, extraction failure, other
  configuration) and `NO_COMPARABLE_OFFER` (excluded price). Unconfirmed amounts never become a
  price or a minimum. Publication accepts these statuses.
- **Discovery stopping policy.** A plausible title stops the fallback searches only until
  collection decides. If no collected candidate of the job is an exact match for the job's own
  configuration, the job resumes its remaining query tiers in the next round (at most
  `--max-discovery-rounds`, default 3), skipping pages already evaluated, within the same
  request limits. The job ends `SUITABLE_OFFER_FOUND` (identity only: the price may still be
  excluded), `QUERY_PLAN_EXHAUSTED`, `BUDGET_EXHAUSTED` (request, query or round limit),
  `PROVIDER_FAILURE`, or `SEARCH_INCOMPLETE_REPLAY_ONLY`. A match to another configuration remains
  valid for that configuration and never completes the initiating search.
- **Supplied URLs.** Known pages go in `config/supplied_candidate_urls.csv`. They are discovery
  seeds, never approvals, matches, aliases or target assignments: normalized without tracking
  parameters (variant kept), accepted unchanged by the adapter, assigned to a scoped job by an
  explicit search hint or by the complete model code in the URL handle, and checked directly
  (bounded) when native search misses them, which is recorded as incomplete search coverage. A
  `--seed-urls` file must now state `match_status`; a file without it no longer seeds every row
  as a historical exact match.
- **DataBlitz.** A maximum stated inside one flattened row after the installed amount
  (`DDR4-3200 8GB RAM Max up to 64GB 2 Slots`) is split off as `ram_maximum_gb`; the installed
  amount is kept. Limits inside a breakdown, lone limits and two installed amounts are unchanged
  (inseparable, not installed, ambiguous). Reviewed labels `Standard Memory`,
  `Storage Capability` and `Storage Capacity` map to memory/storage. A code written after an
  accessory noun in a bundle-item segment that does not name a laptop (`+ Acer Entry Run Rate
  Backpack E-1620-P`) is an `accessory_identifier`, never a laptop model code; two laptop codes
  still need review, and title models still need the four independent fields.
- Unchanged: five required fields, exact screen sizes, no fuzzy or four-of-five matching, no
  suffix stripping, catalog/mappings/approvals untouched, stock eligibility, bundle notes, the two
  minimums, Asia/Singapore Monday weeks, and no carry-forward of earlier-week prices.

## Catalog identity, title evidence and recheck links (second review, 2026-10-19)

Requested after the top-100 run `top100_supervised_20261019T052548Z` (30 price cells, 16
configurations priced). Matching stays five-field exact; nothing below weakens it.

- **Identity kinds.** A catalog `model` is a manufacturer model code, a reviewed marketed product
  name (`config/product_name_registry.csv`, e.g. `MacBook Neo`, `MacBook Air`, `MacBook Pro`) or a
  broad family name (`IdeaPad`, `Aspire`, `Aspire Lite`, `Victus`, `Vivobook`). An unreviewed name
  counts as a family name. Each registry row states its evidence and review date; the registry is
  part of the matching-rules hash. A family name cannot identify one product (several coded
  products share its five fields), so no offer is ever confirmed for a family-name target: such a
  cell is `CATALOG_IDENTITY_UNRESOLVED` and points at the catalog reconciliation.
- **Product-name presentation.** For a registered product name only, a leading repeated brand and
  a registered colour in parentheses are presentation (`Apple Macbook Neo (Citrus)` →
  `MacBook Neo`, recorded as `MODEL_NAME_PRESENTATION_NORMALIZED` with the original text kept).
  Nothing else is removed: no suffix stripping, prefix matching, joining or fuzzy comparison, and a
  value containing a code keeps the code rules. `MacBook Neo 13-inch`, `MacBook Neo Pro`,
  `MacBook` and `MacBookNeo` are not `MacBook Neo`. The CPU-family policy is unchanged; Apple chip
  modifiers are identity (`A18` ≠ `A18 Pro`).
- **Title evidence (supersedes the 2026-10-15 rule that a title could only corroborate).** A
  product title's explicit statements are matching evidence for screen, CPU, RAM and storage,
  including the joined RAM+storage form (`8GB+256GB`, `16GB / 1TB`: at most 128GB, then at least
  128GB or a TB amount; two memory or two storage amounts do not qualify). A clear title statement
  supplies a field the specifications do not state (`TITLE_SUPPLIED_<FIELD>`, scope `TITLE`, the
  raw title kept as the evidence). A title statement that contradicts the specifications makes
  the field `AMBIGUOUS` (`TITLE_SPEC_CONFLICT_<FIELD>`), routed to review. The title of a page
  whose specifications are shared by several variants describes the parent product and never
  supplies or overrides the selected variant. A title model (a code, or a registered product name:
  `TITLE_PRODUCT_NAME_CORROBORATED`) still needs the four other fields established; they may now
  come from the title. URL wording never establishes a field, and nothing is ever filled from the
  initiating target.
- **Catalog reconciliation (`reconcile_catalog.py`).** For each selected target whose model is a
  name, the target's own contributing source listings decide. One consistent manufacturer code, no
  contrary specification and no other target with that code → `SUPPORTED_CORRECTION`, applied
  only in a local DIAGNOSTIC snapshot and selected list (target ID and mappings kept; the former
  name moves to `series` and `notes`). Otherwise `DUPLICATE_OF_EXISTING_TARGET`,
  `MULTIPLE_PRODUCTS`, `CONFLICTING_SOURCE_SPECS` or `INSUFFICIENT_EVIDENCE`, each with an owner
  action. Retailer offers with the same specifications are listed only as leads, never as
  evidence; refurbished seller descriptors (`X280-i5`, `L570-i5-7th-8G`) are not manufacturer
  codes; one code supported for two targets is never applied. No alias is created and the Sheet is
  never written: the owner applies corrections in `TARGET_MASTER`.
- **Discovery relevance.** Code targets search codes first; product-name targets search the name
  and the name with specifications; family-name targets search only the name and name+specs to
  gather review leads (`FAMILY_NAME_LEAD_CATALOG_UNRESOLVED`, never collected). Code-spelling
  rules apply only to code targets. Accessories are rejected from the listing before any
  inspection (`REJECTED_ACCESSORY`). Identical query texts (any case) are sent once and the
  response cache spans all rounds, so the search limit is cumulative and a resumed search never
  resends a page. `SEARCHES_EXHAUSTED` is renamed `QUERY_PLAN_EXHAUSTED`; `BUDGET_EXHAUSTED` now
  means only a request, query or round limit. A result page is flagged `RESULT_CAP_REACHED`
  (Shopify predictive search returns at most 10 results and has no pagination) or
  `MORE_PAGES_NOT_READ`. A single named-variant Shopify page is collected at its `?variant=` URL;
  DataBlitz `ProductGroup.hasVariant` JSON-LD is read.
- **Recheck links.** A blank comparison cell lists the relevant pages that did not become a
  confirmed comparable offer (fetched but unconfirmed, fetch failed, listed but not fetched, at
  most three family-name leads), most useful first, each with a short reason ("Different
  configuration — 256GB storage; target requires 512GB", "Not fetched — collection budget
  reached", "Matches another target: TGT-…"). The exact variant URL is kept; stock and time are
  shown only when observed. Accessories, unrelated results and other models are never listed. A
  recheck amount is labelled unconfirmed and never fills a price cell, the comparable-retailer
  count or a minimum. Files: `recheck_candidates.csv` (every link) and
  `{code}_Recheck_Count`/`{code}_Recheck_Note` in the comparison. Sheets publisher
  `pilot-sheets-3` adds "recheck link (unconfirmed)" and "recheck note" columns per retailer to
  `PILOT_PRICE_COMPARISON` (filled only beside a blank price, with "+N more in
  PILOT_PRICE_REVIEW") and "Recheck candidate (unconfirmed)" rows to `PILOT_PRICE_REVIEW`.
- **Extraction repairs.** `LPDDR5X 16GB` no longer reads as 80GB (a capacity must not follow a
  letter or digit); curly-quote inch marks are read; DataBlitz `Chip`, `CPU and chipset`,
  `Panel Size`, `Panel Details`, `Display Size`, `Internal Storage`; Octagon `Display Size`,
  `Panel Size`, `Internal Storage` and the `Expansion Slots` heading; PC Express `Processors`. PC
  Express and PC Central compare percent-escaped handles decoded (`pcx-3`, `pcc-5`).
- Unchanged: five required fields, exact screen sizes, MSI 8GB/16GB kept separate, the actual
  target independent of the searched-for target, PCX TUF and Octagon Swift conflicts kept, the URL
  slug warning, bundle and out-of-stock labels, the in-stock minimum, no carry-forward of history,
  Asia/Singapore Monday weeks.

## Independent review fixes (2026-10-19)

Review: `data/output/independent_review_2026-10-19/REVIEW.md`. Validation:
`data/output/review_fixes_2026-10-19/REVIEW_FIXES_REPORT.md`. This section tightens the second
review's title-evidence rule above; nothing else in it changes.

- **Title capacities keep their structure** (`extraction._title_capacities`). Every amount in a
  title is read with its role before anything is deduplicated: RAM (RAM/memory/DDR words), storage
  (SSD/HDD/eMMC/UFS/storage), or not installed (external/portable/USB drive, or free/bonus/extra/
  optional wording before it). Maximum and optional clauses are removed first. A breakdown after
  an amount (`16GB (2x8GB)`) is accepted only when it adds up. `+`-joined components of one role
  are summed under the established component rule (`512GB SSD + 512GB SSD` = 1024;
  `16GB RAM + 16GB RAM` = 32), never collapsed. Several separate amounts of one role
  (`512GB SSD / 1TB SSD`, a repeated `16GB RAM`), an inconsistent breakdown, or an unlabeled
  amount joined to a labeled one (`8GB/16GB RAM`) make the field `AMBIGUOUS` (review, no target).
  The joined `8GB+256GB` form is kept only for an unlabeled amount up to 128GB joined to an
  installed one of at least 128GB, and not as part of a longer list. External and extra amounts
  are kept as supporting evidence (`TITLE_NOT_INSTALLED_<FIELD>`).
- **Secondary claims in accepted titles.** An unshared page title's explicit GPU model (RTX/GTX/
  MX numbers, Radeon RX), panel type (IPS, IPS-Level, OLED) and resolution (numeric, or the
  precise VESA names WUXGA and WQXGA) are supplied like core title statements, so the matcher's
  existing blocking conflict policy sees them. They are never mandatory: an unstated value stays
  unknown, and a target without the value is not compared. A title contradicting the page's own
  rows makes the field `AMBIGUOUS` (`TITLE_SPEC_CONFLICT_<FIELD>`); IPS-Level in one place and IPS
  in the other is not a contradiction. Several different title values stay supporting only. A
  shared parent title supplies nothing. GPU models are compared in a canonical form
  (`RTX4050` = `RTX 4050` = `GeForce RTX 4050 Laptop GPU`).
- **Resolution names.** Only WUXGA (1920x1200) and WQXGA (2560x1600) establish a resolution from a
  name. FHD, Full HD, FHD+ and QHD are used loosely by retailers (PC Express writes
  `Full-HD WUXGA`), so they are kept as supporting `resolution_name` evidence with their nominal
  size. HD, 2K, 3K and UHD are not read.
- **Offers that disagree.** When the offers chosen for one configuration state different
  resolutions, panel types or GPUs, every one of those cells gets the warning
  `OFFERS_DISAGREE_<FIELD>` and the review queue gets one row per offer listing each retailer's
  statement (a loose name shown as nominal). Nothing decides which source is right; prices,
  counts and minima are unchanged. This surfaces the PC Express `14" Full-HD IPS` wording for
  AL14-45P-R10U against DataBlitz/Laptop Factory WUXGA 1920x1200 (the catalog records no
  resolution for TGT-000182), and Laptop Factory's `WUXGA (1920x1200) OLED` for SF14-51-553D
  against DataBlitz/Octagon 2880x1800.
- **URL paths** (`url_paths.normalized_path`; `pcx-4`, `pcc-6`). Paths compare in RFC 3986 normal
  form: escape hex digits in upper case, escaped unreserved characters decoded, literal non-ASCII
  encoded. An escaped `/` (`%2F`) is never a separator, `%25` is never decoded twice, path case is
  kept, and queries (variant IDs) are not part of the comparison. `pcx-3`/`pcc-5` decoded every
  escape and could merge distinct products.
- **Reconciliation duplicates.** A duplicate needs the same complete identity (code, screen, CPU
  family, RAM, storage) and no material secondary difference (the matcher's blocking fields); the
  same identity with such a difference is `SAME_CORE_SECONDARY_CONFLICT`. The same code with
  another RAM/storage configuration is a different configuration, flagged for information only.
  Proposals are grouped the same way. Duplicate actions say owner review: nothing is merged.
  `INSUFFICIENT_EVIDENCE` means unresolved under the current evidence and parser, not
  permanently unpriceable.
- **Family-name leads** are ranked by evidence: a title naming the target's own recorded
  manufacturer code or part number first (a catalog field, never a confirmed match), then more
  agreeing title clues. A bare number in screen-size range without a unit that differs from the
  target's screen is shown as unverified ("title has '16' without a unit (target screen 14";
  not a verified screen size)") and ranks the lead lower; it is never read as inches.
- Unchanged: five required core fields, title statements may supply missing core fields, A18 and
  A18 Pro distinct, LPDDR5X not a module count, installed vs maximum capacity, DataBlitz Neo 256GB
  to TGT-000003 only, MSI 8GB not on the 16GB target, PCX TUF and Octagon Swift conflicts,
  approved bundles comparable and labelled, out-of-stock never the in-stock minimum,
  Asia/Singapore weeks.

## Listed-spec comparison for broad-family targets (2026-10-20)

Intentional policy change: a missing manufacturer model code in Shopee no longer prevents a
useful price comparison. Code: `src/laptop_tracker/listed_spec.py`.

- **Three outcomes.** *Exact product match*: a specific identity is established and the required
  specifications match (unchanged rules; model codes and reviewed product names such as MacBook
  Neo). *Listed-spec match*: the target's catalog model is a broad family name (`IdeaPad`,
  `Aspire Lite`, `Victus`, any unreviewed name), so no specific identity is available, but the
  brand, family and required listed specifications match. *Review / different configuration*:
  required evidence is missing, conflicting or different. A listed-spec match is never labelled
  an exact product match.
- **Requirements**, all from the offer's own evidence (nothing is filled from the target):
  - The target records a brand. The page's brand field, or the brand as a whole word in the
    title, matches it. A brand field naming another brand without the target brand in the title
    is a mismatch.
  - The family name appears in the title. Case, spacing and punctuation are presentation, and a
    joined spelling (`Idea Pad`/`IdeaPad`) is the same name. There is no prefix or fuzzy
    matching.
  - Screen size, CPU series (the approved family forms; `Core i5`, `Core 5` and `Core Ultra 5`
    stay distinct; Apple chip modifiers are kept), installed RAM and installed storage are all
    extracted and equal.
  - No contradictory identity evidence: conflicting model claims, several model codes, or an
    ambiguous model.
  - A manufacturer code the target records (`manufacturer_model_code`) is an identity
    requirement. Another code in the title, or another designation as the page's model, excludes
    the offer; the fallback never bypasses it.
  - No material secondary specification the target records (the matcher's blocking fields: GPU,
    panel, resolution, part number, ...) is contradicted or made ambiguous. A field the target
    does not record stays unknown and never blocks.
- **Different processor numbers within the accepted family** stay eligible. The actual numbers
  are reported and a difference from the target's recorded `cpu_model` is the warning
  `CPU_NUMBER_DIFFERS_FROM_TARGET_RECORD`.
- **Ambiguity rule.** Targets with a verified secondary conflict are excluded first. The most
  specific family name stated in the title wins (`IdeaPad Slim 5` over `IdeaPad`), and the
  broader targets are recorded. If several equally specific targets remain, the offer is
  `LISTED_SPEC_AMBIGUOUS`: it prices none of them and every association goes to review. The
  target a search was made for is never preferred.
- **Associations, not assignments.** The stored match (and any exact assignment to a code target)
  is never rewritten. The listed-spec association is reported beside it: the history columns
  `listed_spec_status`, `listed_spec_target_ids`, `listed_spec_ambiguous_target_ids`,
  `listed_spec_comparability` and `listed_spec_comparability_reasons`. One offer can be an exact
  match for a code target and a listed-spec match for a family target at the same time.
- **Prices.** A listed-spec match prices the retailer cell under the same price rules (stock,
  purchase terms, PHP, price type), only when that retailer has no comparable exact offer for
  the target. Each retailer counts once, and both minimums include it. The cell's match basis is
  `LISTED_SPEC` and its warnings start with `SPECIFIC_IDENTITY_UNRESOLVED`. `Offer_Details` keeps
  the offer's actual model, processor, GPU, panel and resolution. Minimums that include
  listed-spec prices are labelled "Lowest price matching listed specs" and "Lowest in-stock price
  matching listed specs", with the winning offer's basis (`EXACT_PRODUCT`, `LISTED_SPEC`, `MIXED`).
  Cross-retailer secondary differences (`OFFERS_DISAGREE_<FIELD>`) apply unchanged.
- **Discovery.**
  - Family targets search brand, family and specifications as before.
  - A listing stating the brand and family (`FAMILY_LISTED_SPEC_CANDIDATE`) is ranked and
    collected within the per-retailer limit; incomplete titles are still collected.
  - A listing stating the target's own recorded code ranks first. One stating another code is
    not collected for it.
  - A listing whose title states another configuration is not fetched, and does not end the
    search.
  - Non-laptops (desktops, all-in-ones, mini PCs, handhelds, tablets, routers, monitors,
    printers, components) are rejected before any request (`REJECTED_PRODUCT_TYPE`), as are
    accessories.
  - A collected listed-spec match is a suitable offer for the job. A listing without the brand
    stays a review lead.
  - Result caps, pagination and budgets are reported as before.
- **GPU rows.** A labelled graphics row ("Graphics", "GPU", "Graphics Card", ...) now supplies
  the discrete GPU model for every retailer (canonical form), so a target's recorded GPU can be
  checked. Integrated graphics are not read and stay unknown.
- **Shared parent titles.** Their secondary hardware (GPU, panel, resolution) applies to the
  selected variant only when the variants are proven to differ in RAM and storage alone. Shared
  titles never supply RAM or storage.
- **Coverage.** `LISTED_SPEC_PRICE_AVAILABLE` ("Listed-spec price available; specific identity
  unresolved") and `LISTED_SPEC_PRICE_EXCLUDED` sit beside `PRICE_AVAILABLE` ("Exact-product
  price available"), with a `price_basis` column. Family cells without a qualifying offer give
  the specific reason (different configuration, required specifications missing, conflicting
  evidence, another family). `CATALOG_IDENTITY_UNRESOLVED` is used only when listings name the
  family without the brand.
- **Sheets (`pilot-sheets-4`).**
  - A listed-spec status reads "Comparable price; listed-spec match — brand, family, screen, CPU
    series, installed RAM and storage agree; specific Shopee model unknown", followed by the
    offer's own details.
  - New columns: "Lowest observed price basis", "Lowest in-stock price basis" and "Comparison
    basis". The pilot-sheets-3 header is still recognised.
  - Validation accepts a listed-spec price only from a comparable listed-spec association of
    that observation.
- **Catalog reconciliation** stays useful (specific identities make exact matches), but it is no
  longer a prerequisite for a broad-family comparison. Catalog corrections are not applied.
