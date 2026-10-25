# Web discovery benchmark — 25 October 2026

On ten predeclared public product URLs spanning all five retailers, expanded native
search recovered six URLs. One web-search invocation per case recovered eight.
Their union recovered nine: three additional URLs beyond the expanded native scan.
All ten expected URLs were independently fetched and confirmed as product pages.
This measures link recovery, not configuration matching or comparable prices.

| Measurement | Expanded native search | Web search | Union |
|---|---:|---:|---:|
| Expected product URLs recovered | 6/10 | 8/10 | 9/10 |
| Recovery on this cohort | 60% | 80% | 90% |
| Newly recovered beyond native | — | 3 | 3 |
| Search errors | 0 | 0 | — |
| Recorded fetch attempts / completed search calls | 51 | 10 | — |
| Recorded search-stage time | 442.892 s | 121.237 s | — |

The union improves recovery by 30 percentage points on this cohort, or 50% relative
to six native recoveries. Native HTTP attempts and completed model search calls are
different units. Timings include retailer throttling for native search and CLI/model
round trips for web search. They are observations from these runs, not a general
speed or cost guarantee. The union is calculated from the saved result sets; no
production Top 100 coverage change is claimed.

## Per-case results

| Case | Retailer and searched model | Native | Web |
|---|---|---|---|
| W01 | PC Express FA607NUQ-RL011W | Found | Found |
| W02 | Laptop Factory 14-ep0445TU | Missed | Missed |
| W03 | Laptop Factory B12UC-1432PH | Missed | Found |
| W04 | PC Express MacBook Neo MHFC4PP/A | Missed | Found |
| W05 | PC Central MacBook Neo | Found | Found |
| W06 | Octagon SF14-51-553D | Found | Missed |
| W07 | Octagon FA607NUQ-RL204WSM | Found | Found |
| W08 | PC Express 15-fc0756au | Found | Found |
| W09 | DataBlitz S3407VA-LY135WSM | Found | Found |
| W10 | Laptop Factory X1404VA-EB2264WSM | Missed | Found |

[Per-case CSV](web-discovery-2026-10-25.csv) includes expected URLs, counts, timings,
errors and independent product-page fetch outcomes. The
[single-query comparison](web-discovery-single-query-2026-10-25.csv) also recovered
6/10 native, 8/10 web and 9/10 combined, before expanding the native search budget.

The HP 14-ep0445TU page remains reachable but neither search recovered its URL.
That is an unresolved discovery case, not evidence that Laptop Factory lacks the
product. Octagon SF14-51-553D was found only by native search, so web search should
supplement native search rather than replace it.

## Method and limits

- Cohort: `tests/scraping_fixtures/discovery_benchmark/web_cases_v1.csv`, declared
  before the first live search. Expected URLs were never passed to the search model.
  The cases were known during development; this is a convenience/regression cohort,
  not a random sample, held-out evaluation or full production catalog.
- Web search: one quoted identifier/name query per case, at most five returned
  results. Each invocation emitted exactly one completed web-search event. Search
  is provided by signed-in Codex CLI, not claimed to be Google specifically.
- Expanded native baseline: the existing `search_queries` planner, every planned
  tier up to seven, first result page per query, no early stopping. This produced
  51 queries. No candidate ranker, collection continuation, supplied/historical URLs,
  sitemaps or product matcher ran. Compound names use the same CSV input in both
  methods; this is not the full source-catalog production workflow.
- Page verification: the existing bounded fetcher fetched discovered pages and,
  separately, each expected URL. These verification fetches never entered the
  discovered lead sets. Twelve unique product pages were saved in total, including
  two other candidates. Multiple offers remain raw separately stated offers.
- No prices, mappings, approvals or historical observations were published. A found
  page can have incompatible or contradictory specifications. Neither search result
  rank nor this benchmark accepts it as a configuration match.
- Search indices and retailer pages change over time. This is a timestamped snapshot.

Live web/paired run: 12:57:39–13:00:29 Manila time.
Expanded native run: 13:03:21–13:10:44 Manila time.
Environment: SCL, Python 3.11.15, pandas 2.3.3, Codex CLI 0.158.0.
The CLI model was not explicitly pinned by the discovery command.

Canonical parsed cohort SHA-256 (JSON sorted by key):
`0f2fcf2eba095a0370bc1ba1ccf48aea879574f6454a16126f38e70ac7d9f784`.
The original input bytes, query records, raw page responses and timestamps remain
in the ignored SCL evidence directories:

```text
data/output/web_discovery_20261025_v1/
data/output/web_discovery_20261025_expanded/
```

## Reproduction and verification

The exact commands are documented in [Independent web discovery](../WEB_DISCOVERY.md).
Both live runs replayed without network; the expanded replay's metrics were exactly
equal to the saved live metrics. Hash verification covers saved retailer responses,
and original retrieval timestamps remain unchanged.

The full offline suite and lint passed on SCL. The repository's unpinned pandas 3.1.0
installation exposes an unchanged data-quality baseline failure; the passing suite
uses pandas 2.3.3 as documented in the setup notes. No pandas compatibility fix or
dependency-policy change is included in this discovery PR.
