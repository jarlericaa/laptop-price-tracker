# DataBlitz (DBZ) Adapter

**Status:** Adapter implemented and tested offline on 2026-10-01 (`adapter_version = "dbz-1"`). A user-reported live spot check on 2026-10-01 returned one MSI Crosshair A16 HX offer at PHP 162,995.00 with `IN_STOCK`. Live use is enabled under the project-owner policy in [DECISIONS.md](../DECISIONS.md). The retailer's published permission remains unverified (external-policy finding UNKNOWN; project status OWNER_APPROVED). Adapter maintainer: Jarelle Ricaforte.

| Item | Value |
|---|---|
| Module | `src/laptop_tracker/adapters/datablitz.py` |
| Class | `DataBlitzAdapter` |
| `retailer_id` / retailer code | `datablitz` / `DBZ` (matches `DBZ_Price` in [PRICE_TRACKING_OUTPUT_SCHEMA.md](../PRICE_TRACKING_OUTPUT_SCHEMA.md)) |
| Allowed hosts | `ecommerce.datablitz.com.ph` only (exact). `datablitz.com.ph`, `www.datablitz.com.ph`, and other hosts are rejected. |
| Input | One fetched product page (`/products/<handle>`). Build requests with `product_request(url)`. |
| Output | One `ParsedOffer` with `OfferDetails`, or a `ParseError` |
| Tests | `tests/test_datablitz.py`, fixtures in `tests/scraping_fixtures/datablitz/` |

## Host confirmation

On 2026-10-01, `https://datablitz.com.ph/` and `https://www.datablitz.com.ph/` both returned HTTP 301 to `https://ecommerce.datablitz.com.ph/`. That host serves the DataBlitz Shopify storefront, with "© 2026 DataBlitz", product pages under `/products/`, and the laptop collections found by web search. The redirecting domains are not on the allowlist, so requests must name the storefront host directly. A future redirect away from it fails with `REDIRECT_HOST_NOT_ALLOWED`.

## Setup

No configuration or credentials are needed. The adapter never performs HTTP; it only parses a `FetchedPage` from the shared `Fetcher`. Run the offline tests with:

```bash
python -m pytest -q tests/test_datablitz.py
```

The default `Fetcher()` uses a live transport, so calling `scrape(DataBlitzAdapter(), [...], Fetcher())` can send a real request. No standalone scraper CLI exists. One user-reported live spot check has been run (see Status). Collection (`/collections/...`), search, and collection-scoped product URLs (`/collections/<c>/products/<handle>`) are out of scope; `product_request` refuses them.

## Page assumptions

These come from two laptop product pages reviewed on 2026-10-01, one at a regular price and one on sale. The site is Shopify with the Warehouse theme (version 7). A real product page is about 2.4 MB, within the framework's 5 MB response cap.

| Data | Primary source | Fallback |
|---|---|---|
| Title | JSON-LD `Product.name` | `h1.product-meta__title` in the main product form |
| Product URL | JSON-LD `Offer.url` (includes `?variant=`), then `Product.url` | `<link rel="canonical">`, then the page URL. Relative links resolve against the page. |
| Current price | JSON-LD `Offer.price`, cross-checked with the displayed price | `.price-list` in the main form: `span.price--highlight` when on sale, otherwise its single `span.price` |
| Regular price | `span.price--compare` in `.price-list`. Shopify's JSON-LD has no compare-at price. | None |
| Currency | JSON-LD `priceCurrency` must be `PHP` | Each displayed amount must show `₱` or `PHP` |
| Availability | JSON-LD `availability` and the main add-to-cart button (label and disabled state), combined | None |
| SKU | JSON-LD `sku`, kept as text. On reviewed pages it is the barcode (GTIN). | `.product-meta__sku-number` |
| Product ID | Shopify product ID from the main form's hidden `product-id` input | None |
| Brand | JSON-LD `brand.name` | The product-meta vendor link (`a.product-meta__vendor`). It is retailer markup, not a guess from the title. |
| MPN | JSON-LD `mpn` only. The reviewed pages have none, so it stays blank. | None |
| Raw specifications | Rows of the table(s) in the Description card (`.product-block-list__item--description`), `th`/`td` or `td`/`td` | `Label: value` lines of `.product-meta__short-description`, then the JSON-LD `description` text |
| Categories | JSON-LD `category` (for example `Laptops`), kept raw | None |

"Main" means inside `form#product-form-main-…`. The page repeats the whole product block inside a `<template>` for the quick-buy modal. Everything inside `<template>` is ignored, including its JSON-LD, as are recommendation cards, the hidden "Save ₱…" label, visually hidden text ("Sale price", "Regular price"), and the "See full description" in-page link.

**Price rules**
- **SALE or REGULAR:** a highlighted price with a higher compare price gives `SALE`. A single displayed price, or a compare price equal to the current one, gives `REGULAR`. When only JSON-LD gives a price (no displayed price), `price_type`, `regular_price`, and `sale_price` stay empty, because JSON-LD cannot show whether the price is discounted.
- **Parse failures:**
  - `PRICE_CONFLICT`: the JSON-LD and displayed prices disagree, or the compare price is below the current price.
  - `AMBIGUOUS_PRICE`: a range or several numbers in one amount, wording besides the amount (for example `From ₱…`), several amounts or price blocks, a highlight without a compare price, several JSON-LD offers (variants), or an `AggregateOffer`.
  - `PRICE_NOT_FOUND`: no amount with digits, for example "Call for price".
  - `UNSUPPORTED_CURRENCY`: a non-peso symbol or currency code, or a non-PHP `priceCurrency`.
  - `CURRENCY_NOT_FOUND`: a displayed amount without `₱` or `PHP`, or a JSON-LD-only price without `priceCurrency: PHP`.
  - `INVALID_PRICE`: zero or malformed numbers.

**Availability rules.** No stock wording is shown on the reviewed pages, so the explicit signals are:
- JSON-LD `availability`, mapped as InStock to `IN_STOCK`, OutOfStock or SoldOut to `OUT_OF_STOCK`, and PreOrder to `PREORDER`. Other values, such as BackOrder, give `UNKNOWN`.
- The button labels `Sold out` (`OUT_OF_STOCK`), `Pre-order` (`PREORDER`), and `Unavailable` (`UNKNOWN`).

`Add to cart` is not stock wording; it only confirms another signal. The explicit signals must agree, and the button must be consistent with them:
- `IN_STOCK` needs an enabled `Add to cart`.
- `PREORDER` needs an enabled `Pre-order` or `Add to cart`.
- `OUT_OF_STOCK` needs the button absent, disabled, or labelled `Sold out`.

Otherwise, or when the main form has several different buttons, the result is `UNKNOWN` with `signals conflict` in the evidence. With no explicit signal, for example a page without JSON-LD showing only `Add to cart`, the result is `UNKNOWN`. Unrecognized labels are `UNKNOWN`. Evidence example: `JSON-LD availability: http://schema.org/InStock; add to cart button: Add to cart (enabled)`.

**Bundles.** Offers are flagged, never dropped, when the title contains `FREE` (including `+ FREE`), or one of `BUNDLE`, `MONITOR`, `TABLET`, `CHAIR`, `BACKPACK`, `BRIEFCASE`, `BAG`, `MOUSE`, or `HEADSET`. A plural `S` is allowed, matching is case-insensitive, and only whole words count. `bundle_evidence` records each match, for example `title term BRIEFCASE: Briefcase`. Many DataBlitz laptop titles name an included bag after `+`, so many listings are expected to be flagged. That is intentional, since review happens downstream. Software inclusions such as `MS Office Home 2024` are not flagged.

**Not inferred.** CPU, RAM, refresh rate, and other specifications are kept only as the page's raw text. A missing row stays missing. GTIN appears in JSON-LD but has no `OfferDetails` field; it is captured only where the retailer uses it as `sku`. No `target_id` is assigned.

## Versioning and maintenance

- Bump `adapter_version` (`dbz-1` → `dbz-2` and so on) whenever parsing rules or assumptions change. Every observation records the version in `source.adapter_version`.
- When the theme changes, expect `UNRECOGNIZED_LAYOUT`, `TITLE_NOT_FOUND`, or a price failure rather than wrong data. Update the fixtures from a newly reviewed page, keeping markup minimal with no full page copies or personal data. Then adjust the parser and bump the version.
- The storefront theme is named "Copy 3 of Warehouse Last (Enhancify-dev)", which suggests ongoing customization; recheck the main-form id, `.price-list`, and button classes after any visible redesign.
- Keep `DATA_USE_REVIEW_STATUS` and `live_use_approved` in step with [RETAILER_DATA_USE_REVIEW.md](../RETAILER_DATA_USE_REVIEW.md) and [DECISIONS.md](../DECISIONS.md). The current owner policy authorizes internal collection; it does not verify retailer permission.

## Known limitations

- **Live behavior has one spot check.** The user-reported check returned one in-stock, single-variant MSI offer. Its final URL, offer URL, and bundle fields were not reported. No sold-out, pre-order, sale-priced, or multi-variant page has been checked live.
- Only single-variant laptop pages were reviewed. Multi-variant products (several JSON-LD offers) are `AMBIGUOUS_PRICE`, since the displayed price belongs to whichever variant is preselected.
- The `Sold out`, `Pre-order`, and `Unavailable` labels come from the theme's own strings on the reviewed pages. No sold-out or pre-order laptop page was reviewed, so their markup is assumed, not observed.
- Shopify JSON-LD availability is usually derived from whether the variant can be bought, so it may say `InStock` for items sold on continued selling or as pre-orders. A `Pre-order` button with `InStock` JSON-LD is treated as a conflict and gives `UNKNOWN`.
- Without Product JSON-LD, availability is `UNKNOWN` unless the button says `Sold out` or `Pre-order`.
- Bundle detection is keyword based and can over-flag, for example a title that mentions a monitor size, or under-flag freebies named with other words.
- Brand comes from JSON-LD or the vendor link; MPN is blank on the reviewed pages. Phase 8 must use the raw title, specs, and categories as evidence without treating them as verified identity.
- Collection, search, and collection-scoped product pages are not parsed, and no crawling or discovery exists.
