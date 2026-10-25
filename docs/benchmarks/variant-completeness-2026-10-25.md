# Catalog completeness and selected variants — 25 October 2026

The follow-up adds public catalog metadata to discovery and a narrowly proven
colour/storage variant interpretation to extraction. The measured improvements
are **26 to 34 expected links recovered out of 48 audit cases**, and **9 to 15
exact comparable offers across 45 page requests**. These are separate experiments.
Neither result establishes full tracker coverage or general matching accuracy.

## Broader link discovery

The existing `leads_v1.csv` audit has 53 rows, of which 48 claim product pages.
It contains repeated URLs and target claims that were not independently confirmed.
We retained all 48 product-page cases, using target-record model/name and optional
hardware clues, up to three queries and five results per query. Expected URLs were
evaluation labels only; they never seeded the index or query planner.

| Local search on the same complete sitemap snapshot | Expected links found |
|---|---:|
| Sitemap URL/title words, stable URL ordering | 26/48 |
| Public feed metadata plus optional specification ranking | 34/48 |

This comparison combines two discovery changes; it is not an ablation attributing
the improvement to the feed alone. Query terms remain required. Additional clues
rank candidates and cannot turn a lead into a configuration match.

The full sitemap index contains 41 of the 48 expected URLs. The seven absent cases
are L007, L035, L039, L041, L042, L043 and L044. Some use variant-specific URLs that
the sitemap does not list. Other misses still arise from sparse metadata or result
ranking. [All 48 outcomes](catalog-expanded-discovery-2026-10-25.csv) preserve the
denominator and unverified audit-label status. This is a known convenience cohort,
not a held-out or random accuracy study. Native search was not rerun on this cohort.

## Fuller catalog coverage

| Retailer | Product URLs | Product sitemaps read / advertised |
|---|---:|---:|
| Laptop Factory | 905 | 1/1 |
| DataBlitz | 36,736 | 37/37 |
| PC Express | 2,830 | 3/3 |
| PC Central | 27 | 1/1 |
| Octagon | 715 | 1/1 |
| Total | 41,213 | 43/43 |

The sitemap refresh used 53 HTTP requests, 14:13:19–14:21:24 Manila time.
The earlier ten-case benchmark used a deliberately partial DataBlitz snapshot;
its original results remain documented separately.

An opt-in Laptop Factory public WooCommerce Store API scan returned 278 products
in three pages, reaching the end of that feed. It used six requests including its
own robots/root/product-sitemap refresh, 14:29:10–14:30:01 Manila time. This enriches
278 of 905 sitemap entries; it does not establish full website inventory. The
[public API documentation](https://developer.woocommerce.com/docs/apis/store-api/resources-endpoints/products/)
describes published product data. Feed prices and stock are excluded from the index.

All advertised sitemaps read does not mean all saleable variants indexed. Missing
sitemap entries, unpublished feed items, stale pages and variant URLs remain possible.
Local queries send no network requests. Refresh and current-page fetches still do.

## Same-byte extraction and matching comparison

We captured 39 unique audit URLs plus six explicitly selected PC Central variant
URLs, retaining existing same-day cached responses at their original times. Of 45
requests, 44 returned pages, one returned HTTP 404, and one returned an unselected
multi-variant page that could not be safely parsed. The remaining 43 observations
were matched against the same validated 232-target supplied-workbook snapshot.

Baseline source was commit `0e910c7`, loaded in a separate source directory with the
same Python environment and configuration. The updated source replayed identical
saved HTML bytes and original observation timestamps with networking disabled.

| Outcome across all 45 requests | Baseline | Updated |
|---|---:|---:|
| EXACT | 11 | 17 |
| REVIEW_REQUIRED | 17 | 11 |
| UNMATCHED | 15 | 15 |
| PARSE_ERROR | 1 | 1 |
| FETCH_ERROR | 1 | 1 |
| Exact and price-comparable (subset of EXACT) | 9 | 15 |
| Listed-spec associations (separate policy) | 2 | 2 |

Only the six bound PC Central variant outcomes changed. They represent **two
hardware configurations**, with colour variations, rather than six independent
families. All other 39 request outcomes stayed the same.

The complete, adapter-proven picker names a fixed product prefix, known colours
and storage capacity. Every picker capacity must also be stated in a Storage-labelled
product row. The selected variant supplies storage; the shared description must
explicitly state its fixed RAM, CPU and screen. Missing RAM is never copied from a
target, and conflicting hardware, incomplete pickers or unexplained dimensions fail
this rule. PC Central's `Chip` heading is recognized as CPU evidence. The matching
rules version changes so stored results from the old interpretation are not reused.

The four 256GB offers were PHP 47,999.00 and the two 512GB offers PHP 52,999.00,
observed at 14:19:03–14:19:53 Manila time. Stock remains separately recorded, including
out-of-stock variants. [Selected offers](bound-variant-offers-2026-10-25.csv) include
their exact URLs and timestamps. [All page outcomes](variant-page-comparison-2026-10-25.csv)
include source hashes and both matching results. Replay does not refresh those prices.

## Sheets and verification

The new integration test runs actual selected-variant extraction, matching,
observation storage, report selection, report validation and the existing Sheets
publisher. It verifies separate 256GB and 512GB prices, write/readback success and
preservation of a protected input tab. Its prices are synthetic fixtures, separate
from the live observations above.

A separate temporary native Google Sheet was created in the user's connected
account with the six actual observations, comparison metrics and unresolved cases.
Its values and timestamps were read back, then its exported workbook was rendered
to verify conversion. This proves the sample upload through the connected account.
It does not configure unattended Sheets credentials on SCL or update the original
production tracker. The original tracker still returns permission denied.

Validation on SCL: **1,859 tests passed**, full Ruff check and diff checks passed.
Environment: Python 3.11.15, pandas 2.3.3. The existing pandas 3.1.0 baseline failure
remains documented in `CATALOG_INDEX.md`; dependency policy is unchanged.

Remaining review cases include a product name with an appended part number, screen
marketing size versus stated diagonal, ambiguous RAM, conflicting model rows and
real hardware differences. The change does not loosen those into accepted prices.

Ignored SCL evidence directories:

```text
data/output/catalog_index_full_20261025/
data/output/catalog_feed_full_20261025/
data/output/catalog_expanded_cohort_20261025/
data/output/variant_gap_baseline_20261025/
data/output/expanded_page_capture_20261025/
```

The provided workbook is a snapshot, not a fresh read of the live tracker. Private
target records, raw HTML and credentials are not committed. Public summaries above
contain retailer URLs, observed public facts and evaluation outcomes only.
