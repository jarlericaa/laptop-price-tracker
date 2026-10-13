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
