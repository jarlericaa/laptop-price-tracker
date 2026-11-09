# Cached retailer catalog search

`search_catalog_index.py` searches saved retailer sitemap data locally, then optionally
fetches candidate product pages through the existing HTTP fetcher. It requires no
Codex, ChatGPT, LLM, search API, subscription or API key. The index can also feed
the existing discovery command through the opt-in `--catalog-index-run` flag.

The existing `discover_catalog_leads.py` already collects retailer sitemaps. Its lead
finder accepts only `MODEL_CODE` targets. This new index reuses the collector's raw
responses and searches product names, identifiers, URL handles and sitemap titles.
It supports punctuation variations by indexing adjacent joined words. All query
terms must be present; this is candidate discovery, not fuzzy configuration matching.

See the [measured benchmark](benchmarks/catalog-index-2026-10-25.md). On its ten
predeclared cases: native search found 6/10 links, the index found 9/10, and their
union found 10/10. Native plus the existing sitemap lead finder covered 9/10 on
the same saved catalog. These are link-recovery results, not accepted prices.

The [completeness and variant follow-up](benchmarks/variant-completeness-2026-10-25.md)
records the larger refresh, 48-case discovery comparison, 45-page matching replay
and Sheets validation. It also documents the narrowly proven colour/storage rule
added to extraction, with remaining ambiguities kept in review.

## Refresh once, search many times

1. Run the existing sitemap collector for the desired retailer scope. It checks
   robots.txt, applies request ceilings and spacing, and saves raw XML with hashes.
2. Search that saved snapshot for any number of target batches. The new index
   reparses the hash-checked XML and builds an in-memory inverted index. It does
   not trust the collector's derived lead or entry CSVs.
3. Fetch only candidate product pages needed for the current run. Use existing
   seed/supplied-URL inputs for downstream discovery and matching.

Refresh traffic is shared across all searches. Local queries use no network. The
index is rebuilt from the saved XML when the command starts; it is not a separate
database or running service. A periodic refresh currently reads the selected
sitemaps again. An opt-in Laptop Factory public feed adds titles, SKUs and description
clues. Conditional HTTP refreshes and category-page crawling remain future work.

Sitemaps may omit products, be stale, or exceed a chosen request budget. The run
retains per-retailer coverage. An unavailable snapshot produces `SEARCH_ERROR`;
an unmatched query against a partial snapshot produces `INDEX_INCOMPLETE`.
`NO_RESULTS` means no indexed candidate, never that a retailer does not sell it.
Native search remains useful alongside the index.

## Input and output

Input CSV columns:

| Column | Meaning |
|---|---|
| `case_id` | Unique identifier for this search request. |
| `retailer_id` | `datablitz`, `laptop_factory`, `pcx`, `pcc`, or `oct`. |
| `model` | Model code or product name. |
| `brand`, `family`, `specs` | Optional clues for additional query tiers. |
| `target_id` | Optional existing target ID, exported as an unconfirmed search hint. |
| `expected_url` | Optional benchmark label; never used by the index or query planner. |

The first query uses the model/name, the second varies punctuation and adds the
brand, and the optional third uses family/specification clues. At most five results
are returned per query. Specification clues rank matching links, then URLs break ties.
Those clues never infer missing fields or approve a configuration. Broad names can produce multiple
candidates or exhaust that cap. The `site:` prefix in saved query text is a local
retailer filter; it does not send a search to Google or another service.

Outputs include the query plan, source coverage, result records, `leads.csv`,
`supplied_urls.csv`, `metrics.json` and `benchmark.csv`. Live page fetching also saves
raw HTML and hashes. Output directories must be new or empty. Search leads retain
the original sitemap/feed retrieval time; querying an old index does not refresh it.

Only approved HTTPS retailer product URLs are accepted. Shopify collection product
routes normalize to `/products/<handle>`; tracking is removed and an explicit numeric
variant remains. Product fetching uses the existing throttle, timeout, retry and size
limits. Cross-origin redirects and canonical URLs are refused.

Product reports retain actual title, JSON-LD facts and separately stated offers.
Missing RAM/storage stays missing. The tool does not choose variants, reconcile
contradictory specifications, run the matcher, publish prices, or write Sheets/history.
Cached sitemap text is never treated as a current price observation.

## Commands on SCL

From `/home/profile2/projects/laptop-price-tracker`, use a saved original live catalog
run. This searches locally and fetches no pages:

```sh
.venv/bin/python scripts/search_catalog_index.py \
  --cases tests/scraping_fixtures/discovery_benchmark/web_cases_v1.csv \
  --index-run data/output/catalog_index_20261025 \
  --output data/output/catalog_index_offline
```

Add `--live` to fetch candidate pages. The defaults are two queries per case, at most
20 cases and at most three candidate pages per case. Set the cohort and bounds
explicitly for larger batches:

```sh
.venv/bin/python scripts/search_catalog_index.py --live \
  --cases tests/scraping_fixtures/discovery_benchmark/web_cases_v1.csv \
  --index-run data/output/catalog_index_20261025 \
  --max-queries 1 --max-pages-per-case 2 \
  --output data/output/catalog_index_search
```

Pass the exported `supplied_urls.csv` to an otherwise normally scoped
`discover_products.py --supplied-urls ...` run. Product-name targets need their real
existing `target_id` in the input CSV for a useful search hint. Downstream discovery
re-fetches and checks leads; a hint is never an approved target association.

For direct integration into an existing weekly discovery invocation, add:

```sh
--catalog-index-run data/output/catalog_index_full_20261025 \
--catalog-feed-run data/output/catalog_feed_full_20261025
```

Keep that invocation's validated target master, selected list, contribution ledger,
scope, request ceilings and `--collect` choices. Local index results enter the same
supplied-candidate path and its per-target/retailer cap (default two). They are
re-fetched, variant-checked and matched under existing controls. The catalog flags
add leads; they do not publish Sheets. Rebuilding price reports and the explicit
`publish_prices.py --write-sheets` step remain as described in
[the weekly workflow](WEEKLY_WORKFLOW.md).

`--catalog-feed-run` requires `--catalog-index-run`. A target whose model cannot form a
query (blank, `nan`, over 200 characters or containing a double quote) is skipped for the
index only and appears in the supplied-URL report with the basis
`CATALOG_QUERY_NOT_FORMED`; other targets continue. A brand-only query is never issued.
Sitemap URLs the retailer adapter cannot normalize are counted in the index's
`rejected_urls` (and `index_rejected_urls` in `search_catalog_index.py`'s `run.json`)
instead of aborting the load.

To refresh all advertised sitemaps and the optional Laptop Factory feed, increase
the collector's explicit limits for your chosen scope, for example:

```sh
.venv/bin/python scripts/discover_catalog_leads.py --live \
  --target-master data/output/current/target_master_validated.csv \
  --scope-file data/output/current/pilot_scope.csv \
  --max-total-requests 90 --max-sitemaps-per-retailer 40 \
  --include-product-feed --max-feed-pages 20 \
  --output-dir data/output/catalog_fresh
```

Use your actual fresh input paths and a new output folder. Feed pages share the
collector's request budget, robots rules and throttle. The raw JSON is saved and
hash-checked; feed prices/stock never become observations. A separately saved feed
can be merged with `search_catalog_index.py --feed-run <folder>` or the discovery
flag above. End-of-feed coverage means the visible public feed ended, not that the
whole store's inventory is known.

This PR adds runnable commands to the registered SCL repository. It does not add
a scheduled job or automatically start a production weekly run. A connected-account
sample Sheet does not supply SCL's unattended service-account credentials.

## Reproduce the catalog benchmark

The public ten-case cohort was declared before live collection. To prepare the
collector's target-master format without passing expected URLs:

```sh
.venv/bin/python - <<'PY'
import csv
from pathlib import Path
with open('tests/scraping_fixtures/discovery_benchmark/web_cases_v1.csv') as f:
    cases = list(csv.DictReader(f))
Path('data/output').mkdir(parents=True, exist_ok=True)
with open('data/output/catalog_index_scope.csv', 'w', newline='') as f:
    writer = csv.DictWriter(f, fieldnames=['target_id', 'brand', 'model'])
    writer.writeheader()
    writer.writerows({'target_id': c['case_id'], 'brand': c['brand'], 'model': c['model']}
                     for c in cases)
PY
.venv/bin/python scripts/discover_catalog_leads.py --live \
  --target-master data/output/catalog_index_scope.csv \
  --targets W01,W02,W03,W04,W05,W06,W07,W08,W09,W10 \
  --max-total-requests 35 --max-sitemaps-per-retailer 5 \
  --output-dir data/output/catalog_index_fresh
```

Then run `search_catalog_index.py --live` against that fresh folder using the command
above. The measured refresh read only 5 of DataBlitz's 37 product sitemaps, and all
listed product sitemaps for the other four retailers. Increase explicit sitemap and
request budgets for fuller coverage; do not interpret that partial index as the
whole catalog. Retailer layouts and inventory may change between runs.

Give native search every planned query tier, up to seven per case, first page each,
and measure cached-query performance:

```sh
.venv/bin/python scripts/benchmark_catalog_index.py --live \
  --run data/output/catalog_index_search \
  --output data/output/catalog_index_comparison
```

Alternatively `--native-run <saved-native-run>` reuses existing native evidence
without requests, after checking the identical cohort and query words. The measured
comparison reused the same-day `data/output/web_discovery_20261025_expanded` native
scan. Only its native records are read; historical web-search records are ignored.
`performance.json` reports index load time and 10,000 local queries by default;
`existing_catalog_baseline.csv` evaluates the unchanged model-code-only helper
against the same snapshot. These are discovery benchmarks; the production ranker,
continuation/pagination, seeds and matcher do not run.

Expected URLs are separately fetched for benchmark verification, even if search
misses them. Those requests never create discovery leads. Page counts include these
verification fetches; a found-and-fetched link is not an accepted configuration.

Replay saved search/page evidence without requests:

```sh
.venv/bin/python scripts/search_catalog_index.py \
  --replay data/output/catalog_index_comparison \
  --output data/output/catalog_index_replay
```

Replay checks saved product-page hashes and interpretations while preserving original
timestamps. The index loader independently checks sitemap hashes when rebuilding a
snapshot. Keep ignored raw evidence locally; publish only reviewed benchmark summaries.

## Verification environment

Run `python -m pytest -q` and `python -m ruff check .`. SCL uses Python 3.11.15 and
pandas 2.3.3. Unpinned requirements initially selected pandas 3.1.0 and exposed the
unchanged baseline failure `test_all_invalid_input_still_writes_validation_diagnostics`
(`math.isfinite(pd.NA)`). That failure reproduces with the original data-quality
source and test. This PR does not change that code or the dependency policy.
