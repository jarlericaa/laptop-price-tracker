"""Plan-only alternate discovery routes (2026-11-03).

A retailer's own search can miss a product that its pages and sitemap list: a capped predictive
search, an out-of-stock product that sorts last, a hyphenated code tokenized badly. Two further
public routes exist, and neither is used silently:

- **Product-only storefront search** (``/search?q=<query>&type=product`` on the Shopify stores)
  is implemented in ``retailer_search`` as an explicit second route, off by default and bounded by
  ``--max-storefront-search-requests``.
- **Retailer-restricted web search** (``site:<host> "<code>"`` in a public search engine) needs a
  third party's service and terms, so this tool never sends it. ``web_search_plan_rows`` only writes
  the queries a person can run by hand; every product URL found that way goes into
  ``config/supplied_candidate_urls.csv`` (or ``--supplied-urls``) and is then revalidated like any
  known URL: a lead, never an approved match.

Nothing here sends a request.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from .retailer_search import SEARCH_METHODS, code_spellings, without_repeated_words

WEB_SEARCH_PLAN_COLUMNS: tuple[str, ...] = (
    "target_id",
    "retailer_id",
    "route",
    "order",
    "query",
    "restricted_host",
    "status",
    "how_to_use",
)
WEB_SEARCH_ROUTE = "RETAILER_RESTRICTED_WEB_SEARCH"
PLAN_ONLY = "PLAN_ONLY_NOT_SENT"
_HOW_TO_USE = (
    "Run by hand in a public search engine. Record any product page URL it shows in "
    "supplied_candidate_urls.csv with this target in search_target_id; discovery then fetches "
    "and matches the page like any other known URL. A search-engine snippet is never evidence."
)


def web_search_plan_rows(
    targets: Iterable[Mapping[str, str]], retailers: Sequence[str], *, per_target: int = 3
) -> list[dict[str, str]]:
    """Restricted web-search queries per target and retailer, in order, for a person to run.

    ``targets`` carry ``target_id``, ``brand``, ``model``, ``kind`` (``MODEL_CODE`` or a name
    kind), ``recorded_code`` and ``cpu_series``. A code (or recorded manufacturer code) is quoted
    as recorded, then with each safe spelling; a product or family name is quoted with its
    processor. At most ``per_target`` queries per retailer; none is sent.
    """
    rows: list[dict[str, str]] = []
    for target in targets:
        code = (
            target.get("model", "")
            if target.get("kind") == "MODEL_CODE"
            else (target.get("recorded_code", ""))
        )
        if code:
            texts = [code] + [spelling for _, spelling in code_spellings(code)]
        else:
            name = " ".join(
                part for part in (target.get("brand", ""), target.get("model", "")) if part
            ).strip()
            cpu = target.get("cpu_series", "")
            texts = [name] + ([without_repeated_words(f"{name} {cpu}")] if cpu else [])
        texts = list(dict.fromkeys(text for text in texts if text))[:per_target]
        for retailer in retailers:
            method = SEARCH_METHODS.get(retailer)
            if method is None:
                continue
            for order, text in enumerate(texts, start=1):
                rows.append(
                    {
                        "target_id": target["target_id"],
                        "retailer_id": retailer,
                        "route": WEB_SEARCH_ROUTE,
                        "order": str(order),
                        "query": f'site:{method.host} "{text}"',
                        "restricted_host": method.host,
                        "status": PLAN_ONLY,
                        "how_to_use": _HOW_TO_USE,
                    }
                )
    return rows
