# Laptop Factory (LF) Adapter

**Status:** Offline adapter implemented and tested on 2026-09-29 (`adapter_version = "lf-1"`). Live use is enabled under the project-owner policy in [DECISIONS.md](../DECISIONS.md). The retailer's published permission remains unverified (external-policy finding UNKNOWN; project status OWNER_APPROVED). A controlled bare-URL live check on 2026-10-01 returned one in-stock Acer Aspire AI 14 offer at PHP 45,999.00 (SALE, regular PHP 50,999.00); the requested, final, and offer URLs were the same plain string. See [PROGRESS_LOG.md](../PROGRESS_LOG.md#phase-7-live-follow-up-checks-2026-10-01). Adapter maintainer: Jarelle Ricaforte.

| Item | Value |
|---|---|
| Module | `src/laptop_tracker/adapters/laptop_factory.py` |
| Class | `LaptopFactoryAdapter` |
| `retailer_id` / retailer code | `laptop_factory` / `LF` (matches `LF_Price` in [PRICE_TRACKING_OUTPUT_SCHEMA.md](../PRICE_TRACKING_OUTPUT_SCHEMA.md)) |
| Allowed hosts | `laptopfactory.com.ph` only (exact; `www.` and other hosts are rejected) |
| Input | One fetched product page (`/product/<slug>/`). Build requests with `product_request(url)`. |
| Output | One `ParsedOffer` with `OfferDetails`, or a `ParseError` |
| Tests | `tests/test_laptop_factory.py`, fixtures in `tests/scraping_fixtures/laptop_factory/` |

## Setup

No configuration or credentials are needed. The adapter never performs HTTP; it only parses a `FetchedPage` from the shared `Fetcher`. Run the offline tests with:

```bash
python -m pytest -q tests/test_laptop_factory.py
```

The default `Fetcher()` uses a live transport, so calling `scrape(LaptopFactoryAdapter(), [...], Fetcher())` can send a real request. No standalone scraper CLI exists. Search (`/?s=`) and category pages are out of scope.

`product_request` accepts only `https://laptopfactory.com.ph/product/<slug>/`, with or without the trailing slash. Since 2026-10-01 it refuses `http`, other hosts, nested or empty product paths, fragments, and every query. Queries are refused because WooCommerce GET queries such as `?add-to-cart=` change cart state, and robots.txt disallows them. Pass a bare URL; a string wrapped in Markdown link syntax is refused.

## Page assumptions

These come from two product pages reviewed on 2026-09-29. The site is WooCommerce on the Flatsome theme.

| Data | Primary source | Fallback |
|---|---|---|
| Title | JSON-LD `Product.name` | `h1.product_title` in the product summary |
| Product URL | JSON-LD `Offer.url`, then `Product.url` | `<link rel="canonical">`, then the page URL. Relative links resolve against the page. |
| Current price | JSON-LD `Offer.price`, cross-checked with the displayed price | `p.price` in the summary: `<ins>` amount when on sale, otherwise its single amount |
| Regular price | JSON-LD `priceSpecification` with `priceType` `ListPrice` | `<del>` amount in `p.price` |
| Currency | JSON-LD `priceCurrency` must be `PHP` | The displayed symbol must be `₱` |
| Availability | Stock wording (`p.stock`), JSON-LD `availability`, and the Add to Cart button, combined | None |
| SKU | JSON-LD `sku`, kept as text. It may be descriptive text or a number equal to the post ID. | `span.sku` in `.product_meta` |
| Product ID | `div#product-<id>` | Add to Cart button `value` |
| Brand, MPN | JSON-LD `brand`, `mpn` only. The reviewed pages have neither, so they stay blank. | None. The brand is never guessed from categories or the title. |
| Raw specifications | "Tech Specs" table in `.product-short-description` | Table in the description tab, then the JSON-LD `description` text |
| Categories | `a[rel=tag]` in `.product_meta .posted_in`, kept raw | None |

Only markup inside the product summary is read. Related products and smart-bundle (`woosb`) component rows have their own prices and stock and are ignored. Screen-reader text ("Original price was …") is skipped.

**Price rules**
- **SALE or REGULAR:** a regular price above the current price gives `SALE`. No regular price, or an equal one, gives `REGULAR`. `regular_price`, `sale_price`, and `price_type` record which is which.
- **Parse failures:** a JSON-LD and displayed price disagreement is `PRICE_CONFLICT`. A price range, several amounts, a `<del>` without an `<ins>`, or more than one price block is `AMBIGUOUS_PRICE`. No price is `PRICE_NOT_FOUND`. A non-peso currency is `UNSUPPORTED_CURRENCY`. A regular price below the current one is `PRICE_CONFLICT`.

**Availability rules.** Stock wording and JSON-LD are the explicit signals. They must agree, and the Add to Cart state must be consistent with them: enabled for in stock or preorder, absent or disabled for out of stock. Otherwise the result is `UNKNOWN` with `signals conflict` in the evidence. With no explicit signal the result is `UNKNOWN`; price visibility and the cart alone never decide stock. "Available on backorder" and unrecognized wording are `UNKNOWN`. The evidence string records all three signals, for example `stock text: In stock; JSON-LD availability: https://schema.org/InStock; add to cart: enabled`.

**Bundles.** Offers are flagged, never dropped, when:
- the title contains `FREE` (including `+ FREE`), `BUNDLE`, `MONITOR`, `TABLET`, or `CHAIR`, case-insensitively. Word-internal matches such as `ERGOCHAIR` count.
- the page has smart-bundle component markup (`woosb`).

`bundle_evidence` records each match, for example `title term CHAIR: ERGOCHAIR` and `bundle components: …`. Categories such as "GRAND MEGA BUNDLE SALE" are kept in `raw_categories` but are not used for flagging, because sale categories may include non-bundles.

**Not inferred.** Refresh rate, CPU, RAM, and other specifications are kept only as the page's raw text. A missing row, such as refresh rate on the reviewed pages, stays missing. No `target_id` is assigned.

## Versioning and maintenance

- Bump `adapter_version` (`lf-1` → `lf-2` and so on) whenever parsing rules or assumptions change. Every observation records the version in `source.adapter_version`.
- When the site changes, expect `UNRECOGNIZED_LAYOUT`, `TITLE_NOT_FOUND`, or a price failure rather than wrong data. Update the fixtures from a newly reviewed page, keeping markup minimal with no full page copies or personal data. Then adjust the parser and bump the version.
- Keep `DATA_USE_REVIEW_STATUS` and `live_use_approved` in step with [RETAILER_DATA_USE_REVIEW.md](../RETAILER_DATA_USE_REVIEW.md) and [DECISIONS.md](../DECISIONS.md). The current owner policy authorizes internal collection; it does not verify retailer permission.

## Known limitations

- **Live behavior has two one-product checks.** The 2026-09-29 user-reported check returned an in-stock Acer Nitro V 15 offer; its displayed input URL had Markdown markup, which `ScrapeRequest` would refuse, so that markup was most likely added when the output was displayed. The 2026-10-01 controlled bare-URL check confirmed clean URL attribution for one sale-priced, in-stock simple product. Out-of-stock, backorder, and variable products have not been observed live. The smart-bundle parse was checked only against a saved research page, not through the project scraper.
- Only simple and smart-bundle product pages were reviewed. Variable products with a price range or several JSON-LD offers yield `AMBIGUOUS_PRICE` unless exactly one current price is displayed.
- Only the stock wordings `In stock`, `<n> in stock`, and `Out of stock` are mapped. Other wording is kept as evidence and mapped to `UNKNOWN`.
- Bundle detection is keyword and markup based and can over-flag, for example a title mentioning a monitor size. That is intentional, since review happens downstream.
- Brand and MPN are blank on the reviewed pages. Phase 8 must use the raw title, specs, and categories as evidence without treating them as verified identity.
- Search and category pages are not parsed, and no crawling or discovery exists.
