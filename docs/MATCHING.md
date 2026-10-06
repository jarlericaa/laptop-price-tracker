# Phase 8: Exact offer matching

**Status (2026-10-06):** COMPLETE as an offline path. Retailer-specific extraction, the five-field
exact matcher, traceable results, and a review queue run locally from saved observation and
`TARGET_MASTER` CSV snapshots. Nothing in this path fetches pages, reads or writes Google Sheets,
reads `raw`, creates targets, or creates `SOURCE_MODEL_MAP` rows. It has been exercised only on
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
| `model` | The labeled value as stated, with trademark marks removed. A trailing color or code is not stripped. | Retailer text |
| `screen_size_in` | A number followed by `"`, `″`, `”`, `''`, or `inch`. Other text such as `2-in-1` is ignored. | `14`, `15.6` |
| `cpu_series` | `Core Ultra N`, `Core iN`, `Core N`, `Ryzen AI N`, `Ryzen N` (N = 3/5/7/9), `Snapdragon X` / `X Elite` / `X Plus`, `Apple Mn` (`Pro`/`Max`/`Ultra`). The manufacturer prefix is omitted. Anything else is `UNPARSEABLE`. | `Core i5`, `Core 5`, `Ryzen 7`, `Snapdragon X` |
| `ram_capacity_gb` | Exactly one total per field, from one labeled row in one of three shapes: a single amount (`16GB`), a single layout counted as count × size (`2x 8GB`, `16GB*1`), or an amount followed by its own parenthesized layout with the same total (`16GB (2x 8GB)`). Every capacity mention is counted before deduplication, so repeated equal amounts (`8GB + 8GB`), any other combination, or more than one labeled row is `AMBIGUOUS`. TB is unparseable for RAM. | `16` |
| `storage_capacity_gb` | As RAM, with `TB × 1000`. Several drives (`512GB SSD + 512GB HDD`, `512GB SSD + 1TB HDD`) are `AMBIGUOUS`, never summed or collapsed. | `512`, `1000` |
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
```

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

- Retailer matching uses `TARGET_MASTER` only. Shopee `source_model_id` links stay in
  `SOURCE_MODEL_MAP` and are neither read nor written here.
- Bundle-flagged offers are matched like any other offer; `bundle_flag` is carried into the results
  so Phase 9 can decide price comparability.
- No result is written to Google Sheets, and no target or mapping is promoted.

## Limitations

- **Only synthetic evidence.** Extraction rules were derived from the adapters' synthetic fixtures
  and documented reviewed pages. No real target snapshot or saved real observation has been run.
- **Canonical conventions need owner confirmation.** `cpu_series` uses the family form above
  (`Core i5`, not `Intel Core i5-13420H`), and `model` uses retailer wording. `TARGET_MASTER`
  rows must use the same forms or offers will be `UNMATCHED` (fail-safe, but no match).
- **Model coverage.** Only Laptop Factory and single-variant PC Central offers can supply `model`.
  DataBlitz, PC Express, Octagon, and multi-variant PC Central offers always go to review.
- **No observation store.** No command yet saves live adapter observations to the CSV this command
  reads; persisting collected observations is pilot/Phase 9 work.
- Unsupported CPU families (for example Celeron, Pentium, Athlon), multi-drive storage, and
  variant-specific RAM/storage go to review rather than being guessed.
