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
- Missing core values never compare equal. Core values are never fuzzily compared or taken from a
  title.

## Retailer extraction (`src/laptop_tracker/extraction.py`)

Each retailer extractor reads only the adapter's labeled `(label, value)` rows in
`OfferDetails.raw_specifications`, plus the structured `brand` and `manufacturer_part_number`
detail fields. It never parses `raw_title`, `raw_configuration_text`, variant option titles,
unlabeled lines (headings, `✅ …` bullets), or URLs.

Labels are matched case-insensitively. Only labels seen in each adapter's reviewed pages and
synthetic fixtures are mapped; any other label is ignored.

| Retailer | `model` | `screen_size_in` (+ display secondaries) | `cpu_series` | `ram_capacity_gb` | `storage_capacity_gb` |
|---|---|---|---|---|---|
| Laptop Factory | `Model Name` | `Display Size` | `Processor` | `RAM` | `Storage` |
| DataBlitz | — | `Display` | `CPU`, `Processor` | `Memory` | `Storage` |
| PC Express | — | `Display` | `Processor` | `Memory` | `Storage` |
| PC Central | `Model` | `Display` | `Processor` | `Memory` | `Internal Storage`, `Storage` |
| Octagon | — | `Display` | `Processor` | `Memory` | `Storage` |

No model label appears in the reviewed DataBlitz, PC Express, or Octagon output, so their offers
always reach the review queue with `MISSING_MODEL`. Extend a label set only after reviewing real
adapter output for that retailer.

### Value rules

| Field | Rule | Canonical form |
|---|---|---|
| `model` | The labeled value with trademark marks removed. If it contains exactly one model-code token ([conventions](TARGET_MASTER_SCHEMA.md#model-and-cpu-series-conventions-2026-10-08)), that code is the value as written: case and suffixes are kept, and brand, family words, and a color in parentheses are dropped. With no code token, the stated name is kept. Two or more distinct codes are `AMBIGUOUS`. The full label stays in the evidence. | `ANV15-42-R2UH`; otherwise the stated name |
| `screen_size_in` | A number followed by `"`, `″`, `”`, `''`, or `inch`. Other text such as `2-in-1` is ignored. A unit-less diagonal is read only when all of these hold: (1) the value has no inch-marked size; (2) the label names a size (LF `Display Size`, not a generic `Display`); (3) the value *opens* with the number; (4) the next word is `display`; and (5) the number is 10 to 18.4 (`14.0 display, WUXGA 1920 x 1200` → `14`). In a size-labeled row, every stated size is collected first, whatever separates it: each inch-marked size and each `NN[.N] display` diagonal. Two distinct sizes are `AMBIGUOUS`, whether both are unit-less or one is inch-marked (`14.0 display/16.0 display`, `14" / 16.0 display`). The same size stated twice is one size. Resolutions, refresh rates, and other numbers are never used. | `14`, `15.6` |
| `cpu_series` | `Core Ultra N`, `Core iN`, `Core N`, `Ryzen AI N`, `Ryzen N` (N = 3/5/7/9), and `Snapdragon X` / `X Elite` / `X Plus`, without the manufacturer prefix. `Apple Mn` (`Pro`/`Max`/`Ultra`) and `Apple Ann` (`Pro`) require and keep `Apple`. `Intel Nnnn` (`Intel N150`, `Intel Processor N150`) and `Celeron Nnnnn` keep the processor number. A complete Intel Core processor number without the `Core` word also gives `Core iN`: lower-case `i`, tier, hyphen, a 4–5 digit SKU, and an optional upper-case suffix, not glued to other text (`Raptor Lake i5-13420H`, `i3-1115G4`). A bare `i5`, `i3-N305`, `Inteli5-…`, or a lower-case suffix is not enough. Matching uses the canonical family (owner policy, 2026-10-12). Processor numbers in one family give one value (`i5-13420H or i5-1335U` → `Core i5`), and the full processor wording stays in the evidence. Different canonical forms (`Core i5` and `Core i7`, `Core i5` and `Ryzen 5`, `Intel N100` and `Intel N150`) are `AMBIGUOUS`. Anything else is `UNPARSEABLE`. | `Core i5`, `Ryzen 7`, `Apple M5`, `Intel N150`, `Celeron N4020` |
| `ram_capacity_gb` | Exactly one total per field, from one labeled row in one of three shapes: a single amount (`16GB`), a single layout counted as count × size (`2x 8GB`, `16GB*1`), or an amount followed by its own parenthesized layout with the same total (`16GB (2x 8GB)`). RAM only: a stated total followed by one parenthesized `+` breakdown whose components sum to it (`DDR5 16GB (8GB on board + 8GB SO-DIMM)` → `16`), with exactly one amount outside the parentheses, before them, and one amount per component. Any extra amount, mismatched sum, layout inside the breakdown, unbalanced or second parentheses, or `or`/`/`/`up to`/`max`/`expandable`/`upgradable`/`supports` wording falls back to the general rule and is `AMBIGUOUS`. Every capacity mention is counted before deduplication, so repeated equal amounts (`8GB + 8GB`), any other combination, or more than one labeled row is `AMBIGUOUS`. TB is unparseable for RAM. | `16` |
| `storage_capacity_gb` | As RAM (without the RAM breakdown rule), with `TB × 1000`. Several drives (`512GB SSD + 512GB HDD`, `512GB SSD + 1TB HDD`) are `AMBIGUOUS`, never summed or collapsed. | `512`, `1000` |
| `resolution` | `WIDTH x HEIGHT` with `x` or `×` in a display row. | `1920x1200` |
| `display_type` | The words `IPS-Level`, `IPS`, or `OLED` in a display row. | as listed |
| `refresh_rate_hz` | `NNN Hz` in a display row. | `165` |

Each attempted field records `EXTRACTED`, `MISSING` (no labeled row), `UNPARSEABLE` (a row exists
but no rule recognizes a value), `AMBIGUOUS` (more than one distinct value), or `NOT_EXTRACTED`
(evidence present but unsafe), with the source label and text. Only `EXTRACTED` values reach the
matcher. A core field that is not extracted produces a reason such as `MISSING_MODEL`,
`UNPARSEABLE_CPU_SERIES`, or `AMBIGUOUS_RAM_CAPACITY_GB`.

**Multi-variant pages.** PC Central and Octagon share one description across all variants of a
product. Their adapters put the selected variant title before the description in
`raw_configuration_text` only for multi-variant products. When that prefix is present, every core
field is `NOT_EXTRACTED`, display secondaries are skipped, and the reason
`MULTI_VARIANT_SHARED_DESCRIPTION` is recorded, because the description may describe a different
variant. The variant title itself is not parsed.

`gpu_model`, `os`, `ram_type`, and other secondary fields are not extracted: no canonical
vocabulary has been approved, and retailer wording (for example `Windows 11 Home Single
Language`) would create false conflicts. They remain available as raw evidence in the observation.

## Secondary evidence

Known secondary attributes can corroborate a unique core candidate. Missing secondary values on
either side do not block a match. An explicit disagreement, an unparseable supplied value, or
`AMBIGUOUS` secondary extraction produces `REVIEW_REQUIRED` with `SECONDARY_CONFLICT` and a
field-specific code such as `SECONDARY_CONFLICT_REFRESH_RATE_HZ`, and withholds `target_id`. It
never creates a second target or chooses one value. Two targets with the same core key are
`AMBIGUOUS`, even if their secondary details differ.

## Result statuses and reason codes

| Status | Meaning | `target_id` | `candidate_target_ids` |
|---|---|---|---|
| `EXACT` | One `ACTIVE` target has the complete core key and no known secondary conflict. | Set | That target |
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
  so Phase 9 can decide price comparability.
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

## Limitations

- **Little real evidence.** Extraction rules were derived from the adapters' synthetic fixtures
  and documented reviewed pages. One small real check (nine Laptop Factory offers, saved
  2026-10-08) has been matched; no other real observation has been matched.
- **Catalog partly converted.** The 28 READY model codes and 24 Apple CPU forms are applied in
  the live catalog (2026-10-08), and TGT-000059, -215, and -219 were reconciled by 2026-10-12.
  Other unresolved model items keep their names. Targets without a verified code keep their name,
  and retailer labels without a code keep their stated name, so the two match only when the
  wording is identical.
- **Model coverage.** Only Laptop Factory and single-variant PC Central offers can supply `model`.
  DataBlitz, PC Express, Octagon, and multi-variant PC Central offers always go to review.
- **No live collector.** No command yet collects live adapter observations. Since 2026-10-12, the
  Phase 9 store (`observation_store.py`) can persist `scrape()` results and saved CSVs, and
  `track_prices.py` matches them under a recorded matching context; a supervised collector is
  pilot work.
- Unsupported CPU lines (Pentium, Athlon, incomplete Intel numbers), multi-drive storage, RAM
  breakdowns that do not sum to one stated total, display rows without a unit outside the narrow
  size-label form, and variant-specific RAM/storage go to review rather than being guessed. The
  unit-less diagonal rule rests on one reviewed retailer's label convention: in the 8 other saved
  LF `Display Size` rows, the diagonal comes first and in inches.
- Model codes use ASCII hyphens only. A label written with en dashes, or with a code split by
  spaces, keeps its stated name and will not match a code.
