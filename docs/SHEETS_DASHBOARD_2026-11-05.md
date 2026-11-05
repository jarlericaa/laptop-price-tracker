# Compact Sheets dashboard (layout `variation-compact-1`, 2026-11-05)

The comparison tab had 80 columns and retailer statuses of hundreds of characters. The next normal
publication writes a compact layout instead. **This is presentation only**: ranking, mappings,
discovery, matching rules, alternative selection, price eligibility, stock rules, minima and history
are untouched, and every published number is checked against the saved reports (see
[Validation](#validation)).

Nothing was written to the live workbook while building this. The weekly driver still stops at the
publish preview; the existing `publish_variation_prices.py --write-sheets` step writes the new
layout (see [Next run](#next-run-and-commands)).

## Versions

| | Before | Now |
|---|---|---|
| Publisher (`PUBLISHER_VERSION`) | `variation-sheets-1` | `variation-sheets-2` (a new publication ID) |
| Sheet layout (`LAYOUT_VERSION`, `layout_version` in the contract) | none | `variation-compact-1` |
| Report schema (`SCHEMA_VERSION`) | `shopee-variation-sheets-1` | unchanged; no report file or machine field changed |

`config/shopee_variation_sheet_contract.json` now carries the 140-column comparison header and keeps the
earlier 80-column header under `earlier_headers`, so a live tab that still has it is recognised as this
publisher's own output and replaced, never refused as foreign. A tab with any other header is still
refused.

## `PILOT_PRICE_COMPARISON`: the visible block

Two banner rows (reporting week and cutoff; colour legend) sit above the header, once per sheet. The
header is row 3, frozen with the first three columns; the filter starts at the header, so sorting never
moves the banner. No merged cells anywhere in the table.

| # | Column | Notes |
|---|---|---|
| 1 | Rank | the variation's own rank |
| 2 | Laptop | brand, series, model of the mapped target (note: row ID, source key, mapping) |
| 3 | Shopee seller | |
| 4 | Shopee variation | the selected variation |
| 5 | Target specs | `15.6" · Ryzen 5 · 16GB · 512GB · GPU`, recorded fields only |
| 6 | Shopee snapshot price (PHP) | numeric, linked to the Shopee listing |
| 7 | Shopee snapshot date | source date, kind of date, `STALE` |
| 8-17 | per retailer: `<R> price (PHP)`, `<R> result` | DataBlitz, Laptop Factory, PC Express, PC Central, Octagon (the tab's existing order) |
| 18-19 | Lowest in-stock matching price and retailer(s) | matching offers only |
| 20-21 | Lowest in-stock alternative price and retailer(s) | alternatives only; never labelled cheapest *matching* |
| 22 | Row coverage | `2 match · 1 alt · 2 no price` |

## Collapsed column groups (to the right)

Nothing was dropped: all earlier columns remain, and the detail fields below were added. Each group is a
native Sheets column group, collapsed, one level deep (click `+` above the column letters).

1. **Expanded specifications**: Shopee item, brand, series, model, screen, CPU family and model, RAM, storage, GPU.
2. **Shopee details**: listing link, full price status, observed date, date basis.
3. **`<R> details`**, one group per retailer, 15 columns: full status text, machine status (price status,
   tag, match basis, candidate status, selection reason), differences (target → retailer), unknown fields
   and limits, warnings, stock, observed timestamp, product link, candidate (recheck) link, recheck note,
   search / fetch outcome (full original reason), offer details, observation ID, association ID, and an
   internal link to the alternatives / review rows.
4. **Other price minima and bases**: every minimum that is not visible (all-stock matching and alternative
   minima, in-stock bases) with its basis, matching and alternative retailers, comparison basis.
5. **IDs, GMV and run metadata**: comparison row ID, source key, shop, item and model IDs, target, mapping
   status, M-1 / MTD / ranking GMV, sales snapshot, reporting week, status, cutoff, row grain.

## Result labels

`src/laptop_tracker/result_labels.py` builds each label from the structured fields the reports already
carry (price status, comparison tag, stock, match basis, the alternative record's `comparison_basis`,
the recheck status, the recorded discovery reason). It never truncates a sentence and never changes a
business outcome.

| Situation | Label |
|---|---|
| Eligible matching offer | `MATCH` |
| Match on listed specs (exact product unknown) | `MATCH · Listed specs` |
| Matching offer, price kept, out of stock | `MATCH · Out of stock` |
| Both | `MATCH · Listed specs` / `Out of stock` (two lines) |
| One established difference | `ALT 1 · CPU` |
| Two established differences | `ALT 2 · CPU, Storage` |
| Limited comparison basis (model family or CPU series only) | `... · Limited` |
| Several retailer variants state the configuration | `RECHECK · Variant ambiguous` |
| Required specs not extracted | `RECHECK · Specs incomplete` |
| Contradictory page evidence | `RECHECK · Spec conflict` |
| Other unconfirmed candidates | `RECHECK · Different model`, `Different config`, `Target ambiguous`, ... |
| Relevant candidate waiting for collection | `PENDING · Not fetched` (also `Fetch failed`, `Not searched`) |
| Collection blocked by throttling | `DEFERRED · Rate limited` |
| Matching offer excluded by purchase terms | `EXCLUDED · Bundle terms` (also `Restricted price`, `Preorder`, ...) |
| Searches returned nothing | `NO RESULTS` |
| Results returned, none relevant | `NO RELEVANT` |
| Search capped or pages unread | `... · Search limited` |
| Search stopped early | `INCOMPLETE · Search limited` |

Rules the tests pin:

- `NO RESULTS` / `NO RELEVANT` describe what searches returned; they never say the retailer does not sell
  the laptop, and finishing the query plan is not exhaustive catalog coverage (stated in the cell note).
- Material qualifiers stay on the label: stock, listed specs, limited basis, limited search, STALE. Up to
  two short lines are used rather than hiding one.
- Unknown fields stay in the details and notes. They are never an agreement and never a difference; the
  total difference budget (including GPU) is the report's.
- A label starting `MATCH` or `ALT` appears only on a cell whose price is shown, and a price is shown only
  for `SELECTED` / `ALTERNATIVE_SELECTED`. Excluded or unresolved results have a blank price.
- `Search limited` is read from the wording `pilot_coverage.DiscoveryLogs.stage` records; a test builds the
  real note, so rewording it cannot silently turn a capped search into a complete one.

Colours (always with their text label): green `MATCH`; yellow `ALT 1`; orange `ALT 2`; violet for
`RECHECK`, `EXCLUDED`, `NOT COMPARED`; neutral grey for results without a price (`NO RESULTS`,
`NO RELEVANT`, `PENDING`, `DEFERRED`); soft amber for a stale snapshot date. Banding is written before the
result fills, so it never covers a result colour.

## Notes, links and navigation

- Each retailer **result cell** has a note: the label's meaning, the full original status, the actual
  configuration, differences (target → retailer), unknown fields and limits, limited basis, stock, the
  actual observation timestamp, the selected offer or unconfirmed candidate URL, the search / fetch outcome,
  warnings, and the observation / association / context IDs. The same fields are in the details group.
  Notes are cleared and rewritten on every publication of the comparison, overview, top and alternatives
  tabs, so a note from an earlier run cannot stay on a new result.
- **Prices are numeric** (`numberValue`) with the amount unchanged. Eligible price cells carry a native link
  to the selected product page. Missing prices are blank, never zero. An unconfirmed candidate's URL is in
  the details group and `PILOT_PRICE_REVIEW`, not on a price.
- **Internal links** (`#gid=<tab>&range=A<row>`) from each `alternatives / review link` cell, and from `ALT` and
  review result cells, go to the first matching row of `PILOT_PRICE_ALTERNATIVES` / `PILOT_PRICE_REVIEW`.
  The row is found by comparison row ID and retailer in the final planned tab, never by rank or position,
  and recomputed each time (no accumulation).

## Shopee snapshot price

The column is **Snapshot price**, never "current" or "live". The date column shows the report's own source
date and what kind it is: `2026-11-05 · sales extract` (the extract's date) or
`2026-10-01 · monthly record` (the M-1 record's date, **not** an exact price time), plus `STALE` under the
report's existing seven-day rule. Rebuilding a report never refreshes a date.

## `PILOT_PRICE_OVERVIEW`

Still 26 columns and a concise price matrix, with the same banner rows. Prices stay numeric; the number
format appends the label (`₱19,990.00 · MATCH · Out of stock`), listed-spec prices stay italic, fills and
notes are the comparison's, eligible prices link to their product pages, and a stale Shopee price shows
`· STALE`.

## Publisher mechanics

- **Existing live layout.** The publisher reads the live conditional-format rules and column groups. The
  owner's rules on the comparison and overview tabs are keyed to the old column positions (21 rules each,
  including `INDIRECT` lookups into the old comparison columns) and would colour the wrong cells, so they are
  removed from those two tabs and the result fills are written as cell formats instead (read back and
  verified). Rules on other tabs (alternatives, history, review, run log, top list) are untouched.
- **Idempotent.** Each write first removes any column groups and rules on the two managed tabs, then adds
  exactly its own groups; the readback fails if the groups differ or a rule remains. A repeated publication
  leaves the same values, notes, links, groups and guide rows (tested).
- **Rollback.** Snapshots now include column groups, hidden columns and merges (they already held the
  conditional formats); `--rollback <publication folder>` restores them all, removing the groups and
  rules the publication added. A backup taken by the previous publisher has no groups and does hold the
  owner's 21 rules per tab, so restoring it puts those rules back (earlier rollbacks left rules alone).
- **TRACKER_GUIDE.** The owner's guide is never replaced. Rows whose label is in column A (`STATUS`,
  `Shopee prices`, `MATCH`, `ALT 1`, `ALT 2`, `RECHECK`, `Current results`, `Next step`, `Data dates`,
  `Minimum prices`, `Layout prepared`) get new column B text only; `Compact view`, `Expanded columns` and
  `Result colours` are appended once, formatted and merged like the `Data dates` row. The obsolete
  "awaiting the first variation-level publication" wording is replaced. It is snapshotted first, verified
  after, and a problem with it is reported without undoing the data tabs. The preview prints what would change.
- **Stale header notes.** The comparison tab's old header note ("Layout prepared 2026-10-27; no
  variation-level data has been published ...") is gone because the tab's notes are now cleared and
  rewritten each time.
- **Read quota.** The six-tab readback could exhaust Sheets' read quota and report a false
  `PARTIAL_FAILURE` after a successful write (2026-11-05). Readback reads that fail with a 429 are now
  retried after 20 / 30 / 45 / 60 s; other errors are raised as before.
- **Request size.** The comparison tab's batch is about 2.4 MB (the history tab already sends 1.6 MB).

## Validation

- `tests/test_result_labels.py` (21 tests): label semantics above, including the qualifiers, the
  structured Limited basis, rate limits and the real discovery note.
- `tests/test_dashboard_layout.py` (29 tests): visible block and retailer order, groups tile every column once,
  banner rows, numeric linked prices, out-of-stock handling, fills never standing alone, full detail
  preservation, stale snapshot, repeat publication, an earlier live layout with owner rules and a stale note,
  guide update / append / skip / rollback, navigation by ID, presentation-only agreement, the 429 retry.
  The in-memory fake workbook (`tests/test_sheet_publication.py`) now models groups, rules, merges and hidden
  columns. Mutation check: removing the group / rule clearing makes 4 of these fail.
- The whole suite: 2,455 passed, no failures (no baseline failures remained to report). Ruff is clean
  for every file touched; the 8 existing `E501` findings in `catalog_proposals.py` and `page_cache.py`
  are not part of this change.
- **Presentation-only check on the saved 2026-11-05 run.** The plan of the previous publisher (git `HEAD`)
  and the new one were built from the same inputs and compared: top list, alternatives, history and review
  tabs are identical cell for cell (100 / 288 / 871 / 268 rows); all 8,000 earlier comparison cells and all
  2,600 overview cells agree in kind and value; no earlier link was lost; variation order and IDs match.
  This found two defects of the first version (the matching and alternative minimum-retailer cells dropped the
  "includes stated items" note); both are fixed and have regression tests. `source_agreement_issues` keeps
  this check in the publisher: a published rank, ID, snapshot price, retailer price (and its link), minimum,
  GMV or alternative that differs from the report stops the build.
- Publisher preview on the live workbook (read-only scope): destinations accepted (the live comparison tab is
  recognised as the earlier layout), 331 internal links, 915 comparison and 808 overview notes planned,
  guide: 11 row texts would change and 3 rows would be appended. No Sheet changed.

## Limits of what was verified

- There is no Sheets renderer in this environment. Fills, widths, fonts, wrapping, frozen panes and column
  groups were verified through the request stream and a readback against a fake, and the field masks and
  guide plan against the real API **read-only**; how the page looks, and that Sheets draws adjacent groups as
  separate toggles, still needs a look at the first live publication.
- Result colours are cell formats, not rules: sorting moves them with their rows, but a hand edit of a
  result cell's text will not recolour it. Conditional formatting was removed from the two tabs on purpose
  (above).
- `Search limited` and the not-fetched sub-labels read recorded wording and decision codes; a code the
  mapping does not know falls back to a neutral label with the code in words (never a MATCH or ALT).

## Next run and commands

No new command. `run_variation_week.sh` still ends at `publish_preview` (it now shows the banner rows,
the guide plan and the layout version). Publishing is the existing explicit step:

```bash
cd /home/phsp8184/laptop-price-tracker && source .venv/bin/activate
W="$(cat /tmp/current_weekly_run.txt)"
python publish_variation_prices.py --write-sheets --variation-dir "$W/shopee_variations" \
  --reports-dir "$W/variation_reports" --context-report-dir "$W/context_reports" \
  --collection-dir "$W/discovery/collection" --output-root "$W/publications"
# rollback if needed: python publish_variation_prices.py --rollback <publication folder>
```

The first write replaces the 80-column live tabs with the compact layout, removes the old rules, adds the
groups and refreshes the guide; later runs keep it. The saved 2026-11-05 run can be republished the same way
(`data/output/weekly_runs/20261105T032441Z`) without any new retailer request.
