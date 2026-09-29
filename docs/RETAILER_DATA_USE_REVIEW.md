# Retailer Data-Use Review

One entry per retailer. **CLEAR** means the retailer's own sources or written permission support the intended collection, with project-owner approval recorded in [DECISIONS.md](DECISIONS.md). **OWNER_APPROVED** records an explicit project-owner decision to proceed under the internal-tool assumption, while retailer permission remains unverified. **UNKNOWN** means neither external support nor a project-owner authorization has been recorded. The framework enforces the adapter-level approval with `live_use_approved`; see [SCRAPING_FRAMEWORK.md](SCRAPING_FRAMEWORK.md#live-use-gate).

| Result | Meaning |
|---|---|
| CLEAR | Official sources permit the intended automated collection, or the retailer granted written permission, and the owner approved it. |
| RESTRICTED | Official sources prohibit or limit the intended collection. Live use stays blocked unless permission is obtained. |
| OWNER_APPROVED | The project owner authorized internal collection under an explicit assumption, but retailer permission has not been verified. Live use is enabled by that project decision. |
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
