# Extraction and RECHECK fixes from the saved 2026-11-03 pages

Offline only: every case is a trimmed copy of a real saved retailer page under
`tests/scraping_fixtures/saved_2026-11-03/`, exercised by `tests/test_saved_page_regressions_2026_11_03.py`
(41 tests). `SOURCES.json` beside the fixtures records each page's URL and retrieval date (it was
rebuilt on 2026-11-04 because the original was never committed; the exact retrieval time and the
hash of the untrimmed evidence are not recoverable, see the note in the file). A test that needs a
variant of a page edits one field of the parsed observation, so an unsafe case is always the real
page with a single change. No retailer was contacted.

Policy kept: a close alternative has at most two *established* differences across model, screen,
CPU, RAM, storage and GPU; an unknown is never an agreement; a genuine page contradiction is never
corrected silently. Alternatives were re-evaluated on the corrected extractions
(`test_the_two_difference_budget_holds_on_the_corrected_extractions`): a rejection that exceeds the
budget stays a rejection.

| Case | Cause | Fix (code in `extraction.py` unless noted) | Not done on purpose |
|---|---|---|---|
| DataBlitz HP 15-fc0933AU: the laptop is 15.6", the bundled briefcase "HP Everyday 16" | The accessory's inch size entered the title screen claim | `_without_accessory_sizes`: a size followed within two ordinary words by an accessory noun (briefcase, bag, backpack, sleeve ...) is dropped from the claim; a display word in between ("15.6" FHD IPS display with a bag") keeps the size | A real 16" laptop sold with a bag keeps its 16 |
| PC Express HP 15-fc0933AU | Title writes the code with a space (`15 FC0933AU`), the page hyphenates it | `SPACED_CODE_SHAPES` (reviewed HP notebook shapes) with `TITLE_MODEL_SPACING_NORMALIZED`: the spaced code is read only where the page itself writes the hyphenated form (specification or retailer configuration line) | No brand without a reviewed shape; a URL slug alone never corroborates; two candidates make the title ambiguous, not chosen |
| Octagon Nitro V ANV15-52-572T | Model and display sit in a free-form "Tech Specification" section | The Acer row `Display Panel / Resolution` joins the display labels; the page has no `Model` row, so the model comes from the title, only when corroborated (`TITLE_MODEL_CORROBORATED`) and the title's code agrees with the page's evidence | The screen the offer states is kept even when the catalog differs; a target with another code does not match |
| PC Central Nitro V ANV15-52-585U: CPU row "Core? 5" | A trademark sign lost in transit | `CPU_DAMAGED_MARK_TITLE_CORROBORATED`: the row is read, with the mark dropped, only when the product title states the same series (`Intel Core 5`) | A damaged row with a title naming another or no series stays unread |
| PC Central Nitro: RAM variant | RAM and storage live in the selected option | The selected variant binds RAM and storage; the parent page never prices a variant it did not select (`SELECTED_VARIANT_NOT_CONFIRMED` otherwise) | An unconfirmed binding binds nothing |
| PC Central single-variant pages with named RAM/SSD options (ASUS X1504VA-BQ4301WSM, Acer AG14-71M-58JD) | They read as `MULTI_VARIANT_SHARED_DESCRIPTION` with no model, screen or CPU | `adapters/pcc.py` (pcc-7) sets `complete_sole_variant` only when the picker array, the option values and the JSON-LD offers all name one variant; the matrix then proves no other variant exists and the selected option still binds RAM/storage | A one-variant matrix without that proof proves nothing |
| MacBook Air "13-inch" | Titles say 13, Apple's specification says 13.6 | `MARKETED_SCREEN_CLASSES`: one manufacturer-backed entry (Apple, MacBook Air, chip Apple M5, 13 to 13.6). Requires the exact marketed class in the title, the exact diagonal in the specification, the brand, one registered product name and the chip. Provenance is `SCREEN_CLASS_NORMALIZED`; the catalog value is never edited | No generic tolerance: 15 is not 15.6, other chips and products keep the title/specification conflict |
| Multi-variant pages | A shared description cannot state a per-variant capacity | Invariant parent specifications support the bound child; capacities and prices come from the selected variant only | A changing capacity is never taken from the parent |
| Laptop Factory HP page | Title and SKU say 15-fc0933AU, the model table says 15-fc0993AU | Nothing: the conflict stays visible and unapproved (`test_laptop_factory_title_and_model_table_conflict_stays_visible_and_unapproved`) | No silent correction |

## Variant option axes (`variant_axes.py`, 2026-11-04)

A capacity written against a storage word without a space ("512gbSSD") was not read as a
capacity (`GB` was followed by no word boundary), so the PC Central MacBook Neo page's
"Storage: 256GB & 512gbSSD" listed only 256GB and every colour/capacity variant went to review.
`_CAPACITY` now accepts a unit glued to `ssd`, `sdd`, `hdd`, `nvme` or `emmc`; `GBps` and `GB/s`
are still not capacities. Three tests in `tests/test_variant_axes.py` had been failing because of
this and because two of them edited strings that no longer matched the saved page
(non-breaking spaces); they now edit every copy of the statement.

## Exact processor number is the default (tests aligned 2026-11-04)

`matching.py` has made a verified processor-number disagreement (`Core 5 220H` against `210H`) a
blocking secondary conflict by default since 2026-10-22 (`STRICT_CPU_MODEL`; the earlier warning
policy is only reachable through `LEGACY_CPU_MODEL_WARNING`). Eleven tests still asserted the
warning policy (`test_listed_spec.py` 9, `test_verification_safety.py` 2) and had been counted as an
"unchanged baseline". They were stale, not the code: `test_exact_cpu.py` pins the current rule,
including that a broad-family listed spec with a contradicted exact processor is a
`SECONDARY_CONFLICT`. The listed-spec fixtures now record the processor the offers state, and the
two verification tests exercise the current default and the explicit legacy override.
