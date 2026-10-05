# Planned Price Tracking Output Fields

**Status:** Output fields were recorded on 2026-09-29. On 2026-10-05, the user changed the planned matching priority to five core fields plus lower-priority supporting specifications. This supersedes the earlier ten-field exact-key requirement below. Comparison outputs are not implemented yet; the local target validator and templates still need synchronization.

## Requested fields

| Group | Fields | Type hint supplied | Notes |
|---|---|---|---|
| Shopee listing identity | `Item_ID`, `Model_ID`, `Shop_ID` | `longint` | Source listing identifiers. |
| Shopee metrics | `Total_GMV`, `Current_Price` | Not specified | Preserve source values; units and currency still need to be documented. |
| Product identity | `Brand`, `Model` | `str` | `Model` is a core matching field; `Brand` is useful context. |
| Display | `Screen Size`, `Resolution`, `Display Type`, `Refresh Rate` | Not specified | `Screen Size` is core. Resolution, display type, and refresh rate are lower-priority supporting fields. |
| CPU | `CPU_Brand`, `CPU_Series`, `CPU_Generation`, `CPU_Cores` | Not specified | `CPU_Series` is core. Other CPU details are supporting fields. |
| Memory | `RAM_Capacity`, `RAM_Type` | `RAM_Capacity`: `int`; `RAM_Type`: representation to confirm | `RAM_Capacity` is core; RAM type is supporting detail. |
| Storage | `Storage_Capacity`, `Storage_Type` | `Storage_Capacity`: `int`; `Storage_Type`: representation to confirm | `Storage_Capacity` is core; storage type is supporting detail. |
| Graphics | `GPU Type`, `GPU_Brand`, `GPU_Model`, `VRAM` | Not specified | GPU details are lower-priority supporting fields. |
| Other product details | `OS`, `Battery_Capacity`, `Build_Material` | Not specified | OS, battery capacity, and build material are supporting details. |
| Retailer prices | `DBZ_Price`, `LF_Price`, `PCX_Price`, `PCC_Price`, `OCT_Price` | `float` | One price field per retailer code. Define retailer names and currency handling before implementation. |

## Matching priority approved 2026-10-05

The five core fields are:

```text
Model
Screen Size
CPU_Series
RAM_Capacity
Storage_Capacity
```

The master column names are `model`, `screen_size_in`, `cpu_series`, `ram_capacity_gb`, and `storage_capacity_gb`. A candidate is core-complete when all five fields have explicit, unambiguous values. The user's rule authorizes promotion of such candidates to `TARGET_MASTER`; missing `resolution`, `display_type`, `refresh_rate_hz`, `gpu_model`, `os`, or `source_url` does not block promotion. Keep those details when known. `brand` remains useful context but is not part of the five-field core key.

Phase 8 matching must first require agreement on the five core fields after documented normalization. A missing core value is not equal to another missing value and cannot form a core match. Available secondary fields can strengthen confidence. Missing secondary values do not block a core match. Explicit secondary conflicts must be surfaced as lower-confidence/review cases rather than silently overwritten or treated as proof of a different core identity. Do not introduce fuzzy matching for the five core fields.

## Relationship to the Phase 5 target schema

This is a requested downstream output schema, separate from the Phase 6 `OfferObservation` model. The planned Phase 5 target identity uses the five-field core key above. `manufacturer_model_code` and `cpu_model` remain optional evidence fields; they do not determine whether two target rows share a core identity. Keep all requested detail fields in outputs even when they are not part of the core key.

**Implementation gap:** `src/laptop_tracker/target_master.py`, the local templates, and the current `MATCH_REVIEW` validator still use the earlier ten-field key and do not accept the new `COMPLETE` state. They must be updated before local validation or automated matching can enforce this policy.

Phase 9 must also define retailer-code names, currencies, units, numeric precision, and missing-price representation before generating comparison files. The retailer price fields are requested as floats; the Phase 6 scraper currently stores observed prices as `Decimal`, so conversion should happen only at a deliberately defined output boundary.
