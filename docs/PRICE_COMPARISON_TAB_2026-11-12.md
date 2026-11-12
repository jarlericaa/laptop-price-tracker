# PRICE COMPARISON and PRICE REPORT: the business-facing tabs (2026-11-12)

Two visible tabs answer the business questions; every `PILOT_PRICE_*` tab and
`TOP_100_SHOPEE_VARIATIONS` stays in the workbook as a hidden technical source (layout
`variation-business-2`, publisher `variation-sheets-4`).

| Tab | Columns | Use it to |
|---|---|---|
| `PRICE COMPARISON` | 19 | See at a glance whether a Shopee variation is cheaper or dearer than the same laptop configuration at other retailers. Prices and `—` only. |
| `PRICE REPORT` | 22 | See how each of the five retailers was covered: price, a short result (`Match`, `Alt`, `Review`, `No match`, `Search limited`, `Not checked`), the two in-stock minima and a coverage count. |

Both keep one row per Shopee variation in the Top 100's order, show the same Shopee price, and carry
**no cell notes**. Code: `src/laptop_tracker/price_comparison_view.py` and `price_report_view.py`
(display rules, pure and unit-tested), the `price_plan` / `report_plan` / `*_view_issues` functions and
the create / show / hide steps in `src/laptop_tracker/variation_publication.py`. Tests:
`tests/test_price_comparison_view.py`, `tests/test_price_report_view.py`.

## Architecture decision

Both tabs are **projections** of the same saved report rows the technical tabs are built from. They
select, rank, match and price nothing.

- `PILOT_PRICE_COMPARISON` (and the other technical tabs) keep their headers, data, notes and links,
  so history, review, run-log and troubleshooting workflows are unchanged. They are hidden, not
  deleted; unhide one from the sheet list to troubleshoot a price. The full explanation behind every
  result (differences, unknown fields, observation times, search outcome) lives there.
- Prices stay real numbers. The words beside an amount (`(OOS)`, `⚠ Stale`) are part of the cell's
  *number format*, so sorting, sums and downstream readers see the price and nothing parses a display
  string. Each price is a hyperlink to its listing (cell format link).

## PRICE COMPARISON (minimal view)

Row 1 title; row 2 `Reporting Week: <start> – <end> | Price Data As Of: <timestamp>
(Asia/Singapore)`; row 3 the section labels; row 4 the header; then one row per variation. No KPIs,
charts or filters. Rows 1-4 and the first three columns are frozen.

| Section | Columns |
|---|---|
| Product Details (A-I) | Rank, Shopee Seller, Laptop Model, Variation, CPU, RAM, Storage, GPU, Shopee Price |
| Retailer Prices (J-N) | DataBlitz, PC Express, Laptop Factory, PC Central, Octagon |
| Price Competitiveness (O-S) | Lowest Retailer, Lowest Price, Price Gap (₱), Price Gap (%), Competitiveness |

**Retailer cells** are a price or `—`:

| Shows | Meaning |
|---|---|
| `₱54,950` | Verified matching configuration, in stock. A link opens the retailer's page. |
| `₱54,950 (OOS)` | Verified matching configuration, out of stock. Shown, never used for the lowest price. |
| `—` | **No verified comparable retailer price is available in this report.** It does not mean the retailer does not carry the product: the search may have finished without a match, hit a result cap, found only a close alternative or a candidate to review, or never completed. `PRICE REPORT` says which. |

A close alternative is a different configuration, so its price is never shown here.

**Price competitiveness.** *Lowest Retailer / Lowest Price* are the report's lowest *in-stock*
matching price (built only from offers recorded in stock; a tie lists every retailer). Out-of-stock
prices, unknown stock and alternatives never compete, and there is no fallback: no eligible price is
`—`. *Price Gap (₱)* = Shopee Price − Lowest Price; *Price Gap (%)* = gap ÷ Lowest Price (display
rounded to 0.01 point). *Competitiveness*: positive gap `Retailer cheaper` (red tint), negative
`Shopee cheaper` (green), zero `Same price` (neutral), no valid price on either side `No comparison`
(grey, gaps `—`). Missing data is never zero. Only the gap and competitiveness cells carry a colour,
as cell fills (not conditional-format rules, so repeated publications cannot accumulate rules).

A stale Shopee snapshot (older than 7 days at the report cutoff) reads `₱49,990 ⚠ Stale` with a soft
amber fill and is still compared. *Price Data As Of* is the report cutoff (when the price data was
taken), not the moment the tab was written; the publication time is in `PILOT_PRICE_RUN_LOG`.

## PRICE REPORT (detailed view)

The first 22 columns of the hidden `PILOT_PRICE_COMPARISON`, in its order and with its side-by-side
retailer structure, without its ~118 detail and debugging columns (those stay in the hidden tab).
Same banner as `PRICE COMPARISON`; rows 1-4 and the first three columns frozen.

| Section | Columns |
|---|---|
| Product & Shopee (A-G) | Rank, Laptop, Shopee Seller, Variation, Target Specs, Shopee Price, Shopee Price Date |
| Retailer Comparison (H-Q) | DataBlitz, Laptop Factory, PC Express, PC Central, Octagon: a *Price* and a *Result* each |
| Comparison Findings (R-V) | Lowest Matching Price, Lowest Matching Retailer, Lowest Alternative Price, Lowest Alternative Retailer, Coverage |

The two lowest prices are the report's own **in-stock** minima (an out-of-stock price never counts,
whichever kind it is). A tie lists every retailer; no qualifying price is `—`. The matching minimum is
never built from an alternative.

**Retailer Price** is the price the report selected for the cell: a verified matching configuration or
the close alternative it displays, in stock or `(OOS)`, linked to that offer; `—` when none. The
**Result** beside it:

| Result | Meaning |
|---|---|
| `Match` | Verified matching configuration. `· Listed specs`: the specs agree but the exact product is unknown. |
| `Alt · Screen, Storage` / `Alternative` | A close alternative: a *different* configuration, shown for research. The words name what differs; `· Limited` means only a limited basis (model family or CPU series) was compared. Alternatives never count toward the lowest matching price, the price gap or the competitiveness result. |
| `· OOS`, `· Preorder`, `· Stock unknown` | Added to a `Match` or `Alt` whose stock is not verified in stock (never read as available). |
| `Review` | A candidate or an excluded matching offer needs manual verification. |
| `No match` | The search finished and no matching configuration was verified. |
| `Search limited` | The search hit a retailer result cap, left result pages unread or stopped early, so a match may exist. (The optional category fallback being switched off by configuration does *not* demote a finished search.) |
| `Not checked` | The search or observation never completed (not fetched, failed, rate limited, not observed this week). |
| `Not compared` | The row has no trusted configuration context. |

No result says a retailer does not carry the laptop. These are presentation labels: matching,
scraping and the machine statuses in the hidden tabs are unchanged.

**Coverage** counts the row's five retailer cells: `2 matches · 1 alt · 2 not priced`, `1 alt · 4 not
priced`, `No verified prices` (neither a match nor an alternative), `Not compared`. The counts come
from the cells themselves, so they always reconcile. *Not priced* is deliberately neutral.

Colours (restrained, on the Result cells only; every one also has its text): green `Match`, amber
`Alt` or `Review`, soft red for an out-of-stock result, grey for no price (`No match`, `Search
limited`, `Not checked`). A stale Shopee date cell is amber.

## How the publisher maintains both tabs

- They are the **only tabs the publisher may create** (when absent, next to the pilot tabs: `PRICE
  COMPARISON`, then `PRICE REPORT`, before the Top list). Every other tab must already exist; the
  retired tabs are never recreated.
- A created tab starts **hidden**. Both are written last, after the technical tabs they are projected
  from, then read back like every other tab (values, numeric types, links, formats, notes, fills,
  frozen panes, widths). A cross-check also confirms that every price, label kind, minimum and
  coverage text agrees with the technical comparison and with the other tab.
- Only after every data tab passes the readback does the publisher show both and hide the technical
  tabs (one batch, idempotent). A publication that fails verification leaves the visibility as it
  was, so a half-built report is never in front of readers.
- Rollback (`--rollback <publication folder>`) restores every tab that existed (values, notes,
  formats, visibility). A business tab *created by that publication* has no earlier state: it is
  **hidden** (its content is kept; a republication replaces it in place), so a report is never left
  showing data whose technical tabs were just restored. The run log is never rewritten.
- Repeated runs replace each tab's content and layout in place: no duplicate tabs, header rows,
  filters or rules, and nothing is unhidden. Neither tab has filter controls. Layout is re-applied on
  every publication, so manual formatting of these tabs does not survive a publication, and notes
  left by an earlier layout (the first `PRICE COMPARISON` carried some) are cleared.
- The publisher version is `variation-sheets-4`, so the publication ID of an already published run
  changes; see `docs/GITHUB_ACTIONS_SETUP.md` for what that means for a saved run.
- A foreign tab already called `PRICE COMPARISON` or `PRICE REPORT` with other content is refused,
  not overwritten.

## Not covered

- Visual rendering is verified through the cell properties Google stores (colours, formats, frozen
  panes), not by eye; open the tab once after the first publication.
- A tie, `Same price` and an unknown-stock price are covered by unit tests with synthetic rows (the
  saved run has none).
