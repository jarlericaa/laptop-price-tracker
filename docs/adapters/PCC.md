# PC Central (PCC) Adapter

**Status:** Adapter implemented and tested offline on 2026-10-01 (`adapter_version = "pcc-1"`). **Live use is enabled** (`live_use_approved = True`) under the project-wide owner decision in [DECISIONS.md](../DECISIONS.md). PC Central's published pages neither grant nor prohibit automated collection, so the external-policy finding is **UNKNOWN**. No retailer permission is recorded. A bare-URL live check fetched the page and failed safely with `AMBIGUOUS_VARIANT`; a selected-variant check returned one in-stock ASUS VivoBook offer at PHP 36,999.00. See [PROGRESS_LOG.md](../PROGRESS_LOG.md#pcc-live-smoke-checks-2026-10-01). Adapter maintainer: not yet named.

| Item | Value |
|---|---|
| Module | `src/laptop_tracker/adapters/pcc.py` |
| Class | `PCCAdapter` |
| `retailer_id` / retailer code | `pcc` / `PCC` (matches `PCC_Price` in [PRICE_TRACKING_OUTPUT_SCHEMA.md](../PRICE_TRACKING_OUTPUT_SCHEMA.md)) |
| Allowed hosts | `pccentralcomputerstore.com` only (exact). `www.pccentralcomputerstore.com`, `pccentral.com.ph`, the `myshopify.com` domain, and all other hosts are rejected. |
| Input | One fetched product page (`/products/<handle>`, optionally `?variant=<id>`). Build requests with `product_request(url)` or `product_request(url, variant_id)`. |
| Output | One `ParsedOffer` with `OfferDetails`, or a `ParseError` |
| Tests | `tests/test_pcc.py`, fixtures in `tests/scraping_fixtures/pcc/` |
| Project data-use status | `DATA_USE_REVIEW_STATUS = "OWNER_APPROVED"`; external finding UNKNOWN |

## Retailer and host confirmation

The `PCC` code is not defined anywhere in the repository. Web searches on 2026-10-01 found more than one Philippine laptop retailer that fits it (PC Central and PC Chain), so the user was asked. The user confirmed **PC Central** on 2026-10-01.

| Check (2026-10-01) | Result |
|---|---|
| `https://pccentral.com.ph/` and `https://www.pccentral.com.ph/` | Both returned HTTP 301 to an unrelated third-party domain. This is the domain search engines still index for PC Central. The redirect was not followed, and the domain must never be allowlisted. |
| `https://pccentralcomputerstore.com/` | HTTP 200, a Shopify storefront (`powered-by: Shopify`, served through Cloudflare). Shop `pc-central-computer-store.myshopify.com`, page title "PC Central PH", `og:site_name` "PC Central", Organization JSON-LD name "PC Central". It links the `facebook.com/PCCentral.PH`, `instagram.com/pccentral.ph`, and `tiktok.com/@pccentraltechtok` accounts. The Facebook page is the one search results list for PC Central. |
| `https://www.pccentralcomputerstore.com/` | HTTP 301 to the apex. |
| `pccentralstore.com` (also listed by search) | Did not resolve in DNS. |

The theme is Shopify **Dawn 11.0.0** (theme name "Feb 5 2026 Home Credit"). Product pages are at `/products/<handle>`. Only the apex is allowlisted, so a future redirect to `www` or any other host fails with `REDIRECT_HOST_NOT_ALLOWED` before it is sent.

## Setup

No configuration or credentials are needed. The adapter never performs HTTP; it only parses a `FetchedPage` from the shared `Fetcher`. Run the offline tests with:

```bash
python -m pytest -q tests/test_pcc.py
```

The default `Fetcher()` uses a live transport, so `scrape(PCCAdapter(), [...], Fetcher())` can send a real request. The project-owner decision enables this internal use without changing the retailer's external-policy finding.

`product_request` accepts only `https://pccentralcomputerstore.com/products/<handle>`, with an optional trailing slash and at most one query, `variant=<digits>`. It refuses collections, search, cart, policy pages, collection-scoped product URLs, fragments, and any other query.

## Page assumptions

These come from two in-stock laptop product pages reviewed on 2026-10-01: an HP page with three RAM variants, the first on sale, and an Acer page with two RAM variants at regular prices. A real product page is about 0.19 MB. Fixtures are minimal synthetic pages with invented names and IDs.

| Data | Primary source | Fallback |
|---|---|---|
| Title | `h1` inside `div.product__title` in the main block (visible text). It must equal the JSON-LD `Product.name` when both exist. | JSON-LD `Product.name` |
| Product URL | The selected variant's JSON-LD `Offer.url` (it carries `?variant=<id>`) | `Product.url`, then `<link rel="canonical">`, then the page URL |
| Current price | Dawn's `div.price` block (see price rules). It is cross-checked with the selected JSON-LD `Offer.price` and the variant JSON `price` (integer cents). | JSON-LD `Offer.price` when no price is displayed |
| Regular price | The `<s>` compare-at amount, only when the block has `price--on-sale` | None |
| Currency | JSON-LD `priceCurrency` must be `PHP`; each displayed amount must show `₱` or `PHP` | None |
| Availability | Selected JSON-LD offer `availability`, variant JSON `available`, the add-to-cart button, and `price--sold-out`, combined | None |
| Selected variant | The `name="id"` inputs in the main product forms, which must agree | None |
| Variant data | The JSON in `<variant-radios>` or `<variant-selects>` (`id`, `title`, `price`, `compare_at_price`, `available`, `sku`) | `ShopifyAnalytics` `var meta` variants (no compare-at or availability) |
| SKU | The selected variant's `sku`, kept as text. It is null on both reviewed pages, so it is usually blank. | None |
| Product ID | The `product-id` hidden input | `var meta` `product.id` |
| Brand | JSON-LD `brand.name` | `var meta` `product.vendor` |
| MPN | Never set. No field states one; part numbers in the description stay raw text. | None |
| Categories | `var meta` `product.type` (for example "Laptop") | None |
| Configuration text | For a multi-variant product, the selected variant title first (for example `16GB (2x8) DDR4 RAM / 256GB SSD`), then the description lines joined as `label: value \| …` | JSON-LD `description` |
| Raw specifications | Each non-empty line of `.product__description`, split at the first colon into `(label, value)`. Lines come from `<p>`, `<div>`, `<li>`, `<tr>`, and `<br>`. A line without a colon is kept with an empty value. | None |

"Main" means inside Dawn's `<product-info>` (`.product__info-container`). These are ignored:
- the vendor caption above the title (`p.product__text`), because Dawn uses the same class for other text blocks;
- the `h2` title copy inside the quick-add link;
- badges (`.badge`), including the "Sale" and "Sold out" badges that are always in the markup;
- hidden text (`visually-hidden`, `hidden` class or attribute), such as "Variant sold out or unavailable" labels, the loading spinner, the unit-price line, and the error message;
- everything inside `<template>`, such as the pickup-availability preview;
- the header, cart drawer, and recommendation cards outside the main block.

**Identity rules.** The page fails with `AMBIGUOUS_PRODUCT` if any of these holds:
- there is more than one main block, or more than one distinct `h1` title;
- the `h1` and the JSON-LD name differ;
- the page has several Product JSON-LD nodes that are not identical, or that name different paths;
- the JSON-LD URL and the canonical link name different paths;
- the `product-id` inputs disagree with each other or with `var meta`;
- the JSON-LD brand and the `var meta` vendor disagree;
- `var meta` names several products.

A broken JSON-LD block is ignored, and the visible content is used.

**Variant rules.** On both reviewed pages, each variant is a different configuration (RAM upgrades at different prices). A bare product URL shows the first available variant, so its price cannot be attributed to one configuration. A product counts as multi-variant when any of these holds:
- the variant JSON or `var meta` lists more than one variant;
- JSON-LD lists more than one offer;
- the `<noscript>` `select[name=id]` has more than one option;
- the pickup block says `data-has-only-default-variant="false"`.

For such a product, the request must name `?variant=<id>`, the page's form must select that same ID, and the ID must appear in the variant data. Otherwise the page fails with `AMBIGUOUS_VARIANT`. Disagreeing form IDs, a redirect that changes the variant, unreadable variant JSON, or a single-variant page whose ID differs from the requested one also give `AMBIGUOUS_VARIANT`. The JSON-LD offer whose URL names the selected variant is used. If there is no single such offer, the page fails with `AMBIGUOUS_PRICE`.

Assumption: Shopify renders the requested variant as selected when the URL has `?variant=<id>`. The adapter does not rely on this blindly. It requires the form to select the requested ID and the displayed price to match that variant's JSON-LD and variant-JSON prices. In an offline check of a reviewed page, a request for the second variant failed with `AMBIGUOUS_VARIANT` because the saved page selected the first. A live `?variant=` request for an ASUS VivoBook selected variant succeeded on 2026-10-01.

Single-variant products were not observed. Their handling (one offer, no `<variant-radios>`, `data-has-only-default-variant="true"`, a "Default Title" variant) follows Dawn's standard template.

**Price rules.** Dawn renders two containers in `div.price`. `.price__regular` holds the current price. `.price__sale` holds an `<s>` compare-at amount and `.price-item--sale`. The theme's CSS shows `.price__sale` only when the block has `price--on-sale`.
- **SALE:** the block has `price--on-sale`, and the `<s>` amount is higher than the sale amount. A compare-at amount equal to the sale amount gives `REGULAR`.
- **REGULAR:** there is no `price--on-sale`. The regular amount is the price. A compare-at amount at or below it is hidden by the theme and ignored. On a reviewed page, the second variant had a compare-at price below its price.
- **No displayed price:** if only JSON-LD gives a price, `price_type`, `regular_price`, and `sale_price` stay empty, and the price text is the JSON-LD value (for example `69999.0`).
- **Parse failures:**
  - `PRICE_CONFLICT`: any of the following.
    - The displayed, JSON-LD, and variant prices disagree.
    - The two containers disagree.
    - A compare-at amount is below the sale amount.
    - A higher compare-at amount appears without `price--on-sale`.
    - The variant has a higher `compare_at_price` that is not shown.
    - The shown compare-at amount differs from the variant's.
  - `AMBIGUOUS_PRICE`: any of the following.
    - Several amounts in one container, or several price blocks in the main area.
    - An on-sale block without both amounts.
    - A range, several numbers, or wording besides the amount, for example `From ₱…`.
    - An `AggregateOffer`, or no single JSON-LD offer for the selected variant.
  - `PRICE_NOT_FOUND`: no amount with digits and no JSON-LD price.
  - `UNSUPPORTED_CURRENCY`: a non-peso symbol or code, or a non-PHP `priceCurrency`.
  - `CURRENCY_NOT_FOUND`: a displayed amount without `₱` or `PHP`, or a JSON-LD-only price without `priceCurrency: PHP`.
  - `INVALID_PRICE`: zero or malformed numbers.

**Availability rules.** The explicit signals are:
- the selected JSON-LD offer's `availability`, compared by its last path segment. InStock maps to `IN_STOCK`, OutOfStock or SoldOut to `OUT_OF_STOCK`, and PreOrder to `PREORDER`; other values, such as BackOrder, give `UNKNOWN`;
- the variant JSON `available` flag (`true` is `IN_STOCK`, `false` is `OUT_OF_STOCK`). The `var meta` fallback never supplies it;
- `price--sold-out` on the price block (`OUT_OF_STOCK`);
- the button labels `Sold out` (`OUT_OF_STOCK`), `Pre-order` (`PREORDER`), and `Unavailable` (`UNKNOWN`).

`Add to cart` is not stock wording; it only confirms another signal. The explicit signals must agree, and the button (`button.product-form__submit`, disabled through `disabled` or `aria-disabled="true"`) must be consistent with them:
- `IN_STOCK` needs an enabled `Add to cart` and no sold-out state.
- `PREORDER` needs an enabled `Pre-order` or `Add to cart`.
- `OUT_OF_STOCK` needs the button absent, disabled, or labelled `Sold out`.

Otherwise the result is `UNKNOWN` with `signals conflict` in the evidence. That includes several different buttons in the main block. With no explicit signal, for example a page without JSON-LD that shows only `Add to cart`, the result is `UNKNOWN`. Observed evidence (both reviewed pages, first variant): `JSON-LD availability: InStock; variant available: true; add to cart button: Add to cart (enabled); price sold-out state: no`. Dawn's sold-out markup was not observed and is tested from the standard template.

**Bundles.** Offers are flagged, never dropped, when the title or the selected variant title contains `FREE` (including `+ FREE`), or one of `BUNDLE`, `MONITOR`, `TABLET`, `CHAIR`, `BACKPACK`, `BRIEFCASE`, `BAG`, `MOUSE`, or `HEADSET`. A plural `S` is allowed, matching is case-insensitive, and only whole words count. The evidence records the source, for example `title term BACKPACK: Backpack` or `variant term MOUSE: Mouse`. The terms match the DataBlitz and PC Express adapters. No PC Central bundle page was reviewed.

**Not inferred.** CPU, RAM, refresh rate, and other specifications are kept only as the page's raw text. The page title can disagree with the selected variant. For example, a title says "8GB" while the selected variant is "16GB (2x8) DDR4 RAM"; both are kept as shown. A missing row stays missing. Description text keeps the retailer's characters, including `?` where the reviewed page has lost `™`. No `target_id` is assigned.

## Versioning and maintenance

- Bump `adapter_version` (`pcc-1` → `pcc-2` and so on) whenever parsing rules or assumptions change. Every observation records the version in `source.adapter_version`.
- When the theme changes, expect `UNRECOGNIZED_LAYOUT`, `TITLE_NOT_FOUND`, `AMBIGUOUS_PRODUCT`, `AMBIGUOUS_VARIANT`, or a price failure rather than wrong data. Update the fixtures from a newly reviewed page, keeping markup minimal with no full page copies or personal data. Then adjust the parser and bump the version.
- After a theme update, recheck `<product-info>`, `div.price` and its `price--on-sale` and `price--sold-out` classes, `.price__regular`, `.price__sale`, `button.product-form__submit`, the `<variant-radios>` JSON, and `.product__description`.
- Scrape requests for multi-variant products need variant IDs. Take them from the product's JSON-LD offers or variant list when a URL is reviewed; the adapter does not discover them.
- Keep `DATA_USE_REVIEW_STATUS` and `live_use_approved` in step with [RETAILER_DATA_USE_REVIEW.md](../RETAILER_DATA_USE_REVIEW.md) and [DECISIONS.md](../DECISIONS.md).
- Re-check the host if PC Central changes domains again. The old `pccentral.com.ph` domain redirected off-site on 2026-10-01, which suggests it is no longer under the retailer's control.

## Known limitations

- **Live behavior has been spot-checked.** The bare-URL check fetched a multi-variant product and failed safely with `AMBIGUOUS_VARIANT`. A selected-variant request parsed one in-stock ASUS VivoBook offer at PHP 36,999.00. Other products and page states remain unverified.
- Both reviewed pages, and the live-check product, are multi-variant. Single-variant, sold-out, pre-order, and bundle pages were not observed; their markup is assumed from Dawn's standard template.
- Shopify's `available` and JSON-LD `InStock` usually mean the variant can be bought. They may be true for items sold on continued selling.
- Variant SKUs are null on reviewed pages, so `retailer_sku` is usually blank; the variant ID is kept in the offer URL.
- The description is free-form retailer text. Splitting at the first colon does not verify specifications; Phase 8 must treat it as evidence.
- Categories come only from the Shopify product type in `var meta`; collection memberships are not on the page.
- Bundle detection is keyword based and can over-flag or under-flag.
- robots.txt and Shopify's platform comments point agents to UCP/MCP catalog endpoints. No adapter for those exists, and using them would need its own review.
- Collection, search, and collection-scoped product pages are not parsed, and no crawling or discovery exists.
