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

Row 1 `LAPTOP PRICE COMPARISON`; row 2 `Week: Nov 16–22, 2026  |  Prices as of: Nov 22, 6:58 PM SGT`
at the left and the retailer coverage chip over the retailer columns; row 3 the section labels; row 4
the table header; then one row per Top 100 variation. No KPIs, charts, filters or explanatory text.
Rows 1-4 and the first three columns are frozen. The colours, sizes and notes are in
[Dashboard design](#dashboard-design-2026-11-22).

| Section | Columns |
|---|---|
| Product Details (A-I) | Rank, Shopee Seller, Laptop Model, Variation, CPU, RAM, Storage, GPU, Shopee Price |
| Retailer Prices (J-N) | DataBlitz, PC Express, Laptop Factory, PC Central, Octagon |
| Price Competitiveness (O-S) | Lowest Retailer, Lowest Price, Price Gap (₱), Price Gap (%), Competitiveness |

- **Rows**: exactly the Top 100 selection (its eligibility, GMV ranking, variation grain and order);
  Rank is the Top list's rank. Rows of one seller or one configuration are never combined.
- **Week / Prices as of** come from the report's metadata. *Prices as of* is the report cutoff (when
  the price data was taken, in Singapore time), not the moment the tab was written; the publication
  time is in `PILOT_PRICE_RUN_LOG`.
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
and the header note says so. The one recorded caveat that does *not* demote `No match` is the optional
category fallback being switched off by configuration (`FALLBACK_NOT_TRIED_DISABLED`), because the
query plan itself ran to its end. Even `No match` means the searches returned nothing matching, not
that the retailer does not sell the laptop. This is display wording only: matching, scraping and the
reports are unchanged. The reason behind a status is not repeated on every cell: the header note of
the retailer columns says what each state means, and the full technical status is in
`PILOT_PRICE_COMPARISON` and `PILOT_PRICE_ALTERNATIVES`.

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
- The three result cells of a row (gap, gap %, label) share one colour. All colours are applied by the
  publisher as cell styles (like the other tabs), not as conditional-format rules, so repeated
  publications cannot accumulate rules.

## Stale Shopee prices

The tracker's existing rule (the snapshot is older than 7 days at the report cutoff) is unchanged.
A stale price reads `₱49,990 ⚠ Stale` with an amber fill, and its gap is still calculated. The
snapshot date and kind are in that cell's note and in the hidden tabs; there is no date column.

## Dashboard design (2026-11-22)

A presentation change only: values, links, number formats, statuses, price eligibility, the lowest
price, the gaps and the competitiveness are exactly those above (the displayed text of a saved run
was compared before and after: only the row-2 wording differs). Code: `price_comparison_view.py`
(which state gets which style, widths), `variation_publication.price_banner` (the header rows) and
`sheet_publication` (the `DASHBOARD` theme, `DASHBOARD_STYLES`, `style_of`). Tests:
`tests/test_price_dashboard_style.py`.

**Hierarchy.** Row 1 is the title (white on navy `#1B365D`, 18 pt). Row 2 is a quiet strip: the week
and cutoff at the left, the coverage chip over the retailer columns. Row 3 names the three sections on
light bands (blue, teal, slate). Row 4 is the column header: navy over the product and result columns,
a lighter navy `#294D73` over the five retailers, and Shopee orange `#EE8B35` for the Shopee price,
whose column carries a very light orange tint as its accent. A thin rule separates the three
sections. Rows alternate white and `#F6F8FC`; a status colour always wins over the banding.

**Retailer cells (J-N)**, one style per state, none of which changes the text or the state:

| State | Text | Style |
|---|---|---|
| verified, in stock | `₱54,950` (a link) | teal fill, deep-teal text, right |
| verified, out of stock | `₱52,990 (OOS)` | peach fill, brown text, right (never used for the lowest price) |
| verified, stock unknown | `₱52,990 (Stock unknown)` | slate fill, right (never used for the lowest price) |
| `Review` | `Review` | amber fill, dark-amber text, centred |
| `Not checked` | `Not checked` | slate fill, grey text, centred |
| `No match` | `No match` | no fill, very light grey text, centred |
| not compared | `—` | no fill, light grey, centred |

There is no separate state for an invalid or conflicting offer in the display model: a conflicting
offer is `Review`. Only a verified matching price is ever a number, and only a number is teal or peach.

**Result columns (O-S).** `Shopee cheaper`: green fill, dark-green text. `Retailer cheaper`: red fill,
dark-red text. `Same price`: soft blue. `No comparison`: slate, with quiet `—` for the lowest
retailer, price and both gaps. The gap and gap % share the label's colour, so the eye lands on Q-S;
the other columns keep their neutral look.

**Coverage chip.** Neutral slate `Coverage: 5/5 retailers checked` when every retailer was checked,
amber and bold `Coverage: 4/5 retailers checked | Laptop Factory incomplete` (or `Partial comparison`)
when some were not, red when none were. It is a separate cell, so the week never shares a line with
it; only an incomplete chip carries a note. No chip appears when no discovery summary is known: no
coverage is claimed.

**Notes.** Row cells carry no repeated explanations (about 700 before). What a state means is in the
header notes (11). Notes remain only where a reader needs them beside the number: a listed-spec match,
the lowest price resting on one, a stale or missing Shopee price, and the incomplete coverage chip.
The audit detail behind every status is unchanged in the hidden technical tabs.

**Sizes.** Rows 46 / 28 / 28 / 38 px for the title, week, section and header rows, 40 px per data row.
Widths (px): Rank 48, Seller 130, Model 190, Variation 170, CPU 160, RAM 62, Storage 72, GPU 100,
Shopee Price 125, each retailer 124, Lowest Retailer 135, Lowest Price 104, gap 100, gap % 92,
Competitiveness 132. Prices right-aligned, short statuses centred.

**Persistence.** The publisher applies all of it on every publication: it resets the tab's formatting,
draws the layout, then the cell styles, so nothing from an earlier run survives and no rule
accumulates. Readback still checks values, links, number formats, notes, frozen panes, widths and the
background of every styled cell; text colour and alignment are written but not read back.

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
- There is no legend row: what a state means is in the header notes (hover a column header).
- The dashboard design below was inspected as a mock rendered from the publisher's own requests, not
  in Google Sheets: look at the first real publication.
