# Cached catalog benchmark — 25 October 2026

On ten predeclared public cases, a local sitemap index recovered **9/10 expected
product links**. Combined with the previously measured native search results, it
covered **10/10**. There are no LLM calls, search API calls or API keys in this path.

| Method | Expected links recovered |
|---|---:|
| Native search, all planned query tiers | 6/10 |
| Existing model-code-only sitemap helper | 7/10 |
| Native + existing sitemap helper | 9/10 |
| New cached index | 9/10 |
| Native + new cached index | 10/10 |

The index adds four links beyond native search: W02, W03, W04 and W10. Relative to
native plus the existing catalog helper, the incremental recovery is **one case,
W04**. The old helper already recovers model-code cases from this snapshot; it
excludes the two product-name cases. Native search recovers W05, while the new
index additionally reaches W04's compound product name/part number.

This is link recovery on a known regression/convenience cohort, not a random or
held-out sample, full Top 100 coverage, configuration accuracy or comparable prices.

## Refresh and query measurements

The existing collector made **21 HTTP requests** and saved **9,379 product URLs**.
That shared refresh took 163.696 seconds including the existing 10-second per-retailer
spacing. Each request served the catalog, rather than one query for one model.

| Retailer | Product URLs indexed | Product sitemaps read/listed |
|---|---:|---:|
| Laptop Factory | 905 | 1/1 |
| DataBlitz | 4,902 | 5/37 |
| PC Express | 2,830 | 3/3 |
| PC Central | 27 | 1/1 |
| Octagon | 715 | 1/1 |

The request ceiling was 35, with at most five product sitemaps per retailer. All
roots were advertised in robots.txt and allowed by the parsed rules. DataBlitz is
deliberately partial under that limit. Its W09 query returns `INDEX_INCOMPLETE`,
not an unavailable-product conclusion. Native search found W09's expected page.

The committed benchmark helper measured:

| Measurement on SCL | Result |
|---|---:|
| Load and index the saved snapshot | 0.587582 s |
| Run 10,000 local queries cycling the ten declared queries | 0.040408 s |
| Network requests for those 10,000 queries | 0 |

Load time excludes Python startup/imports. The query test measures this small,
repeated workload in memory; it is not a general capacity or end-to-end latency
guarantee. A separate check disabled the HTTP transport and successfully rebuilt
the index and ran the same 10,000 queries with zero requests. Refreshing catalogs
and fetching current product pages still create bounded retailer traffic. Refreshes
currently reread the selected sitemaps; incremental HTTP caching is not implemented.

## Per-case recovery

| Case | Retailer and model | Native | Old catalog helper | New index |
|---|---|---|---|---|
| W01 | PC Express FA607NUQ-RL011W | Found | Found | Found |
| W02 | Laptop Factory 14-ep0445TU | Missed | Found | Found |
| W03 | Laptop Factory B12UC-1432PH | Missed | Found | Found |
| W04 | PC Express MacBook Neo MHFC4PP/A | Missed | Excluded | Found |
| W05 | PC Central MacBook Neo | Found | Excluded | Found |
| W06 | Octagon SF14-51-553D | Found | Found | Found |
| W07 | Octagon FA607NUQ-RL204WSM | Found | Found | Found |
| W08 | PC Express 15-fc0756au | Found | Found | Found |
| W09 | DataBlitz S3407VA-LY135WSM | Found | Outside saved subset | Index incomplete |
| W10 | Laptop Factory X1404VA-EB2264WSM | Missed | Found | Found |

[Per-case metrics](catalog-index-2026-10-25.csv) include expected URLs, calls,
timings and page verification. The [existing-helper comparison](catalog-index-existing-baseline-2026-10-25.csv)
reports its results against exactly the same saved snapshot.

## Method and evidence

- The cohort is `tests/scraping_fixtures/discovery_benchmark/web_cases_v1.csv`, fixed
  before the original searches and reused unchanged here. Expected URLs never
  enter the index or query planner. The index reparses raw sitemap bytes; it does
  not read the scope-dependent lead CSV or the benchmark's expected URLs.
- The index uses one model/name query per case and returns at most five links.
  Terms are case-insensitive; punctuation splits words, with adjacent joined words
  also indexed. Result order is deterministic. Native and existing-helper results
  remain separate, so the new index does not silently inherit their discoveries.
- The native baseline is the same-day, saved expanded scan: all 51 planned query
  tiers, first page per query, no early stopping, six expected links recovered.
  The comparison checks identical cases and query words before reusing its native
  records. No historical Codex/web-search results are included in these scores or
  invoked by this implementation. The collection times differ, as recorded below.
- The existing-helper baseline uses unchanged `lead_targets`/`catalog_leads` on
  all indexed sitemap entries, with the declared cohort as target scope. It accepts
  eight `MODEL_CODE` targets; seven expected URLs occur in the saved subset.
- The live index run fetched nine discovered expected links. The tenth, W09, was
  fetched only for independent ground-truth verification; that verification never
  added a discovery lead. All ten responses were confirmed as product pages.
- A product page remains a candidate. Contradictory specifications, missing fields
  and separate variants/offers are not resolved by these scores. No matcher,
  price publication, live Sheets write or production history update ran.

Times in Manila on 25 October 2026:

- Saved native scan: 13:03:21–13:10:44.
- New catalog refresh: 13:26:31–13:29:14.
- New index search and product verification: 13:30:39–13:31:04.

Environment: SCL, Python 3.11.15, pandas 2.3.3. Canonical parsed cohort SHA-256:
`0f2fcf2eba095a0370bc1ba1ccf48aea879574f6454a16126f38e70ac7d9f784`.

Raw evidence remains in ignored SCL directories:

```text
data/output/catalog_index_20261025/
data/output/catalog_index_search_20261025/
data/output/catalog_index_comparison_20261025_v2/
data/output/catalog_index_comparison_replay_20261025_v2/
```

The comparison replay's metrics exactly equal the saved comparison metrics. Page
hashes and original observation times are preserved; the index loader independently
checks saved sitemap hashes. Commands are in [cached catalog search](../CATALOG_INDEX.md).

Validation: **1,832 tests passed**, lint and diff checks passed. Pandas 3.1.0 exposes
an unchanged baseline data-quality failure; the passing suite uses pandas 2.3.3, as
documented in the setup notes. No dependency policy change is included.
