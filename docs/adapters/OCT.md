# Octagon (OCT) Adapter

**Status:** Adapter implemented and tested offline on 2026-10-01 (`adapter_version = "oct-1"`). **Live use is enabled** (`live_use_approved = True`) under the project-wide owner decision in [DECISIONS.md](../DECISIONS.md). Octagon's published terms of service prohibit using the site or its content to "spider, crawl, or scrape", so the external-policy finding is **RESTRICTED**. No Octagon written permission is recorded, and the owner decision is not Octagon's consent. **No live smoke check has been run through the project scraper, and no observation has been persisted.** Adapter maintainer: not yet named.

| Item | Value |
|---|---|
| Module | `src/laptop_tracker/adapters/oct.py` |
| Class | `OCTAdapter` |
| `retailer_id` / retailer code | `oct` / `OCT` (matches `OCT_Price` in [PRICE_TRACKING_OUTPUT_SCHEMA.md](../PRICE_TRACKING_OUTPUT_SCHEMA.md)) |
| Allowed hosts | `www.octagon.com.ph` only (exact). The apex `octagon.com.ph`, the `octagoncomputer-superstore.myshopify.com` domain, Octagon's marketplace shops (for example on Lazada), and all other hosts are rejected. |
| Input | One fetched product page (`https://www.octagon.com.ph/products/<handle>`, optionally `?variant=<id>`). Build requests with `product_request(url)` or `product_request(url, variant_id)`. |
| Output | One `ParsedOffer` with `OfferDetails`, or a `ParseError` |
| Tests | `tests/test_oct.py`, fixtures in `tests/scraping_fixtures/oct/` |
| Project data-use status | `DATA_USE_REVIEW_STATUS = "OWNER_APPROVED"`; external finding RESTRICTED |

## Retailer and host confirmation

`OCT` is the project's code from `OCT_Price`; Octagon itself does not use it. Its own email address and X handle use `OCS` (`ocs@octagon.com.ph`, `@Ocs_Octagon`). The Phase 7 request for this adapter identified `OCT` as Octagon. One web search then found the storefront `www.octagon.com.ph` and Octagon's Lazada shops. The identity and host below come from Octagon's own pages, not from search snippets.

| Check (2026-10-01) | Result |
|---|---|
| `https://octagon.com.ph/` | HTTP 301 to `https://www.octagon.com.ph/` (`powered-by: Shopify`, served through Cloudflare). |
| `https://www.octagon.com.ph/` | HTTP 200. `og:site_name` and the Organization JSON-LD name are "Octagon Computer Superstore". The shop is `octagoncomputer-superstore.myshopify.com`, the contact email is `ocs@octagon.com.ph`, and the page links the `facebook.com/OctagonComputerSuperstore` and `x.com/Ocs_Octagon` accounts. |
| Terms of service | "This website is operated by Octagon Computer Superstore"; "Octagon Computer Superstore is owned and operated by Upson International Corp." |
| Privacy policy | Names the same shop domain and lists "Octagon Computer Superstore, Upson International Corp., 747 Romualdez St. Ermita, 1000 Manila". |
| About Us | Gives the same Ermita, Manila head office and describes Octagon Computer Superstore as a computer retailer established in 1982. |

The theme is **"Multi" 2.0.1** (Shopify theme store 2337). Product pages are at `/products/<handle>`. Only `www` is allowlisted. A request to the apex is refused, and a redirect from `www` to any other host fails with `REDIRECT_HOST_NOT_ALLOWED` before it is sent.

## Setup

No configuration or credentials are needed. The adapter never performs HTTP; it only parses a `FetchedPage` from the shared `Fetcher`. Run the offline tests with:

```bash
python -m pytest -q tests/test_oct.py
```

The default `Fetcher()` uses a live transport, so `scrape(OCTAdapter(), [...], Fetcher())` can send a real request. The project-owner decision enables this internal use. It does not change the RESTRICTED external-policy finding.

`product_request` accepts only `https://www.octagon.com.ph/products/<handle>`, with an optional trailing slash and at most one query, `variant=<digits>`. It refuses:
- `http`, the apex host, and other hosts;
- collections, search (including the old `index.php?route=product/search` form), and collection-scoped product URLs;
- cart, checkout, account, and policy pages;
- `.js` or `.json` product endpoints;
- fragments and any other query.

## Page assumptions

These come from three product pages reviewed on 2026-10-01: two in-stock single-variant laptops at regular prices (Lenovo and HP), and one in-stock single-variant headset on sale, reviewed only for the sale markup. A real product page is about 0.08 MB. Fixtures are minimal synthetic pages with invented names and IDs.

| Data | Primary source | Fallback |
|---|---|---|
| Title | `h1` inside `.#product-title` in the main block (visible text). It must equal the JSON-LD `Product.name` when both exist. | JSON-LD `Product.name` |
| Product URL | The selected variant's JSON-LD `Offer.url` (it carries `?variant=<id>`) | `Product.url`, then `<link rel="canonical">` |
| Current price | The `#price-item @regular` amount in the main `dl.#price` block (see price rules). It is cross-checked with the selected JSON-LD `Offer.price` and the `var meta` variant `price` (integer cents). | JSON-LD `Offer.price` when no price is displayed |
| Regular price | The `#price-item @compare` amount, only when the current item has `@on-sale` | None |
| Currency | JSON-LD `priceCurrency` must be `PHP`; each displayed amount must show `₱` or `PHP` | None |
| Availability | Selected JSON-LD offer `availability`, the stock alert (`.#product-stock-alert-text`), and the add-to-cart button (`data-element="to-cart-button"`), combined | None |
| Selected variant | The `name="id"` inputs in the main block (installment form, `<noscript>`, product form), which must agree | None |
| Variant data | `ShopifyAnalytics` `var meta` variants (`id`, `public_title`, `price`, `sku`) and the theme's `data-element="variants-data"` JSON (variant ID to `options`) inside `<product-variant-provider>`. Their variant IDs must match. | None |
| SKU | The visible `SKU:` line, the selected JSON-LD offer `sku`, and the `var meta` variant `sku`; all that are present must agree. It is kept as text, so leading zeros stay. | None |
| Product ID | The `product-id` hidden inputs | `var meta` `product.id` |
| Brand | **Never set.** JSON-LD `brand.name` and the `var meta` `vendor` are both "Octagon Computer Superstore" on every reviewed page, so neither names the maker. A maker name in the title is not extracted. | None |
| MPN | Never set. Part numbers in the title or description stay raw text. | None |
| Categories | `var meta` `product.type` (for example "Laptop") | None |
| Configuration text | For a multi-variant product, the selected variant's option title first, then the description lines joined as `label: value \| …` | JSON-LD `description` |
| Raw specifications | Each non-empty line of the first `div.#rte` in the separate section whose `h2.#hero-heading` reads "Tech Specification". The line is split at the first colon into `(label, value)`. Lines come from `<p>`, `<div>`, `<li>`, `<tr>`, and `<br>`. A line without a colon, such as `✅ 8GB DDR5 MEMORY` or `DESIGN`, is kept with an empty value. | None |

"Main" means inside the theme's `div.#product` (`productPageContainer-…`). These are ignored:
- the installment-link rich text (`.#rte-block`) and the "Share this product" block;
- hidden text (`visually-hidden`, `hidden` class or attribute);
- everything inside `<template>`;
- the header, cart drawer, the other sections, and product cards (`.#product-card`), including their sale prices and "Sale" or "Sold out" badges.

**Identity rules.** The page fails with `AMBIGUOUS_PRODUCT` if any of these holds:
- there is more than one main block, or more than one distinct `h1` title;
- the `h1` and the JSON-LD name differ;
- the page has several Product JSON-LD nodes that are not identical, or that name different paths;
- the JSON-LD URL, the canonical link, or the `var meta` handle names a different product path from the fetched page's final URL (compared case-insensitively, without a trailing slash);
- the `product-id` inputs disagree with each other or with `var meta`;
- `var meta` names several products.

**JSON-LD defect.** The theme writes barcodes as bare JSON numbers. A barcode with a leading zero (`"gtin12": 097855145710`, seen on the reviewed sale page) makes the whole block invalid JSON. Before parsing, the adapter quotes bare digit values of `gtin`, `gtin8`, `gtin12`, `gtin13`, and `gtin14`; nothing else in the block is changed. A block that is still invalid is ignored, and the visible content is used.

**Variant rules.** All reviewed products have one `Default Title` variant: `public_title` is null, and `variants-data` lists one ID with `options: ["Default Title"]`. No multi-variant Octagon page was observed. Variant handling follows the PCC pattern and the data shapes seen on single-variant pages. A product counts as multi-variant when any of these holds:
- `var meta` or `variants-data` lists more than one variant;
- JSON-LD lists more than one offer;
- a `select[name=id]` in the main block has more than one option.

For such a product, the request must name `?variant=<id>`. The page's forms must select that same ID, and the ID must appear in the variant data. Otherwise the page fails with `AMBIGUOUS_VARIANT`. These also give `AMBIGUOUS_VARIANT`:
- form IDs that disagree, or a redirect that changes the variant;
- unreadable `variants-data`, or `var meta` and `variants-data` listing different IDs;
- shown, JSON-LD, and analytics SKUs that disagree, or a SKU line that is not `SKU: …`;
- a single-variant page whose ID differs from the requested one.

The JSON-LD offer whose URL names the selected variant is used. If there is no single such offer, the page fails with `AMBIGUOUS_PRICE`. The variant picker markup was not observed and is not parsed.

**Price rules.** The theme renders `dl.#price` with `#price-item` entries. Each has a heading (`dt`, for example "Price", "Sale price", "Regular price") and an amount in `span.#price-value`. Displayed amounts have no decimals, for example `₱44,999`.
- **SALE:** the `@regular` item also has `@on-sale`, and the `@compare` amount is higher. A compare amount equal to the current amount gives `REGULAR`.
- **REGULAR:** there is no `@on-sale`; the `@regular` amount is the price.
- **No displayed price:** if only JSON-LD gives a price, `price_type`, `regular_price`, and `sale_price` stay empty, and the price text is the JSON-LD value (for example `44999.0`).
- The `@save` item ("You save 8%") is a percentage, not a price, and is ignored. The `og:price:amount` meta tag is not used.
- **Parse failures:**
  - `PRICE_CONFLICT`: any of the following.
    - The displayed, JSON-LD, and analytics prices disagree.
    - The compare amount is below the sale amount.
    - A higher compare amount appears without `@on-sale`.
  - `AMBIGUOUS_PRICE`: any of the following.
    - Several amounts in one item, or several price blocks in the main area.
    - An `@on-sale` item without a compare amount, or a block without an `@regular` item.
    - An amount in a `#price-item` of any other kind.
    - A range, several numbers, or wording besides the amount, for example `From ₱…`.
    - An `AggregateOffer`, or no single JSON-LD offer for the selected variant.
  - `PRICE_NOT_FOUND`: no amount with digits and no JSON-LD price.
  - `UNSUPPORTED_CURRENCY`: a non-peso symbol or code, or a non-PHP `priceCurrency`.
  - `CURRENCY_NOT_FOUND`: a displayed amount without `₱` or `PHP`, or a JSON-LD-only price without `priceCurrency: PHP`.
  - `INVALID_PRICE`: zero or malformed numbers.

**Availability rules.** The explicit signals are:
- the selected JSON-LD offer's `availability`, compared by its last path segment. InStock maps to `IN_STOCK`, OutOfStock or SoldOut to `OUT_OF_STOCK`, and PreOrder to `PREORDER`; other values, such as BackOrder, give `UNKNOWN`;
- the stock alert. `Only <n> left in stock!` (observed) and `In stock` map to `IN_STOCK`; `Sold out` and `Out of stock` map to `OUT_OF_STOCK`. Any other wording is kept as evidence and counts as `UNKNOWN`;
- the button labels `Sold out` (`OUT_OF_STOCK`), `Pre-order` (`PREORDER`), and `Unavailable` (`UNKNOWN`). "Sold out" and "Unavailable" are the theme's own strings; their rendered markup was not observed.

`Add to cart` is not stock wording; it only confirms another signal. A missing stock alert is not a signal. The explicit signals must agree, and the button (disabled through `disabled` or `aria-disabled="true"`) must be consistent with them:
- `IN_STOCK` needs an enabled `Add to cart`.
- `PREORDER` needs an enabled `Pre-order` or `Add to cart`.
- `OUT_OF_STOCK` needs the button absent, disabled, or labelled `Sold out`.

Otherwise the result is `UNKNOWN` with `signals conflict` in the evidence. That includes several different buttons or several different stock alerts in the main block. With no explicit signal, for example a page without JSON-LD or a stock alert that shows only `Add to cart`, the result is `UNKNOWN`. Observed evidence on both laptop pages: `JSON-LD availability: InStock; stock alert: Only 2 left in stock!; add to cart button: Add to cart (enabled)` (the count differs per page).

**Bundles.** Offers are flagged, never dropped, when the title or the selected variant title contains `FREE` (including `+ FREE`), or one of `BUNDLE`, `MONITOR`, `TABLET`, `CHAIR`, `BACKPACK`, `BRIEFCASE`, `BAG`, `MOUSE`, or `HEADSET`. A plural `S` is allowed, matching is case-insensitive, and only whole words count. The evidence records the source, for example `title term BACKPACK: BACKPACK`. The terms match the DataBlitz, PC Express, and PC Central adapters. No Octagon bundle page was reviewed. The reviewed laptop titles include bundled software (`OFFICE HOME 2024 + OFFICE 365 BASIC`), which is not flagged. The reviewed headset is flagged by its own `HEADSET` title term; this shows that the keyword check can over-flag non-laptop products.

**Not inferred.** CPU, RAM, refresh rate, and other specifications are kept only as the page's raw text. A missing row stays missing. Description text keeps the retailer's characters, including `✅` markers and spaces before colons (`Chipset    : …` becomes the label `Chipset`). No `target_id` is assigned.

## Versioning and maintenance

- Bump `adapter_version` (`oct-1` → `oct-2` and so on) whenever parsing rules or assumptions change. Every observation records the version in `source.adapter_version`.
- When the theme changes, expect `UNRECOGNIZED_LAYOUT`, `TITLE_NOT_FOUND`, `AMBIGUOUS_PRODUCT`, `AMBIGUOUS_VARIANT`, or a price failure rather than wrong data. Update the fixtures from a newly reviewed page, keeping markup minimal with no full page copies or personal data. Then adjust the parser and bump the version.
- After a theme update, recheck:
  - `div.#product` and `.#product-title h1`;
  - `dl.#price` and its `#price-item @regular`, `@on-sale`, `@compare`, and `@save` classes;
  - `.#product-stock-alert-text` and `data-element="to-cart-button"`;
  - the `b-sku` listener;
  - `variants-data` inside `<product-variant-provider>`;
  - the "Tech Specification" section;
  - the JSON-LD `gtin12` output.
- Scrape requests for multi-variant products need variant IDs. Take them from the product's JSON-LD offers or `var meta` variants when a URL is reviewed; the adapter does not discover them.
- Keep `DATA_USE_REVIEW_STATUS` and `live_use_approved` in step with [RETAILER_DATA_USE_REVIEW.md](../RETAILER_DATA_USE_REVIEW.md) and [DECISIONS.md](../DECISIONS.md).

## Known limitations

- **No live check through the project scraper has been run.** The adapter was checked offline against the three research pages saved temporarily outside the repository. It gave REGULAR PHP 44,999 and PHP 37,999 `IN_STOCK` laptop offers, and a SALE PHP 2,295 (regular 2,495) `IN_STOCK` headset offer through the `gtin12` repair. A controlled live smoke check is a later step.
- Multi-variant, sold-out, pre-order, unavailable, and bundle pages were not observed. Their handling is assumed from the theme's strings and the data shapes on single-variant pages.
- The stock alert was seen only as a low-stock warning (`@type:warning`, "Only 1 left" or "Only 2 left"). It may be absent at higher stock levels; a page without it depends on JSON-LD and the button.
- Shopify's JSON-LD `InStock` usually means the variant can be bought online. It may be true for items sold on continued selling, and it does not describe branch stock.
- Brand is always blank, because the page's brand fields name the store. Phase 8 must take the brand from the title text or other evidence.
- The description is free-form retailer text. Splitting at the first colon does not verify specifications; Phase 8 must treat it as evidence.
- Categories come only from the Shopify product type in `var meta`; collection memberships are not on the page.
- Bundle detection is keyword based and can over-flag or under-flag.
- robots.txt and Shopify's platform comments point agents to UCP/MCP catalog endpoints. No adapter for those exists. Using them would need its own review and would still be subject to Octagon's terms.
- Collection, search, and collection-scoped product pages are not parsed, and no crawling or discovery exists.
