# Retailer Data-Use Review

One entry per retailer. **CLEAR** means the retailer's own sources or written permission support the intended collection, with project-owner approval recorded in [DECISIONS.md](DECISIONS.md). **OWNER_APPROVED** records an explicit project-owner decision to proceed with internal collection; the retailer's external-policy finding is recorded separately and may still be RESTRICTED. **UNKNOWN** means neither external support nor a project-owner authorization has been recorded. The framework enforces the adapter-level project approval with `live_use_approved`; see [SCRAPING_FRAMEWORK.md](SCRAPING_FRAMEWORK.md#live-use-gate).

| Result | Meaning |
|---|---|
| CLEAR | Official sources permit the intended automated collection, or the retailer granted written permission, and the owner approved it. |
| RESTRICTED | Official sources prohibit or limit the intended collection. Live use defaults to blocked unless permission or an applicable explicit owner decision is recorded; an owner decision does not change the external finding. The 2026-10-01 project-wide owner decision covers retailer adapters in this internal tool. |
| OWNER_APPROVED | The project owner authorized internal collection under an explicit assumption. Retailer permission may be unverified or restricted; live use is enabled by the project decision. |
| UNKNOWN | Official sources are silent or unclear, and no project-owner authorization to proceed has been recorded. Live use stays blocked. |

## Laptop Factory (LF): `laptopfactory.com.ph`

- **Project decision: OWNER_APPROVED. Live use is enabled.** `LaptopFactoryAdapter.live_use_approved = True` under the project-wide owner policy recorded in [DECISIONS.md](DECISIONS.md).
- **External policy finding: UNKNOWN.** The pages reviewed below do not grant or prohibit automated collection. The project decision assumes retailer approval for internal use; it is not evidence of retailer permission.
- **Date checked:** 2026-09-29.
- **Method:** one manual GET each, by Claude during implementation, using a descriptive user agent, no cookies, and no login. Five requests in total: robots.txt, the terms page, the privacy page, and two product pages. No category page, search, or pagination was requested. The project scraper was not run.

| Source | URL | Evidence |
|---|---|---|
| robots.txt | https://laptopfactory.com.ph/robots.txt | `User-agent: *`. Disallows `/wp-content/uploads/wc-logs/`, `/wp-content/uploads/woocommerce_transient_files/`, `/wp-content/uploads/woocommerce_uploads/`, `/*?add-to-cart=`, `/*?*add-to-cart=`, and `/wp-admin/` (except `admin-ajax.php`). Lists sitemap `https://laptopfactory.com.ph/wp-sitemap.xml`. It does not disallow `/product/`, `/product-category/`, or `/?s=`. It has no `Crawl-delay`. |
| "Terms and conditions" (footer link) | https://laptopfactory.com.ph/termsandconditions/ | The page is titled "General Warranty Terms and Conditions". It covers the 7-day replacement policy, warranty exclusions, and manufacturer-warranty claims only. It says nothing about website use, automated access, scraping, content reuse, or rate limits. It names Golden Laptops Incorporated as the main store's operator. |
| Privacy policy (footer link) | https://laptopfactory.com.ph/privacy-policy-2/ | A generic template ("Website privacy policy template by WebsitePolicies.com") covering personal data, cookies, and links. It says nothing about automated access or data reuse. It names a different entity, "SHIRTS, LAPTOPS, EQUIPMENT, AND FURNITURE (S.L.E.F.) ENTERPRISES CO.", in template brackets. |
| Product page | https://laptopfactory.com.ph/product/acer-aspire-ai-14-a14-11m-x3bp/ | Returned HTTP 200 with no redirect. Public page with no login, and structure matching the audit. Used only to design fixtures. |
| Product page (bundle) | https://laptopfactory.com.ph/product/tecno-megabook-d-free-ergochair-ttracing-airlite-pro-black/ | Returned HTTP 200 with no redirect. A "+ FREE" chair bundle with WooCommerce smart-bundle markup. Used only to design fixtures. |

**Findings**

- No published terms of use, acceptable-use policy, or data-use restriction was found, so there is neither a grant nor a prohibition of automated collection.
- robots.txt does not exclude product pages. That is a crawler-etiquette signal, not a grant of permission.
- There is no published rate guidance.

**Unresolved external-policy questions**

1. Does Laptop Factory have website terms of use not linked from the product-page footer, or will it grant written permission for low-volume automated price collection?
2. Which legal entity operates the website, given that the warranty page and privacy policy name different entities? Whom should a permission request go to (for example, the listed business email)?
3. What request rate and schedule are acceptable? The framework default is at least 10 seconds between requests; a weekly cadence over a limited product list would be the likely pilot.
4. Is internal price comparison the only intended use, with no republication of product text or images?
5. Who owns ongoing maintenance of the adapter? No adapter maintainer is named.

The questions above remain useful if the project later seeks retailer confirmation. They do not block current internal use under the owner decision. The existing throttle remains at least 10 seconds between requests per retailer; this is an engineering default, not a retailer-confirmed rate. Do not describe this `OWNER_APPROVED` entry as `CLEAR` unless the external-policy evidence changes.

## DataBlitz (DBZ): `ecommerce.datablitz.com.ph`

- **Project decision: OWNER_APPROVED. Live use is enabled.** `DataBlitzAdapter.live_use_approved = True` under the project-wide owner policy recorded in [DECISIONS.md](DECISIONS.md).
- **External policy finding: UNKNOWN.** No DataBlitz website terms of use or data-use policy was found. robots.txt allows product pages, but that is not permission. The project decision assumes retailer approval for internal use; it is not evidence of retailer permission.
- **Host:** `ecommerce.datablitz.com.ph`, the Shopify storefront. `datablitz.com.ph` and `www.datablitz.com.ph` redirect to it (HTTP 301). Only the storefront host is allowlisted.
- **Date checked:** 2026-10-01.
- **Method:** a web search, then single manual GETs by Claude during implementation. They used a descriptive user agent, no cookies, and no login. Eleven requests in total:
  - each redirecting domain once;
  - robots.txt twice (status, then content);
  - the laptops collection page once, to find product URLs;
  - four candidate policy URLs;
  - two product pages.

  No search, pagination, cart, or checkout URL was requested during this research. A later user-reported project-scraper spot check is recorded in [PROGRESS_LOG.md](PROGRESS_LOG.md#datablitz-live-smoke-test-2026-10-01).

| Source | URL | Evidence |
|---|---|---|
| Apex and `www` domains | https://datablitz.com.ph/, https://www.datablitz.com.ph/ | Both returned HTTP 301 to `https://ecommerce.datablitz.com.ph/`. |
| robots.txt | https://ecommerce.datablitz.com.ph/robots.txt | Shopify's generated file. For `User-agent: *` it has `Allow: /`. It disallows admin, cart, checkout, orders, account, `/services`, `/sf_*`, `/cart.js`, `/recommendations/products`, and collection sort and filter crawl traps. It does not disallow `/products/` and has no `Crawl-delay`. Its comments, written by the platform, say checkout must not be completed automatically and suggest that agents use Shopify's UCP/MCP endpoints for catalog, cart, and checkout. They link the **Shopify** Terms of Service and `bots@shopify.com`, not DataBlitz terms. Lists sitemap `https://ecommerce.datablitz.com.ph/sitemap.xml`. |
| Shopify policy pages | https://ecommerce.datablitz.com.ph/policies/terms-of-service, https://ecommerce.datablitz.com.ph/policies/privacy-policy | Both returned HTTP 404. No published terms of service or privacy policy on the storefront. |
| Guessed terms page | https://ecommerce.datablitz.com.ph/pages/terms-and-conditions | HTTP 404. |
| FAQ (footer link) | https://ecommerce.datablitz.com.ph/pages/frequently-asked-questions | HTTP 200. The FAQ content is not in the static HTML, so it could not be reviewed without a browser. The footer links only Return and Exchange, Order Cancellation, Product Warranty, FAQs, Store Locations, and Contact Us. There is no terms-of-use or privacy link. |
| Collection page | https://ecommerce.datablitz.com.ph/collections/laptops | HTTP 200, public. Used only to pick the two product pages below. |
| Product pages | https://ecommerce.datablitz.com.ph/products/hp-15-fc0826au-laptop-natural-silver-15-6-fhd-1920x1080-ips-ryzen-5-7530u-16gb-ram-512gb-ssd-amd-radeon-graphics-windows-11-home-ms-office-home-2024-ms-365-basic-hp-everyday-16-laptop-briefcase and https://ecommerce.datablitz.com.ph/products/msi-modern-14s-g3mg-020ph-laptop-urban-silver-14-fhd-1920x1200-120hz-ips-core-5-320-16gb-ram-512gb-ssd-intel-graphics-windows-11-home-ms-office-home-2024 | Both returned HTTP 200 with no redirect. Public pages with no login. Used only to design fixtures. |

**Findings**

- No DataBlitz terms of use, acceptable-use policy, or data-use restriction was found, so there is neither a grant nor a prohibition of automated collection. The FAQ was not readable without JavaScript and may contain relevant text.
- robots.txt does not exclude product pages. That is a crawler-etiquette signal, not a grant of permission. The platform comments steer agents toward Shopify's UCP/MCP interfaces and forbid automated checkout. The project adapter reads public product HTML only and never touches cart or checkout.
- There is no published rate guidance.

**Unresolved external-policy questions**

1. Does DataBlitz publish website terms elsewhere, for example in the JavaScript-rendered FAQ, or will it grant written permission for low-volume automated price collection?
2. Which legal entity operates the storefront, and whom should a permission request go to (for example the Contact Us page)?
3. Should collection use the platform's suggested UCP/MCP catalog interface instead of product HTML? That would need a separate review and adapter.
4. What request rate and schedule are acceptable? The framework default is at least 10 seconds between requests per retailer.
5. Is internal price comparison the only intended use, with no republication of product text or images?
6. Who owns ongoing maintenance of the adapter? No adapter maintainer is named.

These questions do not block current internal use under the owner decision. Do not describe this `OWNER_APPROVED` entry as `CLEAR` unless the external-policy evidence changes.

## PC Express (PCX): `pcx.com.ph`

- **External policy finding: RESTRICTED.** The PC Express terms of service, published on the storefront, say: "You must not conduct any systematic or automated data collection activities (including without limitation scraping, data mining, data extraction, and data harvesting) on or in relation to this website without PC Express' express written consent." No written consent has been recorded.
- **Project status: OWNER_APPROVED. Live use is enabled.** `PCXAdapter.live_use_approved = True` and `DATA_USE_REVIEW_STATUS = "OWNER_APPROVED"`. After reviewing the PC Express terms, the project owner explicitly directed live use for this internal tool on 2026-10-01; see [DECISIONS.md](DECISIONS.md). This is a project authorization despite the published restriction. It is not PC Express consent, and the external-policy finding remains RESTRICTED. A later user-reported live check returned one in-stock Acer Aspire Lite offer; see [PROGRESS_LOG.md](PROGRESS_LOG.md#pcx-live-smoke-check-2026-10-01).
- **Host:** `pcx.com.ph`, the Shopify storefront ("PC Express" in the title, `og:site_name`, and footer). `www.pcx.com.ph` redirects to it (HTTP 301). Only the apex host is allowlisted.
- **Date checked:** 2026-10-01.
- **Method:** single manual GETs by Claude during implementation, at least 10 seconds apart. They used a descriptive user agent, no cookies, no login, and no redirect following. Seven requests in total:
  - the apex and `www` home pages, once each;
  - robots.txt;
  - the terms of service and privacy policy;
  - two product pages linked from the home page.

  No collection, search, pagination, cart, or checkout URL was requested, and the project scraper was not run. The two product pages were requested to design the parser, before the terms page was read in full.

| Source | URL | Evidence |
|---|---|---|
| Apex and `www` domains | https://pcx.com.ph/, https://www.pcx.com.ph/ | The apex returned HTTP 200 with the PC Express storefront. `www` returned HTTP 301 to `https://pcx.com.ph/`. |
| robots.txt | https://pcx.com.ph/robots.txt | Shopify's generated file. For `User-agent: *` it has `Allow: /`. It disallows admin, cart, checkout, orders, account, `/services`, `/sf_*`, `/cart.js`, `/recommendations/products`, and collection sort and filter crawl traps. It does not disallow `/products/` and has no `Crawl-delay`. Its comments, written by the platform, say checkout must not be completed automatically and that agents should use Shopify's UCP/MCP endpoints for catalog, cart, and checkout. They link the **Shopify** Terms of Service and `bots@shopify.com`. Sitemap: `https://pcx.com.ph/sitemap.xml`. |
| Terms of service | https://pcx.com.ph/policies/terms-of-service | HTTP 200. The terms govern use of the website. Its acceptable-use section prohibits systematic or automated data collection, "including without limitation scraping, data mining, data extraction, and data harvesting", without PC Express' express written consent. It also prohibits republishing, reproducing, or commercially exploiting site material, and using the site for marketing purposes without written consent. It says purchases on the site are for retail and personal use only. Philippine law and courts govern. |
| Privacy policy | https://pcx.com.ph/policies/privacy-policy | HTTP 200. It covers customers' personal data. It does not address automated collection of public product data. |
| Product pages | https://pcx.com.ph/products/hp-14-ep1144tu-c4fw3pa-intel-core-5-laptop-natural-silver and https://pcx.com.ph/products/asus-16-zenscreen-mb169ck-portable-usb-monitor-full-hd | Both returned HTTP 200 with no redirect. Public pages with no login. Used only to design minimal, sanitized fixtures; no page copy was kept in the repository. |

**Findings**

- The retailer's own terms expressly prohibit automated scraping and data extraction without written consent. That is a prohibition, not silence, so the finding is RESTRICTED, not UNKNOWN.
- robots.txt does not exclude product pages, but robots.txt is crawler etiquette and does not override the published terms.
- There is no published rate guidance.

**Open questions for the owner**

1. Will the project request written consent from PC Express for low-volume internal price collection? The contact page is linked from the storefront footer.
2. Should collection use the platform's suggested UCP/MCP catalog interface? That interface is still subject to the retailer's terms and would need a separate review and adapter.
3. What request rate and schedule are appropriate? The framework default is at least 10 seconds between requests per retailer.
4. Who owns ongoing maintenance of the adapter? No adapter maintainer is named.

The owner's PCX decision enables the project adapter while the external finding remains RESTRICTED. Do not describe the retailer's terms as permitting collection or mark this entry CLEAR without new evidence.
