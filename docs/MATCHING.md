# Phase 8: Exact offer matching

**Status (2026-10-06):** COMPLETE as an offline path. Retailer-specific extraction, the five-field
exact matcher, traceable results, and a review queue run locally from saved observations and a
`TARGET_MASTER` CSV snapshot, or, with `--target-master-from-sheets`, the live `TARGET_MASTER`
tab read with the read-only scope. Nothing in this path fetches retailer pages, writes to Google
Sheets, reads `raw`, creates targets, or creates `SOURCE_MODEL_MAP` rows. It has been exercised only on
synthetic fixtures; see [Limitations](#limitations).

```text
adapter OfferObservation ──► extraction.py ──► ExactMatcher ──► match_offers.py reports
 (raw labeled spec rows)     (canonical fields   (validated        (results, review queue,
                              + evidence)          TARGET_MASTER)    field evidence)
```

## Five-field core identity

An offer can match only when all of these fields have explicit values:

```text
model
screen_size_in
cpu_series
ram_capacity_gb
storage_capacity_gb
```

The same comparison rules are used by the matcher and by the `TARGET_MASTER` duplicate check
(`src/laptop_tracker/normalization.py`), so a snapshot that validates cannot hide two targets the
matcher would treat as one:

- `model` and `cpu_series`: Unicode NFC, `®`/`™`/`℠` removed, whitespace collapsed, case-folded.
  Punctuation, symbols, and digits are preserved.
- `screen_size_in`: a positive bare decimal compared by value (`14`, `14.0`, and `14.00` are equal).
- `ram_capacity_gb` and `storage_capacity_gb`: positive bare integers in GB (`1 TB = 1000 GB`).
  The matcher does not strip units: `16GB` is invalid input, not `16`.
- Missing core values never compare equal. Core values are never fuzzily compared. Since
  2026-10-15 a title can supply only a complete `model` code, and only under the corroboration
  rules in [Balanced matching policy](#balanced-matching-policy-2026-10-15); screen, CPU, RAM,
  and storage never come from a title.

## Retailer extraction (`src/laptop_tracker/extraction.py`)

Each retailer extractor reads only the adapter's labeled `(label, value)` rows in
`OfferDetails.raw_specifications`, plus the structured `brand` and `manufacturer_part_number`
detail fields. Since 2026-10-13 it also reads the adapter's scoped evidence
(`OfferDetails.scoped_evidence`), but only a *bound* selected variant can supply a matching value
(see [Multi-variant pages](#multi-variant-pages)); everything else in it is supporting evidence. It
never parses `raw_configuration_text`, variant option titles, unlabeled lines (headings, `✅ …`
bullets), or URLs for a matching value. `raw_title` is read for one corroborated model code only
(2026-10-15) and otherwise only for conflict checks; URLs are conflict checks only.

Labels are matched case-insensitively. Only labels seen in each adapter's reviewed pages and
synthetic fixtures are mapped; any other label is ignored.

| Retailer | `model` | `screen_size_in` (+ display secondaries) | `cpu_series` | `ram_capacity_gb` | `storage_capacity_gb` |
|---|---|---|---|---|---|
| Laptop Factory | `Model Name` | `Display Size` | `Processor` | `RAM` | `Storage` |
| DataBlitz | `Model` (code rule) | `Display`, `Size`, `Display Panel/ Resolution` | `CPU`, `Processor` | `Memory`, `Total System Memory` | `Storage` |
| PC Express | `Model` (code rule) | `Display` | `Processor` | `Memory` | `Storage` |
| PC Central | `Model` | `Display` | `Processor`, `CPU` | `Memory` (selected variant: `RAM`, `Memory`) | `Internal Storage`, `Storage` (selected variant: `SSD`, `Storage`, `HDD`) |
| Octagon | `Model` (code rule) | `Display` | `Processor`, `CPU and chipset`, `CPU` | `Memory`, `RAM` | `Storage` |

Labels added on 2026-10-15 (`Total System Memory`, PC Express `Model`, Octagon `Model`/`CPU`/
`RAM`) were seen in the saved 2026-10-15 pilot pages. Extend a label set only after reviewing real
adapter output for that retailer. A `Model`, `Model Name`, `Model Number`, or `Model No.` row on
any page is also read as a *model claim*: it never supplies a value outside a retailer's trusted
labels, but it can conflict with a title model code.

**Paragraph layouts (PC Central, Octagon; 2026-10-13).** These descriptions are paragraphs and
line breaks, not tables. A recognized heading (a field label above, or a listed section heading
such as `Graphics`, `Audio`, `Wireless and networking`) opens a group; the lines that follow
belong to it until the next recognized heading. A heading may carry its value on the same line
(`Memory: 16GB`, `CPU and chipset1 AMD Ryzen 5 6600H`) and may carry footnote numbers glued to it
(`Display4`, `Memory1, 2, 3`, `Storage1, 5`); footnotes are removed only from a recognized
heading, never from values (`16:9`, `1x 16GB` stay intact). Lines before the first heading and
unrecognized lines belong to no group. No position, product code, or ID is hardcoded.

### Value rules

| Field | Rule | Canonical form |
|---|---|---|
| `model` | The labeled value with trademark marks removed. If it contains exactly one model-code token ([conventions](TARGET_MASTER_SCHEMA.md#model-and-cpu-series-conventions-2026-10-08)), that code is the value as written: case and suffixes are kept, and brand, family words, and a color in parentheses are dropped. With no code token, the stated name is kept. Two or more distinct codes are `AMBIGUOUS`. The full label stays in the evidence. | `ANV15-42-R2UH`; otherwise the stated name |
| `screen_size_in` | A number followed by `"`, `″`, `”`, `''`, or `inch`, or by `in` glued to it (`14.5in`; 2026-10-15; a spaced `14 in` is not a unit). A display label that states its unit (`Screen Size (inches)`, `(in)`) makes a bare 10–18.4 value inches. Other text such as `2-in-1` is ignored. A unit-less diagonal is read only when all of these hold: (1) the value has no inch-marked size; (2) the label names a size (LF `Display Size`, not a generic `Display`); (3) the value *opens* with the number; (4) the next word is `display`; and (5) the number is 10 to 18.4 (`14.0 display, WUXGA 1920 x 1200` → `14`). In a size-labeled row, every stated size is collected first, whatever separates it: each inch-marked size and each `NN[.N] display` diagonal. Two distinct sizes are `AMBIGUOUS`, whether both are unit-less or one is inch-marked (`14.0 display/16.0 display`, `14" / 16.0 display`). The same size stated twice is one size. Resolutions, refresh rates, and other numbers are never used. | `14`, `15.6` |
| `model` (code-rule labels: DataBlitz `Model`) | The labeled value must be exactly one model-code token, kept as written. Any other wording beside the code (`A14-11M-X3BP OPI`) is `UNRESOLVED`: the full wording is kept as the value for review, never stripped to the code and never matched (`UNRESOLVED_MODEL`). No suffix is special-cased. Two codes are `AMBIGUOUS`. | `SFG14-75-55MB` |
| `screen_size_in` (DataBlitz `Size` and `Display Panel/ Resolution`) | The inch-marked rule above. `Size: 14"` and `14.0"` agree and give one size; two different sizes are `AMBIGUOUS`. A unit-less `Size: 14.0` cannot supply its own unit. An explicit product diagonal in its own description (for example `14-inch laptop`, visible prose or JSON-LD `Product.description`) can supply inches when unambiguous and consistent with specification sizes. Bare numeric specification sizes check agreement only. Conflicting sizes require review; source text and paragraph/JSON-LD locators are retained. | `14` |
| `cpu_series` | `Core Ultra N`, `Core iN`, `Core N`, `Ryzen AI N`, `Ryzen N` (N = 3/5/7/9), and `Snapdragon X` / `X Elite` / `X Plus`, without the manufacturer prefix. `Apple Mn` (`Pro`/`Max`/`Ultra`) and `Apple Ann` (`Pro`) require and keep `Apple`. `Intel Nnnn` (`Intel N150`, `Intel Processor N150`) and `Celeron Nnnnn` keep the processor number. A complete Intel Core processor number without the `Core` word also gives `Core iN`: lower-case `i`, tier, hyphen, a 4–5 digit SKU, and an optional upper-case suffix, not glued to other text (`Raptor Lake i5-13420H`, `i3-1115G4`). An isolated `i3`/`i5`/`i7`/`i9` token in a labeled CPU field also gives `Core iN` (2026-10-15: `Processor: Intel i5`); `i3-N305`, `Inteli5-…`, a lower-case suffix, or generic `Intel Core` wording is not enough. A CPU row that opens with `up to` (`Up to 13th Gen Intel Core i7`) describes a product line, so it is supporting only. Matching uses the canonical family (owner policy, 2026-10-12). Processor numbers in one family give one value (`i5-13420H or i5-1335U` → `Core i5`), and the full processor wording stays in the evidence. Different canonical forms (`Core i5` and `Core i7`, `Core i5` and `Ryzen 5`, `Intel N100` and `Intel N150`) are `AMBIGUOUS`. Anything else is `UNPARSEABLE`. | `Core i5`, `Ryzen 7`, `Apple M5`, `Intel N150`, `Celeron N4020` |
| `ram_capacity_gb` | Exactly one total per field, from one labeled row in one of three shapes: a single amount (`16GB`), a single layout counted as count × size (`2x 8GB`, `16GB*1`), or an amount followed by its own parenthesized layout with the same total (`16GB (2x 8GB)`). RAM only: a stated total followed by one parenthesized `+` breakdown whose components sum to it (`DDR5 16GB (8GB on board + 8GB SO-DIMM)` → `16`), with exactly one amount outside the parentheses, before them, and one amount per component. Any extra amount, mismatched sum, layout inside the breakdown, unbalanced or second parentheses, or `or`/`/`/`up to`/`max`/`expandable`/`upgradable`/`supports` wording falls back to the general rule and is `AMBIGUOUS`. **Installed components (2026-10-15):** a row of `+`-joined parts with exactly one amount each and no parentheses, layout, alternative, maximum, optional, external, or slot wording is summed (`8GB + 8GB` → `16`, `8GB onboard + 8GB SO-DIMM` → `16`); the parts stay as supporting `ram_components`. **Repeated rows (2026-10-15):** several rows stating the same amount with the same placement are one fact (`EQUIVALENT_REPEATED_FACTS_DEDUPLICATED`), never a sum; equal rows for different parts (`8GB onboard`, `8GB SO-DIMM`) and different amounts stay `AMBIGUOUS`. TB is unparseable for RAM. **Maximum clauses (2026-10-13; comma 2026-10-15):** a clause (split at `;`, ` \| `, a line break, or a comma before the clause) that *opens* with `up to` or `max` is an upgrade limit (`8GB DDR5-4800 SO-DIMM, Max Capacity:64GB` → `8`), never installed memory: `1x 16GB DDR5 4800 MT/s \| Up to 32GB` gives `16`, and `Up to 32GB` alone gives no installed value. A limit inside one clause (`16GB (8GB + 8GB up to 32GB)`) stays `AMBIGUOUS`. Maximums, slot counts (`2x DDR4 SO-DIMM slots`), and frequencies (`4800 MT/s`) are kept as supporting `ram_maximum_gb`, `ram_slots`, `ram_frequency`. | `16` |
| `storage_capacity_gb` | As RAM (without the RAM breakdown rule), with `TB × 1000`. **Installed drives (2026-10-15):** an explicit `+` list of drives with a unit each and a drive medium in the row is summed (`512GB SSD + 512GB HDD` → `1024`, `512GB SSD + 1TB HDD` → `1512`); the layout is supporting `storage_layout`, and the media become the secondary `storage_type` (`HDD+SSD`, note `STORAGE_MIXED_MEDIA`), so a target recording `SSD` conflicts instead of becoming "1024GB SSD". A stated total with a drive list (`1TB (512GB + 512GB)`), alternatives (`/`, `or`), and external or optional drives stay `AMBIGUOUS`; equal rows naming different media stay `AMBIGUOUS`. A unit followed by `/s` or `ps` is a transfer rate (`16 Gb/s`), never a capacity. A drive amount without a unit (`1x 512 PCIe Gen4`) is `UNPARSEABLE` with `STORAGE_CAPACITY_UNIT_MISSING`; the amount and rate are supporting `storage_amount_without_unit` and `storage_throughput`. | `512`, `1000` |
| `resolution` | `WIDTH x HEIGHT` with `x` or `×` in a display row. | `1920x1200` |
| `display_type` | The words `IPS-Level`, `IPS`, or `OLED` in a display row. | as listed |
| `refresh_rate_hz` | `NNN Hz` in a display row. | `165` |

Each attempted field records `EXTRACTED`, `MISSING` (no labeled row), `UNPARSEABLE` (a row exists
but no rule recognizes a value), `AMBIGUOUS` (more than one distinct value), `NOT_EXTRACTED`
(evidence present but unsafe), or `UNRESOLVED` (a labeled value kept in full that needs an owner
decision), with the source label and text. Only `EXTRACTED` values reach the matcher. A core field
that is not extracted produces a reason such as `MISSING_MODEL`, `UNPARSEABLE_CPU_SERIES`,
`AMBIGUOUS_RAM_CAPACITY_GB`, or `UNRESOLVED_MODEL`.

**Supporting evidence (2026-10-13).** Evidence that is useful to a reviewer but is not a trusted
specification source is recorded with status `SUPPORTING`, its scope in the label (for example
`[shared product description]`, `[PRODUCT_DESCRIPTION; explicit size retained for consistency/review]`,
`[retailer specification row; manufacturer role unverified]`), and never reaches the matcher. A
supporting core value adds a reason such as `SUPPORTING_EVIDENCE_SCREEN_SIZE_IN`. Other notes:
`COLOR_TITLE_DIFFERS`, `CPU_NUMBER_TITLE_DIFFERS` (title `7640HS`, specification `6600H`),
`MPN_REPEATS_RETAILER_SKU`, `STORAGE_CAPACITY_UNIT_MISSING`, `SELECTED_VARIANT_NOT_CONFIRMED`,
and `SHARED_DESCRIPTION_DIFFERS_RAM_CAPACITY_GB` / `…_STORAGE_CAPACITY_GB`. Retailer SKUs,
barcodes, product IDs, variant IDs, and part-number rows are `identifier` /
`part_number_candidate` supporting rows and never become a verified manufacturer part number.

### Multi-variant pages

PC Central and Octagon share one description across all variants of a product. Their adapters put
the selected variant title before the description in `raw_configuration_text` only for
multi-variant products. When that prefix is present, extracted core values from the
description normally become `NOT_EXTRACTED` (kept as `SUPPORTING` with scope `[shared product
description]`), display secondaries are skipped, and `MULTI_VARIANT_SHARED_DESCRIPTION` is
recorded. The variant title itself is not parsed.

**Selected variant (PC Central, 2026-10-13).** The adapter binds the requested variant only when
the requested variant is the form's variant, every checked option control and any selected
`<option>` agree with that variant's own option values in the product's variant data. It then
emits `SELECTED_VARIANT` rows (`binding status: CONFIRMED`, `variant id`, and one row per option);
otherwise a single `binding status: NOT_CONFIRMED: <reason>` row and no option values. Only with a
confirmed binding does the extractor read the installed RAM and storage from the selected variant's
own option rows (`RAM [selected variant 50886253871271]: 16GB (2x8) DDR4 RAM` → `16`). CPU and
display (owner policy, 2026-10-13) and a full model code (2026-10-15) are inherited from the
shared description only with a complete confirmed RAM/storage-only variant matrix
(`SHARED_PRODUCT` provenance; the matrix is kept as supporting evidence). Otherwise they stay
`NOT_EXTRACTED` with supporting evidence. The default,
cheapest, or first available variant is never chosen. Other variants are listed as
`ENUMERATED_VARIANT` supporting rows only with the URL the page's own JSON-LD gives; no URL or
variant ID is constructed.

`gpu_model`, `os`, `ram_type`, and other secondary fields are not extracted: no canonical
vocabulary has been approved, and retailer wording (for example `Windows 11 Home Single
Language`) would create false conflicts. They remain available as raw evidence in the observation.
The one exception (2026-10-15) is `storage_type` for summed installed drives.

## Secondary evidence

Known secondary attributes can corroborate a unique core candidate. Missing secondary values on
either side never block a match or create a conflict. Since 2026-10-15 each field has an explicit
severity (`SECONDARY_SEVERITY` in `matching.py`):

| Severity | Fields | Effect of a verified disagreement |
|---|---|---|
| Blocking | `brand`, `resolution`, `display_type`, `refresh_rate_hz`, `cpu_brand`, `ram_type`, `storage_type`, `storage_layout`, `gpu_type`, `gpu_brand`, `gpu_model`, `vram`, `manufacturer_model_code`, `manufacturer_part_number` | `REVIEW_REQUIRED` with `SECONDARY_CONFLICT` and `SECONDARY_CONFLICT_<FIELD>`; `target_id` withheld. An unparseable or `AMBIGUOUS` value also blocks. |
| Warning | `cpu_model`, `cpu_generation`, `cpu_cores`, `os`, `battery_capacity`, `build_material`, `series`, `color` | `EXACT` kept; `SECONDARY_WARNING_<FIELD>` in the reason codes and `warnings`. |

`color` is descriptive in [TARGET_MASTER_SCHEMA.md](TARGET_MASTER_SCHEMA.md) (matching role
"No"), and same-family processor numbers are matched by family (owner policy 2026-10-12), so their
differences are visible warnings, never identical-CPU claims. The catalog's earlier variant splits
that named `os` or `cpu_model` as distinguishing fields also differ in a core field or model
code, so they stay distinct targets.

**Duplicate core targets.** A validated catalog cannot contain two `CONFIGURATION` rows with one
core key (the validator refuses it), so this applies only to direct `ExactMatcher` input. When
several targets share the offer's core key, a target is excluded only by a verified blocking
disagreement (both values present and different). Exactly one remaining target is assigned
(`DUPLICATE_CORE_NARROWED_BY_<FIELD>`); otherwise the result is `AMBIGUOUS`. A target missing the
discriminating value is not excluded, warning fields never narrow, and target order, popularity,
and the discovery seed are never used.

## Result statuses and reason codes

| Status | Meaning | `target_id` | `candidate_target_ids` |
|---|---|---|---|
| `EXACT` | One `ACTIVE` target has the complete core key and no blocking secondary conflict. Reason codes keep every extraction note (provenance such as `TITLE_MODEL_CORROBORATED` or `OWNER_APPROVED_MODEL`, and warnings such as `CPU_NUMBER_TITLE_DIFFERS`); `match_basis` and `warnings` summarize them. | Set | That target |
| `UNMATCHED` | No target has the core key (`NO_CORE_MATCH`), or the only one is `INACTIVE` (`INACTIVE_TARGET`). | None | Active targets differing in exactly one core field (`ONE_CORE_FIELD_DIFFERS`), or the inactive target |
| `AMBIGUOUS` | More than one target has the core key (`DUPLICATE_CORE_TARGETS`). | None | All of them |
| `REVIEW_REQUIRED` | The offer core is incomplete or invalid (`MISSING_CORE_FIELDS` / `INCOMPLETE_OR_INVALID_CORE_FIELDS` plus per-field codes), or a secondary conflict exists. | None | The conflicting target, if any |

Near candidates are review hints only; no tolerance is applied to the differing field. Inactive
targets are never assigned and never listed as near candidates.

## Offline command

```bash
python match_offers.py \
  --target-master data/review/target_master_export.csv \
  --observations data/review/offer_observations.csv

# Or read the authoritative TARGET_MASTER tab directly (read-only scope, that tab only):
python match_offers.py --target-master-from-sheets \
  --observations data/review/offer_observations.csv
```

- `--target-master` accepts a sheet export or the `target_master_validated.csv` copy written by
  `check_target_tables.py` (its leading `source_row_number` column is dropped before
  validation). `--target-master-from-sheets` reads only the `TARGET_MASTER` tab through
  `read_target_table_values()`; a missing tab stops the run. Neither path reads `raw` or writes
  to Sheets.

- `--observations` is a CSV with `OfferObservation.to_full_record()` columns
  (`OBSERVATION_COLUMNS`, then optional `OFFER_DETAIL_COLUMNS`). Every row is rebuilt with
  `observation_from_record()` and revalidated; one malformed row stops the run, and the error names
  the row number and field only.
- The `TARGET_MASTER` snapshot is checked with the Phase 5 validator. If any row fails, nothing is
  matched; a partially valid snapshot is refused rather than filtered, because dropping a
  duplicate row could turn an ambiguous offer into a false exact match.
- Writes to `--output-dir` (default `data/output/`, ignored by Git), after removing earlier copies:
  - `offer_match_results.csv`: one row per observation (`config/offer_match_results_template.csv`),
    with retailer, offer URL, retrieval time, offer index, title, adapter version, content hash,
    bundle flag, extractor, status, `target_id`, candidate IDs, reason codes, and the extracted
    core values.
  - `offer_match_review_queue.csv`: the same columns, every non-`EXACT` row.
  - `offer_match_evidence.csv`: one row per extraction or comparison
    (`config/offer_match_evidence_template.csv`), with source label and text for extraction rows
    and the compared target ID and value for match rows.
- Prints counts by status and reason code only. Reports contain retailer text and target values,
  so keep real runs local.

The Python API is `ExactMatcher.match_observation()` for one observation, or
`laptop_tracker.match_run` for batch use (`matcher_from_validated`, `read_observations_csv`,
`match_all`).

## Boundaries

- Only `CONFIGURATION` targets take part (`tracking_mode` blank or `CONFIGURATION`). `LISTING_ONLY`
  targets (approved 2026-10-08) are individual Shopee listings with no verifiable model. The
  catalog is validated first, and then `LISTING_ONLY` rows are excluded from the index. They never
  produce an exact match, a near-match candidate, or `AMBIGUOUS`, even if they carry
  specification values. The command prints how many were excluded. If the catalog has no
  `CONFIGURATION` rows, it says so on stderr and every offer stays unmatched or in review. An
  unknown tracking mode stops the run.
- Retailer matching uses `TARGET_MASTER` only. Shopee `source_model_id` links stay in
  `SOURCE_MODEL_MAP` and are neither read nor written here.
- Bundle-flagged offers are matched like any other offer; `bundle_flag` is carried into the results
  so Phase 9 can decide price comparability. Since the owner's 2026-10-15 update, confirmed
  bundles are comparable at their displayed price with a visible `BUNDLE_OFFER` note. Earlier
  dated replay results below retain the exclusion policy in force when those reports were built.
- No result is written to Google Sheets, and no target or mapping is promoted.

## Real-offer check (2026-10-08)

Nine Laptop Factory offers were collected with the production adapter and matched against the
validated live catalog snapshot (187 `CONFIGURATION` targets). Each offer's Model Name contains
a catalog `manufacturer_model_code`. Results: 0 `EXACT`, 6 `UNMATCHED`, 3 `REVIEW_REQUIRED`, and
no false positive. In every `UNMATCHED` case, `model` is the only differing core field, and the
target named by the code is listed as a near candidate. The three review cases are a display row
without an inch unit, a RAM row with an on-board + SO-DIMM split, and a CPU row without the
`Core` family word. All three follow the rules above.

The blocker is the `model` convention. LF states brand + name + manufacturer code (+ color), for
example `Acer Nitro V 15 ANV15-42-R2UH`. The catalog stores a family name (`Nitro`) and keeps the
code in the secondary `manufacturer_model_code`. The matcher must not strip brands or colors or
invent aliases. Retailer offers can match these targets only after the owner settles the
convention. Details, including the URLs and retrieval times, are in the ignored
`data/output/pilot_matching_2026-10-08/pilot_report.md`. Controlled end-to-end cases are in
`tests/test_matching_safeguards.py`.

**Replay after the conventions (2026-10-08).** The same nine saved observations were replayed
through the updated extractor; nothing was re-fetched. Extraction now yields the verified codes,
for example `ANV15-42-R2UH`.

- **A: current authoritative catalog** (live snapshot, unchanged): 0 `EXACT`, 6 `UNMATCHED`,
  3 `REVIEW_REQUIRED`. The catalog still stores family names, so this is the result that counts
  today.
- **B: DIAGNOSTIC only** (a local copy with only `READY` proposals applied; not authoritative):
  4 `EXACT`, 2 `UNMATCHED`, 3 `REVIEW_REQUIRED`.
  - The 4 `EXACT` offers have equal codes and all other core values agree.
  - The 2 `UNMATCHED` targets have mapped listings recorded under other codes, so their model
    proposals are unresolved.
  - The 3 review cases are the unchanged screen, RAM, and CPU evidence gaps.

The pilot is not successful until a replay against the corrected *authoritative* catalog
confirms it.

**Corrected authoritative catalog (owner, 2026-10-08).** The owner applied the 28 `model`/`series`
and 24 `cpu_series` READY items in the Sheet. The read-only checks passed (199 / 359 / 5,154;
Top 100 84 / 12 / 4). The authoritative replay gave 4 `EXACT` (TGT-000017, -034, -095, -226),
2 `UNMATCHED`, and 3 `REVIEW_REQUIRED`; it is recorded in the ignored
`replay_corrected_authoritative/REPLAY_VERIFICATION.md`.

**Narrow extraction rules (2026-10-08).** The three review cases were inspected in the saved
evidence and given narrow rules: the unit-less size-label diagonal, the RAM total-with-breakdown,
and the complete Intel Core number. Replaying the same nine saved observations against the same
validated catalog gives 6 `EXACT`, 3 `UNMATCHED`, and 0 `REVIEW_REQUIRED`.

- **New `EXACT`:** Swift Go → TGT-000030 and Vivobook S16 → TGT-000031. In both, the code and all
  five core values agree with the saved page.
- **MSI Thin 15 B13UC-3474PH:** its CPU now reads `Core i5`, but it stays `UNMATCHED` with no
  target. The page states 16GB while TGT-000219 records 8GB, and TGT-000219's model is still
  `Thin 15`. Its near candidates are unrelated targets that share only the other four values.
- **Two Nitro offers:** still `UNMATCHED`. They need catalog evidence review, not aliases.

The earlier four assignments are unchanged. This is a nine-offer, single-retailer check; it does
not complete Phase 8 or Phase 10.

**Screen alternatives and CPU-family policy (2026-10-12).**
- **Screen.** Code inspection found that a second unit-less size counted only when preceded by a
  space. `14.0 display/16.0 display` was read as `14`, and an inch-marked size could hide a
  unit-less one. Size-labeled rows now collect every stated size before accepting one.
- **CPU.** The owner confirmed that matching is by CPU family. Same-family processor numbers are
  therefore not ambiguous; the earlier statement that any two processors are ambiguous was
  wrong.
- **Replay.** The saved replay against the reconciled catalog (202 targets: 190
  `CONFIGURATION`, 12 `LISTING_ONLY`) is unchanged: 8 `EXACT`, 1 `UNMATCHED` (MSI), 0
  `REVIEW_REQUIRED`.

**MSI reconciliation and current catalog (2026-10-12).** The owner split the MSI Thin targets by
evidence: TGT-000219 (B13UC-3474PH, 8GB), TGT-000257 (B12UC-2601PH, 8GB), and TGT-000258
(B13UC-3474PH, 16GB). The replay recorded in the ignored
`replay_after_msi_reconciliation_2026-10-12/REPLAY_VERIFICATION.md` gave 9 `EXACT`: the MSI offer
matches TGT-000258, not the 8GB TGT-000219. Re-run at the 2026-10-12 checkpoint against the
current catalog (205 targets: 193 `CONFIGURATION`, 12 `LISTING_ONLY`; TARGET_MASTER `fbe29cb5…`),
the result is the same: **9 `EXACT`**, 0 `UNMATCHED`, 0 `REVIEW_REQUIRED` (TGT-000226, -059, -215,
-030, -034, -095, -031, -017, -258). These are saved observations from 2026-10-08, not fresh
prices. The `model`-convention blocker described above is resolved for these offers.

**Live manual check (2026-10-12).** `collect_and_match.py --live` fetched one Laptop Factory page
(`acer-aspire-ai-14-a14-11m-x3bp`) at 2026-10-12T08:37:55Z. It was a fresh retrieval, not a
replay: HTTP 200 on attempt 1, with a new page hash. The offer matched **EXACT** to TGT-000226 (as
hinted; selected rank 39) with no reason codes, and was `IN_STOCK` at a PHP 45,999.00 sale price
(regular 50,999.00). The exported CSV re-matched EXACT through `match_offers.py`. One page from
one retailer; not a pilot.

**Evidence-based extraction update and replay (2026-10-13, before owner approvals).** The five non-Laptop-Factory review
cases of the Phase 10 all-retailer run were inspected in their saved pages and given the layout
rules above (DataBlitz `Model`/`Size`/`Display Panel/ Resolution`, paragraph groups with
footnoted headings, maximum-memory clauses, unit-less drive amounts, transfer rates, PC Central
selected-variant binding). Adapters moved to `dbz-2`, `pcx-2`, `pcc-2`, and `oct-2`; `lf-1` is
unchanged. `replay_extraction.py` replayed the twelve saved offers twice, offline: the original
observation CSV through the current extractor, and the saved HTML through the current adapters.

| Case | Before | After (saved HTML path) | Status |
|---|---|---|---|
| Seven Laptop Factory offers | 7 `EXACT` | 7 `EXACT`, same targets; records identical | unchanged |
| DataBlitz Aspire | model, screen missing | model `UNRESOLVED` (`A14-11M-X3BP OPI`); screen `14`; `COLOR_TITLE_DIFFERS` | `REVIEW_REQUIRED` |
| DataBlitz Swift Go | model, screen missing | model `SFG14-75-55MB`; screen `UNPARSEABLE` (`Size: 14.0`), prose `14` supporting only | `REVIEW_REQUIRED` |
| PC Express Vivobook | model missing | unchanged four fields; `MPN_REPEATS_RETAILER_SKU` | `REVIEW_REQUIRED` |
| PC Central MSI 16GB | all missing | RAM `16` and storage `512` from the bound selected variant; screen/CPU shared (supporting); `SHARED_DESCRIPTION_DIFFERS_RAM_CAPACITY_GB` | `REVIEW_REQUIRED` |
| Octagon Nitro | all missing | screen `15.6`, CPU `Ryzen 5`, RAM `16`; storage `UNPARSEABLE` (`STORAGE_CAPACITY_UNIT_MISSING`); `CPU_NUMBER_TITLE_DIFFERS` | `REVIEW_REQUIRED` |

No new `EXACT` match was created; each remaining gap needs an owner decision or better evidence,
listed in [PHASE_10_PILOT.md](PHASE_10_PILOT.md). Re-parsed records keep their observation
identity but carry new content (`adapter_version`, `scoped_evidence`), so they are never imported:
a throwaway-copy import was refused for the five changed records.

## Limitations

- **Little real evidence.** Extraction rules were derived from the adapters' synthetic fixtures
  and documented reviewed pages. One small real check (nine Laptop Factory offers, saved
  2026-10-08) has been matched; no other real observation has been matched.
- **Catalog partly converted.** The 28 READY model codes and 24 Apple CPU forms are applied in
  the live catalog (2026-10-08), and TGT-000059, -215, and -219 were reconciled by 2026-10-12.
  Other unresolved model items keep their names. Targets without a verified code keep their name,
  and retailer labels without a code keep their stated name, so the two match only when the
  wording is identical.
- **Model coverage.** Before 2026-10-15 only Laptop Factory, single-variant PC Central, and
  DataBlitz offers with a `Model` row that is exactly one code could supply `model`. Since then a
  corroborated title code and an inherited shared code can also supply it, under the rules in
  [Balanced matching policy](#balanced-matching-policy-2026-10-15).
- **Manual collection only.** Since 2026-10-12, `collect_and_match.py` fetches explicitly listed
  product pages, stores them in the Phase 9 store, and matches them with this matcher under a
  recorded context. `expected_target_id` in its manifest is a hint that is compared with the
  outcome, never used to assign it. Nothing schedules collection. One live Laptop Factory page
  has been checked this way (EXACT, TGT-000226).
- Unsupported CPU lines (Pentium, Athlon, incomplete Intel numbers), multi-drive storage, RAM
  breakdowns that do not sum to one stated total, display rows without a unit outside the narrow
  size-label form, and variant-specific RAM/storage go to review rather than being guessed. The
  unit-less diagonal rule rests on one reviewed retailer's label convention: in the 8 other saved
  LF `Display Size` rows, the diagonal comes first and in inches.
- Model codes use ASCII hyphens; Unicode hyphen equivalents (non-breaking hyphen, minus sign,
  full-width hyphen) are read as `-` since 2026-10-15. A label written with en dashes, or with a
  code split by spaces, keeps its stated name and will not match a code.

## Owner-approved extraction policy replay (2026-10-13)

The owner approved explicit product screen sizes from prose and conditional shared CPU/screen
inheritance, retaining the OPI, trusted-model and storage-unit restrictions. DataBlitz is now
`dbz-3` (description paragraph locators); PC Central is `pcc-3` (complete variant matrices).
Replaying the same twelve saved HTML pages gives **8 EXACT / 4 REVIEW_REQUIRED**, with no
reparse failures: Swift Go now matches TGT-000030 from explicit 14-inch product prose; its
bundle flag still excludes its price. PCC MSI extracts shared Core i5 / 15.6 with selected
16GB / 512GB, retains the shared 8GB discrepancy, and still needs trusted model evidence.
Aspire OPI, PCX model evidence and Octagon model/storage-unit gaps remain in review.
Original observation CSV replay remains 7 / 5 because it lacks the new scoped evidence.
Outputs: `data/output/phase10_2026-10-13_all_retailers/owner_policy_replay_2026-10-13/`.
Reparsed observations are research output marked NOT_FOR_IMPORT; no saved observation or
prior match was overwritten, no live page was fetched and nothing was published.

## Listing-scoped owner decisions (2026-10-13)

The owner subsequently approved DataBlitz Aspire OPI equivalence, Octagon Nitro OPI/model
binding, and GB for Octagon's unitless 512 drive amount. These three decisions are explicit
rows in `config/retailer_evidence_approvals.csv`, applied only to exact retailer/variant URLs
with agreeing independently extracted CPU family, screen and RAM. Aspire also requires its
labeled NX.JP3SP.001 part-number evidence. This is owner-reviewed interpretation, not a claim
of manufacturer verification or a generic title/suffix/capacity rule.

Model approval requires the original approved designation, or for the reviewed Octagon listing
its exact designation in the title. Storage approval requires one unitless drive mention and
a single valid capacity after adding GB; conflicting explicit capacities and ambiguous quantities
are not replaced. Original observations, titles and specification text remain unchanged.
Field evidence records OWNER_APPROVED, decision ID, owner/date and original text. Exact result
reason codes retain owner approval plus any title/specification CPU/color discrepancy. Target
assignment still requires all five fields to agree with a valid active configuration.

Approval policy bytes are included in the matching context fingerprint. Changes create a new
matching context; old observations and matches are not rewritten. Saved-HTML replay after these
approvals gives 10 EXACT / 2 REVIEW_REQUIRED; original CSV gives 9 / 3 (older records lack Swift
prose and PCC variant evidence). Five saved prices are comparable: three LF, one DataBlitz
Aspire and one Octagon Nitro. Swift Go stays excluded as a bundle. No live price refresh or
Sheets publication was performed for this policy update.

## Balanced matching policy (2026-10-15)

Owner policy review of 15 October 2026 ([DECISIONS.md](DECISIONS.md#balanced-matching-policy-2026-10-15)).
Rules version `five-field-exact/balanced-evidence/2026-10-15`; adapters `oct-3`, `pcc-4`. The
five required fields, CPU-family matching, `LISTING_ONLY` exclusion, observation identity, and the
three listing-scoped approvals are unchanged. `EXACT` means "matches this catalog configuration
under the five-field policy"; it does not claim an identical factory SKU, processor number, or
commercial package.

### Evidence by scope, before any catalog comparison

Claims are collected, classified by meaning and scope, and resolved without consulting the
target. No source is universally superior:

1. **Selected variant** (confirmed binding): installed RAM and storage options. They override a
   shared default description, which is kept with `SHARED_DESCRIPTION_DIFFERS_<FIELD>` (and
   `SHARED_TITLE_DIFFERS_<FIELD>` for a parent title). Two selected-scope values that differ are
   `AMBIGUOUS`.
2. **Product specification rows** (trusted labels per retailer), deduplicated when equivalent.
3. **Shared product description** on multi-variant pages: model code, CPU family, and screen only
   with a complete confirmed RAM/storage-only matrix (`SHARED_PRODUCT`).
4. **Product-owned prose**: explicit screen diagonals only (2026-10-13 rule).
5. **Title**: one complete model code, only as below. Title RAM/storage/CPU/screen statements are
   conflict checks.
6. **URL path, identifiers, search snippets, related products**: conflict checks or supporting
   evidence only. A JSON-LD `mpn` repeating the retailer SKU is inventory data
   (`MPN_REPEATS_RETAILER_SKU`, a warning); an `mpn` that is a different model code is
   `MODEL_IDENTIFIER_CONFLICT`.

### Title model corroboration (`TITLE_MODEL_CORROBORATED`)

A title code supplies `model` only when all of these hold; otherwise the reason is recorded and
`model` stays unresolved:

- exactly one model code in the title (documented ASUS part numbers such as `90NB1672-M00DD0`
  are recorded as `part_number_candidate`, not codes); else `TITLE_MULTIPLE_MODEL_CODES`;
- no unexplained designation right after it (`ANV15-42-R2UH OPI`); else
  `TITLE_MODEL_UNEXPLAINED_SUFFIX` (only an exact-URL owner approval can resolve it);
- every model claim on the page agrees, is the same full code, or is a less specific
  designation equal to one of its hyphen segments (`Thin 15 B13UC`, `FA506NCQ`); a different code
  or designation (`FA506NC`) gives `MODEL_CLAIM_CONFLICT`;
- the product URL path does not name the same stem with another final segment
  (`anv15-42-r2auh` for `ANV15-42-R2UH`); else `MODEL_URL_CONFLICT`;
- the selected scope is clear: no unconfirmed binding, and a shared page needs the complete
  matrix; else `TITLE_MODEL_SCOPE_UNPROVEN`;
- the other four fields are `EXTRACTED` from non-title evidence of the same product and
  selection, and the title does not contradict them on a single-scope page
  (`TITLE_SPEC_CONFLICT_<FIELD>`).

The code is used as written. Normalization is limited to case, surrounding whitespace, trademark
marks, and Unicode hyphen equivalents; nothing is truncated, and `R2UH` and `R2AUH` differ. A
trusted labeled code that differs from the title's one code is `MODEL_TITLE_CONFLICT`. The
evidence label lists the source of each corroborating field.

### Outcomes, eligibility, and coverage

`offer_outcome()` maps a stored status and reason codes to `MATCHED_CONFIGURATION`,
`DIFFERENT_CONFIGURATION` (complete verified core, no target), `MODEL_UNVERIFIED` (only the model
is missing or unresolved), `IDENTITY_CONFLICT`, `EXTRACTION_INCOMPLETE`, `AMBIGUOUS_TARGET`, or
`INACTIVE_TARGET`. Price eligibility is separate and unchanged (see
[PRICE_TRACKING_OUTPUT_SCHEMA.md](PRICE_TRACKING_OUTPUT_SCHEMA.md)). `coverage_report.py` adds the
configuration × retailer coverage table (`pilot_coverage.py`).

### Saved-evidence replay (2026-10-15)

Offline, same inputs and catalog snapshot (`fbe29cb5…`) for the pre-change working tree and the
revised code. Outputs: `data/output/policy_review_2026-10-15/` (ignored by Git), including
`before_after_by_offer.csv` and a local `preview.html`.

| Input | Baseline | Revised |
|---|---|---|
| 13 Oct pilot, saved HTML re-parsed (12 offers) | 10 EXACT / 2 REVIEW | 12 EXACT |
| 13 Oct pilot, original records (12 offers) | 10 EXACT / 2 REVIEW | 12 EXACT |
| 15 Oct run, original records (16 offers; product bodies not saved under their observation hashes) | 7 EXACT / 9 REVIEW | 12 EXACT / 3 REVIEW / 1 UNMATCHED |
| 15 Oct discovery inspection copies, Octagon/PCC (separate fetches) | Octagon TUF REVIEW | Octagon TUF EXACT TGT-000017 (`oct-3` layout) |

Changed results: PC Express Vivobook (13 and 15 Oct) → TGT-000031; PC Central MSI 16GB (13 Oct)
→ TGT-000258 with shared-8GB warnings; DataBlitz TUF → TGT-000017 (bundle, excluded); DataBlitz
Vivobook → TGT-000031 (bundle, excluded); PC Express Swift Go → TGT-000030 (`Model` row);
PC Express MSI 8GB → TGT-000219 (outside the pilot scope); Octagon Swift SF14-51-553D →
`UNMATCHED` (page 14.5in, TGT-000034 records 14). Still in review: PC Express TUF (`Model:
FA506NC` conflicts with `FA506NCQ-HN012W`), Octagon TUF original record (flattened layout;
`oct-3` fixes new collections), PC Central Nitro (title/spec `R2UH`, URL `r2auh`). No earlier
`EXACT` result changed target or status. Re-parsed `oct-3`/`pcc-4` records keep their observation
identity with new content, so the throwaway-store import was refused (76/76) as designed.

## Scoped evidence, URL slugs, and model-name suffixes (2026-10-19)

Rules version `five-field-exact/scoped-evidence/2026-10-19`. The five required fields, exact
comparison, CPU-family matching, title-model corroboration, the complete-matrix condition for
shared fields, and the listing-scoped owner approvals are unchanged.

**Evidence scope.** Every extraction evidence item carries `scope`: `SELECTED_VARIANT`,
`PRODUCT_SPECIFICATION`, `SHARED_PRODUCT`, `PRODUCT_DESCRIPTION`, `TITLE`, `OWNER_APPROVED`
(these can supply values under the existing rules), and `MODEL_FAMILY_CAPABILITY`,
`MAXIMUM_SUPPORTED`, `OPTIONAL_OR_ALTERNATIVE`, `URL`, `IDENTIFIER`, `OTHER_VARIANT` (supporting
only). Applicability is resolved before comparison, with raw wording and locator kept:

- A CPU clause or parenthesis that opens with capability wording (`up to`, `configurable to`,
  `options up to`, `available with`, `optional`) is a family capability:
  `Intel Core i5-1335U (up to Core i7)` is `Core i5`. Alternatives without such wording
  (`i5-13420H / i7-13620H`, `i5 or i7`) stay `AMBIGUOUS`. Title capability clauses are not title
  conflicts.
- Memory and storage clauses or parentheses that open with maximum wording (`up to`, `max`,
  `expandable/upgradable/configurable (up) to`) or optional wording (`optional`, `option upgrade`,
  `available with`, `alternatively`) and state their own amount are kept as `ram_maximum_gb`,
  `ram_optional_gb`, `storage_maximum_gb`, `storage_optional_gb`, never installed capacity:
  `16GB DDR5 (upgradable up to 32GB)` → 16; `8GB, expandable to 32GB` → 8 (previously
  `AMBIGUOUS`); `512GB SSD | Up to 2TB` → 512. `512GB SSD + 1TB HDD (optional)`,
  `512GB SSD + optional 1TB HDD`, and `16GB (8GB + 8GB up to 32GB)` stay `AMBIGUOUS`.
- Confirmed selected RAM/storage still supersedes shared default wording with the discrepancy
  retained; equivalent repeated rows remain one fact (never summed, never two confirmations).

**URL slugs.** When the page's own evidence has established a consistent model (a trusted model
row, a shared model proven by the complete matrix, or a corroborated title code), a URL path
naming the same stem with another final segment (`anv15-42-r2auh` for `ANV15-42-R2UH`) is the
warning `MODEL_URL_DIFFERS`, no longer the block `MODEL_URL_CONFLICT`. The URL wording and the
page model are both kept (`scope = URL`); no alias is created and the URL never supplies a model.
Page model claims that disagree (`Model: FA506NC` vs `FA506NCQ-HN012W`), unexplained suffixes,
title/specification conflicts, and unconfirmed variant bindings still block.

**Model-name suffixes.** A model-name row (Laptop Factory `Model Name`) with an unexplained
designation right after its one code (`Acer Aspire AI 14 A14-11M-X3BP OPI`) is now `UNRESOLVED`,
as for code-rule rows and titles; it was previously reduced to the code. Ordinary words (colours,
`OLED`, `Gaming`, ...) and parenthesized text are unaffected.

Saved results keep their context; these rules create a new matching context. Synthetic
before/after replay results are in PROGRESS_LOG.md; a replay of the real saved pilot is pending.

## Configuration outcomes, flattened maximums, and accessory codes (2026-10-19)

Rules version `five-field-exact/configuration-lineage/2026-10-19` (a new matching context).

**One rule for "why not this configuration".** `matching.configuration_outcome` explains an offer
for one configuration, and both `coverage.csv` and the weekly comparison use it:
`SAME_MODEL_DIFFERENT_CONFIGURATION` only when the model agrees and every differing field is a
configuration option (RAM or storage for a manufacturer model code; any field for a family name
such as `MacBook Air`); the same code with another screen size or processor family is an
`IDENTITY_CONFLICT` ("same model code, conflicting specification ... (unresolved)"); another
model is `DIFFERENT_MODEL`; unresolved results keep their review outcome. A matcher near-match
hint (`candidate_target_ids`) relates an offer to a configuration only for the same model.

**Flattened maximum clauses.** Inside one memory or storage clause, a maximum phrase with its own
amount after an installed amount (`8GB RAM Max up to 64GB 2 Slots`, a DataBlitz cell whose line
breaks were flattened) is split off: installed 8, `ram_maximum_gb` 64, `ram_slots` 2. Only
outside parentheses, only when the phrase states a GB/TB amount (`up to 5600MHz` is not a
capacity), and only when the wording before it states an amount.

**Identifier kinds in titles.** `conventions.title_identifiers` separates laptop model codes,
documented part numbers and included-accessory codes. A code written after an accessory noun
(backpack, bag, sleeve, mouse, headset, keyboard, monitor, dock, ...) in a bundle-item segment
(`+` or `|` separated) that does not name a laptop is an `accessory_identifier` supporting item,
never a model code (`E-1620-P`). Context decides, never the catalog: a laptop-like code after
"Backpack" is still not chosen, and two laptop codes still give `TITLE_MULTIPLE_MODEL_CODES`.
Retailer SKUs stay with the adapters' identifier evidence.

## Identity kinds, title evidence and product names (2026-10-19, second review)

Decision record: [DECISIONS.md](DECISIONS.md#catalog-identity-title-evidence-and-recheck-links-second-review-2026-10-19).

- **What the catalog model is** (`product_names.py`, `config/product_name_registry.csv`): one
  manufacturer code (`MODEL_CODE`), a reviewed product name (`PRODUCT_NAME`: MacBook Neo/Air/Pro)
  or a broad family name (`FAMILY_NAME`; also every unreviewed name). A model difference against a
  family-name target is `CATALOG_IDENTITY_UNRESOLVED`, never another configuration and never a
  match; `reconcile_catalog.py` proposes source-backed codes for those targets.
- **Product-name presentation** (`conventions.normalize_product_name`): for registered names
  only, a repeated leading brand and a registered colour in parentheses are removed
  (`MODEL_NAME_PRESENTATION_NORMALIZED`, original kept). No suffix, prefix, joining or fuzzy rule.
- **Title statements** (`extraction._apply_title_fields`): screen, CPU (Apple chip modifiers kept),
  RAM, storage and the joined `8GB+256GB` form stated in the title fill a missing field
  (`TITLE_SUPPLIED_<FIELD>`, scope `TITLE`) or make a contradicted field `AMBIGUOUS`
  (`TITLE_SPEC_CONFLICT_<FIELD>`). Skipped on shared multi-variant descriptions; URLs never read.
  A title model (code or registered product name, `TITLE_PRODUCT_NAME_CORROBORATED`) still needs
  the four other fields established, which may now come from the title.
- **Extraction repairs:** `LPDDR5X 16GB` reads 16 (not 80); curly inch marks; new DataBlitz,
  Octagon and PC Express labels (see the adapter notes).
- Replay of the 157 saved top-100 observations under these rules: EXACT 35 → 46 with no exact
  match lost or reassigned (52 with the eight supported catalog corrections applied locally);
  every new exact match was audited field by field
  (`data/output/sample_validation_2026-10-19/new_exact_audit_saved_observations.csv`).

## Title component structure and secondary claims (2026-10-19, independent review)

Decision record: [DECISIONS.md](DECISIONS.md#independent-review-fixes-2026-10-19).

- Title capacities are parsed mention by mention with their role and scope
  (`_title_mentions`, `_title_capacities`) before any total is formed; repeated components are
  summed, external/extra/maximum amounts never count, and unclear structure is `AMBIGUOUS`.
- Title GPU, panel type and resolution claims reach the matcher's blocking secondary check;
  `conventions.canonical_gpu_models` gives one form for equivalent GPU spellings.
- Display rows: WUXGA/WQXGA establish a resolution; FHD/Full HD/FHD+/QHD are supporting
  `resolution_name` evidence only.
- Replay of the 157 saved top-100 observations with these rules: EXACT 46 (unchanged catalog) and
  52 (DIAGNOSTIC catalog), the same observations assigned to the same targets as before the fix;
  73 observations gained supporting or secondary evidence only
  (`data/output/review_fixes_2026-10-19/replay_157/`).

## Listed-spec comparison (2026-10-20)

Decision record: [DECISIONS.md](DECISIONS.md#listed-spec-comparison-for-broad-family-targets-2026-10-20). `listed_spec.check_target` decides one offer against
one broad-family target; `ListedSpecMatcher.associate` applies it across the catalog with the
ambiguity rule. `matching.configuration_outcome(..., title=...)` returns `LISTED_SPEC_MATCH` or the
specific reason for a family target, and is shared by the weekly report, coverage, rechecks and
discovery. The exact matcher (`ExactMatcher`) is unchanged.

## Close alternatives (2026-10-27)

Decision record: [DECISIONS.md](DECISIONS.md#close-alternative-retailer-comparisons-owner-policy-2026-10-27);
specification: [CLOSE_ALTERNATIVES_2026-10-27.md](CLOSE_ALTERNATIVES_2026-10-27.md).

The matcher, its rules version, its matching context and every stored result are unchanged:
`ExactMatcher` still assigns a target only for the complete five-field key, and
`alternatives.py` is outside `price_tracking.RULE_MODULES`. The alternatives layer is a separate,
explanatory comparison of an offer's own evidence with a target: up to two established differences
across model, screen size, CPU, installed RAM, installed storage and GPU make a labelled
alternative (`ALT 1`, `ALT 2`); contradictions inside the retailer's evidence are rechecks; a
missing core value is never agreement; zero differences never create a match. It reads
`StoredMatch.offer_attributes` (what the matcher compared) and the same extraction's evidence,
and writes only report rows.

A broad-family target keeps the listed-spec treatment: its model is compared at family level and
the specific model stays explicitly unknown. `configuration_outcome` (the coverage and recheck
wording, "same model, different configuration") is unchanged; a confirmed alternative is reported
beside it, not instead of it.
