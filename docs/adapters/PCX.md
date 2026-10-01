# PC Express (PCX) Adapter

**Status:** Adapter implemented and tested offline on 2026-10-01 (`adapter_version = "pcx-1"`). **Live use is enabled** (`live_use_approved = True`) by the explicit project-owner PCX decision in [DECISIONS.md](../DECISIONS.md). PC Express's published terms restrict automated collection without express written consent; the external-policy finding remains **RESTRICTED**, and no written consent is recorded. A user-reported live check returned one in-stock Acer Aspire Lite offer; its offer URL appeared as a Markdown link in the reported output. A controlled live check on 2026-10-01 of the reviewed HP 14 laptop page printed `repr(offer.offer_url)` as the bare `'https://pcx.com.ph/products/…'` string (a `str`, equal to the requested and final URLs). See [PROGRESS_LOG.md](../PROGRESS_LOG.md#phase-7-live-follow-up-checks-2026-10-01). Adapter maintainer: Jarelle Ricaforte.

| Item | Value |
|---|---|
| Module | `src/laptop_tracker/adapters/pcx.py` |
| Class | `PCXAdapter` |
| `retailer_id` / retailer code | `pcx` / `PCX` (matches `PCX_Price` in [PRICE_TRACKING_OUTPUT_SCHEMA.md](../PRICE_TRACKING_OUTPUT_SCHEMA.md)) |
| Allowed hosts | `pcx.com.ph` only (exact). `www.pcx.com.ph` and other hosts are rejected. |
| Input | One fetched product page (`/products/<handle>`). Build requests with `product_request(url)`. |
| Output | One `ParsedOffer` with `OfferDetails`, or a `ParseError` |
| Tests | `tests/test_pcx.py`, fixtures in `tests/scraping_fixtures/pcx/` |
| Project data-use status | `DATA_USE_REVIEW_STATUS = "OWNER_APPROVED"`; external finding RESTRICTED |

## Host confirmation

On 2026-10-01, `https://pcx.com.ph/` returned HTTP 200, and `https://www.pcx.com.ph/` returned HTTP 301 to it. The site identifies itself as "PC Express" in its title, `og:site_name`, and footer ("© 2026 PC Express"), and it links the `pcexpressph` social accounts. It is a Shopify storefront (`powered-by: Shopify`, served through Cloudflare) using the Kalles theme (version 4.1.4, named `pcx-themev1/main`). Product pages are at `/products/<handle>`. Only the apex host is allowlisted, so a future redirect to `www` or another host fails with `REDIRECT_HOST_NOT_ALLOWED`.

## Setup

No configuration or credentials are needed. The adapter never performs HTTP; it only parses a `FetchedPage` from the shared `Fetcher`. Run the offline tests with:

```bash
python -m pytest -q tests/test_pcx.py
```

The default `Fetcher()` uses a live transport, so `scrape(PCXAdapter(), [...], Fetcher())` can send a real request. The project-owner decision enables this internal use without changing the retailer's external-policy finding. A user-reported live smoke check and a controlled `repr()` check have succeeded. Collection (`/collections/...`), search, cart, and collection-scoped product URLs are out of scope; `product_request` refuses them.

## Page assumptions

These come from two product pages reviewed on 2026-10-01. One was an in-stock laptop at a regular price. The other was an out-of-stock monitor on sale; no laptop on sale was linked from the home page, and the monitor uses the same product template. A real product page is about 0.5 MB.

| Data | Primary source | Fallback |
|---|---|---|
| Title | `h1.t4s-product__title` in the main block (visible text) | JSON-LD `Product.name` with HTML entities decoded. Both blocks must agree. |
| Product URL | JSON-LD `Offer.url`, then `Product.url` or `@id` | `<link rel="canonical">`, then the page URL. Relative links resolve against the page. |
| Current price | Displayed `div.t4s-product-price`: bare text when regular, `<ins>` when on sale. It is cross-checked with every JSON-LD `Offer.price`. | JSON-LD `Offer.price` when no price is displayed |
| Regular price | `<del>` in the same price block. JSON-LD has no compare-at price. | None |
| Currency | Every JSON-LD `priceCurrency` must be `PHP` | Each displayed amount must show `₱` or `PHP` |
| Availability | JSON-LD `availability`, the main add-to-cart button (label and disabled state), and the "Notify Me When Available" button's visibility, combined | None |
| SKU | JSON-LD `sku` (product and offer), kept as text. On reviewed pages it is PC Express's internal code (for example `LT-…-000089`). | Visible `SKU:` value (`[data-product__sku-number]`) |
| Product ID | Shopify product ID from the main form's hidden `product-id` input | JSON-LD `productID` |
| Brand | JSON-LD `brand.name` | Visible `Vendor:` value |
| MPN | JSON-LD `mpn`, excluding values equal to the SKU. The SEO app copies the SKU into `mpn` and the reviews app leaves it blank, so it is blank on reviewed pages. | None |
| Configuration text | The rich-text summary under the title (`.metafield-rich_text_field`). On the reviewed laptop it is the retailer's slash-separated configuration line; on the monitor it is marketing prose. | Description rows joined as `label: value \| …`, then the JSON-LD `description` |
| Raw specifications | Rows of the Description tab table (`.t4s-tab-content` with id `t4s-tab-des…`), `td`/`td` or `th`/`td`. One-cell rows (section headings) are kept with an empty value. | None |
| Categories | Visible `Type:` value, then the `Categories:` collection links, as shown | None |

"Main" means inside `.t4s-product__info-container`. These are ignored:
- everything inside `<template>`, including the sticky add-to-cart copy;
- header, mini-cart, and recommendation product cards;
- the "SAVE ₱…" badge (`.t4s-badge-price`);
- hidden elements (`hidden`, `t4s-d-none`, `visually-hidden`), such as the loading spinner;
- the static help text "Out of Stock? Contact us.", which appears on every page whatever the stock;
- collection names such as "Website Sale". These are kept as raw categories but never read as sale evidence.

**Two JSON-LD blocks.** Each reviewed page has two Product blocks for the same product. One is from the AVADA SEO Suite app (in `<head>`, with `@id` and HTML-entity-encoded text). The other is from Shopify Product Reviews (in the main section, with `sku`, `productID`, and blank `mpn`). Both must name the same product path, or the page fails with `AMBIGUOUS_PRODUCT`. A second Product block without any URL is also ambiguous. Their price, currency, name, SKU, brand, product ID, and MPN must agree where stated. A broken block is ignored, and the other block or the visible content is used.

**Price rules**
- **SALE or REGULAR:** a `<del>` price higher than the `<ins>` price gives `SALE`. A single bare price, or a `<del>` equal to the `<ins>`, gives `REGULAR`. When only JSON-LD gives a price, `price_type`, `regular_price`, and `sale_price` stay empty, and the price text is the JSON-LD value (for example `47990.0`).
- **Parse failures:**
  - `PRICE_CONFLICT`: the displayed and JSON-LD prices disagree, the two JSON-LD blocks disagree, or the `<del>` price is below the current price.
  - `AMBIGUOUS_PRICE`: any of the following.
    - A range or several numbers in one amount.
    - Wording besides the amount, for example `From ₱…`.
    - Several price blocks in the main area, or several `<ins>`/`<del>` amounts.
    - An `<ins>` without a `<del>`.
    - Several JSON-LD offers, or an `AggregateOffer`.
  - `PRICE_NOT_FOUND`: no amount with digits, for example "Call for price".
  - `UNSUPPORTED_CURRENCY`: a non-peso symbol or currency code, or a non-PHP `priceCurrency`.
  - `CURRENCY_NOT_FOUND`: a displayed amount without `₱` or `PHP`, or a JSON-LD-only price without `priceCurrency: PHP`.
  - `INVALID_PRICE`: zero or malformed numbers.

**Variant rules.** The reviewed pages are single-variant: one hidden `name="id"` input and `data-has-only-default-variant="true"`. The page fails with `AMBIGUOUS_VARIANT` when the main block has `data-has-only-default-variant="false"`, more than one `name="id"` input, or a `<select name="id">`. Multi-variant markup was not observed; these signals come from Shopify's standard product form and the theme's pickup-availability attribute.

**Availability rules.** The explicit signals are:
- JSON-LD `availability`, from either block, compared by its last path segment. InStock maps to `IN_STOCK`, OutOfStock or SoldOut to `OUT_OF_STOCK`, and PreOrder to `PREORDER`. Other values, such as BackOrder, give `UNKNOWN`.
- A visible "Notify Me When Available" button (`button.t4s-pr__notify-stock` without `t4s-d-none`) means `OUT_OF_STOCK`. When hidden, it is not a signal.
- The button labels `Sold out` (`OUT_OF_STOCK`), `Pre-order` (`PREORDER`), and `Unavailable` (`UNKNOWN`).

`Add to cart` is not stock wording; it only confirms another signal. The explicit signals must agree, and the add-to-cart button (`button.t4s-product-form__submit`, disabled through `disabled` or `aria-disabled="true"`) must be consistent with them:
- `IN_STOCK` needs an enabled `Add to cart`.
- `PREORDER` needs an enabled `Pre-order` or `Add to cart`.
- `OUT_OF_STOCK` needs the button absent, disabled, or labelled `Sold out`.

Otherwise the result is `UNKNOWN` with `signals conflict` in the evidence. That includes the two JSON-LD blocks disagreeing, or several different buttons in the main block. With no explicit signal, for example a page without JSON-LD that shows only `Add to cart`, the result is `UNKNOWN`. Observed evidence:
- In stock: `JSON-LD availability: InStock; add to cart button: Add to cart (enabled); notify-me button: hidden`.
- Out of stock: `JSON-LD availability: OutOfStock; add to cart button: Add to Cart (disabled); notify-me button: shown`.

**Bundles.** Offers are flagged, never dropped, when the title contains `FREE` (including `+ FREE`), or one of `BUNDLE`, `MONITOR`, `TABLET`, `CHAIR`, `BACKPACK`, `BRIEFCASE`, `BAG`, `MOUSE`, or `HEADSET`. A plural `S` is allowed, matching is case-insensitive, and only whole words count. `bundle_evidence` records each match, for example `title term BACKPACK: Backpack`. The terms match the DataBlitz adapter. No PCX laptop bundle page was reviewed, but PCX lists console bundles whose titles end in "Bundle".

**Not inferred.** CPU, RAM, refresh rate, and other specifications are kept only as the page's raw text. A missing row stays missing. No `target_id` is assigned.

## Versioning and maintenance

- Bump `adapter_version` (`pcx-1` → `pcx-2` and so on) whenever parsing rules or assumptions change. Every observation records the version in `source.adapter_version`.
- When the theme changes, expect `UNRECOGNIZED_LAYOUT`, `TITLE_NOT_FOUND`, `AMBIGUOUS_PRODUCT`, or a price failure rather than wrong data. Update the fixtures from a newly reviewed page, keeping markup minimal with no full page copies or personal data. Then adjust the parser and bump the version.
- The theme is a customized Kalles build (`pcx-themev1/main`). After any visible redesign, recheck `.t4s-product__info-container`, `.t4s-product-price`, `.t4s-product-form__submit`, `.t4s-pr__notify-stock`, and the Description tab id.
- The two Product JSON-LD blocks come from third-party apps (AVADA SEO Suite and Shopify Product Reviews). If either app is removed or changes its output, recheck the identity-agreement and MPN rules.
- Keep `DATA_USE_REVIEW_STATUS` and `live_use_approved` in step with [RETAILER_DATA_USE_REVIEW.md](../RETAILER_DATA_USE_REVIEW.md) and [DECISIONS.md](../DECISIONS.md).

## Known limitations

- **Live behavior has two one-product checks.** A user-reported call returned an in-stock Acer offer at PHP 34,999.00, with the offer URL displayed as a Markdown link. On 2026-10-01 a controlled call for the reviewed HP 14-EP1144TU page returned REGULAR PHP 47,990.00, `IN_STOCK`, and an offer URL whose `repr()` was the bare product URL. That confirms the adapter returns a plain URL string. The Acer offer's own string was not rechecked, but it comes from the same JSON-LD `Offer.url` path. Both checks were in-stock laptops; the Acer check's price type was not reported. No sale-priced laptop, sold-out, pre-order, or multi-variant page has been checked live. The external-policy finding remains RESTRICTED despite the owner's project authorization.
- Only two pages were reviewed: one in-stock regular-price laptop and one out-of-stock sale monitor. No sale-priced laptop, pre-order, or multi-variant page was observed. The `Sold out`, `Pre-order`, and `Unavailable` labels and the variant markup are assumed.
- Shopify JSON-LD availability is usually derived from whether the variant can be bought, so it may say `InStock` for items on continued selling.
- The configuration summary is free-form retailer text: a configuration line on the reviewed laptop and marketing prose on the monitor. Phase 8 must treat it as evidence, not verified specifications.
- Categories include PC Express merchandising collections ("Active", "Fees products", "Website Sale", "New Products"); they are raw retailer data.
- Bundle detection is keyword based. It can over-flag, for example the reviewed monitor's title ("Portable USB Monitor") is flagged for `MONITOR`. It can also under-flag freebies named with other words.
- Robots.txt and Shopify's platform comments point agents to UCP/MCP catalog endpoints; no adapter for those exists, and using them would need its own review.
- Collection, search, and collection-scoped product pages are not parsed, and no crawling or discovery exists.
