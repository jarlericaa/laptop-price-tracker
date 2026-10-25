# Independent web discovery

`discover_web_leads.py` finds retailer product links using signed-in Codex CLI web
search, then fetches retailer pages through the existing bounded HTTP fetcher.
It does not run a product matcher, change the catalog, publish to Sheets or add
observations to production price history. Existing discovery commands are unchanged.

Use this as an opt-in source of additional links when native store search misses
a listing. Keep native search: the measured search methods recover different pages.
See the [25 October live benchmark](benchmarks/web-discovery-2026-10-25.md) for results
and per-case evidence.

## Requirements and input

Install the project's existing Python requirements. Install Codex CLI separately and
sign in with the intended ChatGPT account; check `codex login status` on the SCL host.
No additional Python dependency or search API key is required. A CLI invocation uses
the subscription's limits; it is not guaranteed free or unlimited.

The search subprocess forces ChatGPT authentication, removes API-key environment
overrides, ignores user configuration, disables command-execution tools, and runs
in a temporary read-only workspace. It never rotates accounts or falls back to API
billing. See the official [non-interactive CLI documentation](https://developers.openai.com/codex/noninteractive/)
and [web-search configuration](https://developers.openai.com/codex/config-reference/).

Input CSV columns:

| Column | Meaning |
|---|---|
| `case_id` | Unique identifier for this discovery request. |
| `retailer_id` | Existing retailer ID: `datablitz`, `laptop_factory`, `pcx`, `pcc`, `oct`. |
| `model` | Identifier or product name to search. |
| `brand`, `family`, `specs` | Optional query clues used for broader searches. |
| `target_id` | Optional existing target ID, exported only as a search hint. |
| `expected_url` | Optional benchmark label. Never sent to the search subprocess. |

The public regression cohort is
`tests/scraping_fixtures/discovery_benchmark/web_cases_v1.csv`. It declares ten
product URLs across all five retailers, including known missing/sold-out pages,
Apple product names and listings with conflicting specifications. It contains no
private workbook rows, Shopee information or target mappings. These cases were known
during development; this is a regression/convenience cohort, not a random sample of
retailer inventory or a measurement of the full Top 100.

## Commands on SCL

From `/home/profile2/projects/laptop-price-tracker`, preview without any network:

```sh
.venv/bin/python discover_web_leads.py \
  --cases tests/scraping_fixtures/discovery_benchmark/web_cases_v1.csv \
  --output data/output/web_preview
```

Run an explicit bounded search and fetch:

```sh
.venv/bin/python discover_web_leads.py --live \
  --cases tests/scraping_fixtures/discovery_benchmark/web_cases_v1.csv \
  --output data/output/web_run \
  --max-queries 2 --max-cases 20 --max-pages-per-case 3
```

The default is at most two CLI invocations per case, with up to five returned search
results per invocation. Queries progress from a quoted identifier to a punctuation
variation and, when enabled, broader family/specification clues. Each invocation
has a timeout. The one-search instruction to the model is advisory; completed search
calls are counted from CLI events and reported separately from CLI invocations.
Search result order and availability can change between runs.
Counts from a timed-out CLI invocation may be incomplete.

Only exact approved retailer origins and supported product paths are accepted.
Shopify `/collections/<collection>/products/<handle>` URLs normalize to the existing
`/products/<handle>` route. Tracking parameters are removed and a numeric `variant`
selection is preserved. Cross-origin redirects are refused by the shared fetcher;
off-domain canonical links are refused by the new parser. Existing throttling,
timeouts, retries and response-size limits apply to retailer requests.

The page report retains actual JSON-LD product/offer objects, title, canonical URL,
retrieval time and response hash. Multiple offers stay separate. It does not infer
an installed configuration from a target, choose a default variant or treat a
search snippet as a current price. JavaScript-only or non-structured pages can stay
unconfirmed; missing information remains missing. A candidate can still be another
configuration or product type and needs the existing downstream checks.

## Results and integration

Each new, empty output directory receives:

- `plan.json`: the declared query plan, saved before live search.
- `run.json`: search results, outcomes, timings and retailer facts.
- `page-*.html`: saved retailer responses with recorded hashes and observation times.
- `leads.csv`: normalized links with the query and original search timestamp.
- `supplied_urls.csv`: web-discovered links in the existing discovery input format.
- `metrics.json` and `benchmark.csv`: discovery-only measurements.

Feed reviewed leads into an otherwise normally scoped discovery run by adding
`--supplied-urls <web_run>/supplied_urls.csv` to `discover_products.py`. Broad-name
targets need their real existing `target_id` in the input CSV to supply a useful
search hint. A hint is never a confirmed target association. This tool itself does
not run the matcher or approve prices.

Reuse the existing verified-URL/seed mechanism on later weekly runs rather than
repeating web search for every known page. Web discovery should be a targeted
fallback for unresolved cells.

## Reproducible benchmark

The initial paired run uses one query per provider on the same ten cases:

```sh
.venv/bin/python discover_web_leads.py --live --benchmark-native \
  --cases tests/scraping_fixtures/discovery_benchmark/web_cases_v1.csv \
  --output data/output/web_discovery_20261025_v1 \
  --max-queries 1 --max-pages-per-case 2
```

The stronger native baseline tries every planned native query tier, up to seven
per case, without early stopping. It reuses the recorded web-search evidence:

```sh
.venv/bin/python benchmark_web_discovery.py --live \
  --run data/output/web_discovery_20261025_v1 \
  --output data/output/web_discovery_20261025_expanded
```

This baseline uses the repository's existing query planner and search-result parser.
It reads the first native result page for each query. It does not run the production
candidate ranker, continuation/collection rounds, historical/supplied seeds, sitemap
discovery or matching. It is not a before/after production coverage claim.

`expected_url` reachability is fetched separately for benchmark verification, even
when neither search finds it. Those verification fetches do not create discovery
leads. `fetched_product_pages` includes all saved page fetches, including these
verification requests; provider-specific expected/fetched counts mean a discovered
expected URL was independently verified as a product page, not that it passed a
production collection budget or configuration matching. Native search-call counts
are fetch attempts; web counts are completed CLI search events. Redirect requests
are not a separate native counter. Timings include the shared retailer throttle.

Offline replay sends no requests and preserves original observation timestamps:

```sh
.venv/bin/python discover_web_leads.py \
  --replay data/output/web_discovery_20261025_v1 \
  --output data/output/web_replay
```

Replay verifies saved page hashes and regenerates normalized leads and metrics. It
does not represent a fresh price observation. Output directories are never silently
reused. Raw evidence remains in ignored `data/output`; publish only the small,
reviewed benchmark report and per-case CSV.

## Verification environment

Run `python -m pytest -q` and `python -m ruff check .`. The SCL validation environment
uses Python 3.11.15 and pandas 2.3.3. Installing the repository's unpinned requirements
initially selected pandas 3.1.0 and exposed an unchanged baseline failure in
`test_all_invalid_input_still_writes_validation_diagnostics` (`math.isfinite(pd.NA)`).
That failure reproduces with the original data-quality source and test, without the
new discovery tests. No application data-quality code or dependency policy is changed
by this PR; pandas 3 compatibility remains a separate issue.
