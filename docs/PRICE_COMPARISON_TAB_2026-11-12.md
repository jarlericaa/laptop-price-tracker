# PRICE COMPARISON: the business-facing tab (2026-11-12)

`PRICE COMPARISON` answers one question for Shopee business analysts: **is this Shopee laptop
variation cheaper or more expensive than the same laptop configuration at the other retailers?**
It is the only visible price tab. Every `PILOT_PRICE_*` tab and `TOP_100_SHOPEE_VARIATIONS` stays in
the workbook as a hidden technical source (layout `variation-business-1`, publisher
`variation-sheets-3`).

Code: `src/laptop_tracker/price_comparison_view.py` (display rules, pure and unit-tested), the
`price_plan` / `price_view_issues` functions and the create / show / hide steps in
`src/laptop_tracker/variation_publication.py`. Tests: `tests/test_price_comparison_view.py`.

## Architecture decision

`PRICE COMPARISON` is a **projection** of the same saved report rows the technical tabs are built
from. It selects, ranks, matches and prices nothing.

- `PILOT_PRICE_COMPARISON` (and the other technical tabs) keep their headers, data and links, so
  history, review, run-log and troubleshooting workflows are unchanged. They are hidden, not
  deleted; unhide one from the sheet list to troubleshoot a missing price.
- A projection (rather than reshaping the 140-column technical tab) keeps the matching and report
  schema untouched and lets the business layout change without a data migration.
- Prices stay real numbers. The words beside an amount (`(OOS)`, `⚠ Stale`) are part of the
  cell's *number format*, so sorting, sums and downstream readers see the price and nothing parses
  a display string. Each price is a hyperlink to its listing (cell format link). The exact numeric
  source of every cell is in the hidden technical tab.

## Layout

Row 1 `LAPTOP PRICE COMPARISON`; row 2 `Reporting Week: <start> – <end> | Price Data As Of:
<timestamp> (Asia/Singapore)`; row 3 the section labels; row 4 the table header; then one row per Top 100
variation. No KPIs, charts, filters or explanatory text. Rows 1-4 and the first three columns are
frozen.

| Section | Columns |
|---|---|
| Product Details (A-I) | Rank, Shopee Seller, Laptop Model, Variation, CPU, RAM, Storage, GPU, Shopee Price |
| Retailer Prices (J-N) | DataBlitz, PC Express, Laptop Factory, PC Central, Octagon |
| Price Competitiveness (O-S) | Lowest Retailer, Lowest Price, Price Gap (₱), Price Gap (%), Competitiveness |

- **Rows**: exactly the Top 100 selection (its eligibility, GMV ranking, variation grain and order);
  Rank is the Top list's rank. Rows of one seller or one configuration are never combined.
- **Reporting Week / Price Data As Of** come from the report's metadata. *Price Data As Of* is the report
  cutoff (when the price data was taken), not the moment the tab was written; the publication time
  is in `PILOT_PRICE_RUN_LOG`.
- **CPU / RAM / Storage / GPU / Laptop Model** are read from the mapped target's recorded fields
  (`Intel Core i5-13420H`, `16GB`, `1TB`, `RTX 4050`). A blank field stays blank; nothing is
  invented.

## Retailer cells

| Shows | Meaning |
|---|---|
| `₱54,950` | Verified matching configuration, in stock. A link opens the retailer's page. |
| `₱54,950 (OOS)` | Verified matching configuration, out of stock. Shown, never used for the lowest price. |
| `No match` | The search finished and no matching configuration was verified. A close alternative (ALT 1 / ALT 2) is a *different* configuration and is also `No match`; its price is never shown here. |
| `Review` | A candidate or an excluded matching offer needs manual verification, **or** the search hit a retailer result cap, left result pages unread or stopped early, so the absence of a match is not established. Never shown as a price or linked. |
| `Not checked` | The search or observation did not complete (not fetched, failed, rate limited, incomplete, not observed this week). |
| `—` | Not compared (the row has no trusted configuration context). |

`No match` is used only when the search finished. A search that hit a result cap (the usual case:
a retailer's search returns at most ten results), left pages unread or stopped early is `Review`,
and the cell note says so. The one recorded caveat that does *not* demote `No match` is the optional
category fallback being switched off by configuration (`FALLBACK_NOT_TRIED_DISABLED`), because the
query plan itself ran to its end. Even `No match` means the searches returned nothing matching, not
that the retailer does not sell the laptop. This is display wording only: matching, scraping and the
reports are unchanged. Hover a cell
for its note; the full technical status is in `PILOT_PRICE_COMPARISON` and
`PILOT_PRICE_ALTERNATIVES`.

## Price competitiveness

- **Lowest Retailer / Lowest Price**: the report's lowest *in-stock* matching price
  (`Lowest_In_Stock_Price`, built only from offers recorded in stock). Out-of-stock prices, unknown
  stock and similar-but-different configurations never compete. A tie lists every retailer in
  column order. No eligible price shows `—`; there is no fallback.
- **Price Gap (₱)** = Shopee Price − Lowest Price. **Price Gap (%)** = gap ÷ Lowest Price × 100
  (rounded to 0.01 point, display only).
- **Competitiveness**: positive gap `Retailer cheaper` (red tint); negative `Shopee cheaper`
  (green); zero `Same price` (neutral); no valid in-stock matching price or no Shopee price
  `No comparison` (grey), with `—` for both gaps. Missing data is never zero.
- Only the gap and competitiveness cells carry a colour. They are applied by the publisher as cell
  fills (like the other tabs), not as conditional-format rules, so repeated publications cannot
  accumulate rules.

## Stale Shopee prices

The tracker's existing rule (the snapshot is older than 7 days at the report cutoff) is unchanged.
A stale price reads `₱49,990 ⚠ Stale` with a soft amber fill, and its gap is still calculated. The
snapshot date and kind are in the cell note and in the hidden tabs; there is no date column.

## How the publisher maintains it

- `PRICE COMPARISON` is the **one tab the publisher may create** (when absent, next to the pilot
  tabs). Every other tab must already exist; the retired tabs are never recreated.
- It is written last, after the technical tabs it is projected from, then read back like every
  other tab (values, numeric types, links, formats, notes, fills, frozen panes, widths).
- Only after every data tab passes the readback does the publisher show `PRICE COMPARISON` and hide
  the technical tabs (one batch, idempotent). A publication that fails verification leaves the
  visibility as it was. The tab's earlier visibility is saved in each backup and `--rollback`
  restores it; a tab the publication created has no earlier state and is left as it is.
- Repeated runs replace the tab's content and layout in place: no duplicate tabs, header rows,
  filters or rules, and nothing is unhidden. The tab has no filter controls. Its layout is
  re-applied on every publication, so manual formatting of this tab does not survive a publication.
- The publisher version changed to `variation-sheets-3`, so the publication ID of an already
  published run changes and that run can be published again to create the tab (the old
  `variation-sheets-2` row in the run log is left as it is).
- A foreign tab already called `PRICE COMPARISON` with other content is refused, not overwritten.

## Not covered

- It has not been written to the live workbook by this change; render, number-format round trip of
  `⚠` and the hyperlink-on-number cells are verified against the fake workbook only.
- A cell's note is the only explanation of a status in the tab; there is no legend row.
