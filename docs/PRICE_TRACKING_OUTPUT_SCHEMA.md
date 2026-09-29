# Planned Price Tracking Output Fields

**Status:** Requirements recorded from the user on 2026-09-29. The Phase 5 `TARGET_MASTER` and `MATCH_REVIEW` schemas now include the requested laptop specification fields. These downstream outputs are not implemented yet.

## Requested fields

| Group | Fields | Type hint supplied | Notes |
|---|---|---|---|
| Shopee listing identity | `Item_ID`, `Model_ID`, `Shop_ID` | `longint` | Source listing identifiers. |
| Shopee metrics | `Total_GMV`, `Current_Price` | Not specified | Preserve source values; units and currency still need to be documented. |
| Product identity | `Brand`, `Model` | `str` | `Model` is marked as a 1:1 matching field. |
| Display | `Screen Size`, `Resolution`, `Display Type`, `Refresh Rate` | Not specified | All four are marked as 1:1 matching fields. |
| CPU | `CPU_Brand`, `CPU_Series`, `CPU_Generation`, `CPU_Cores` | Not specified | `CPU_Series` is marked as a 1:1 matching field. |
| Memory | `RAM_Capacity`, `RAM_Type` | `RAM_Capacity`: `int`; `RAM_Type`: representation to confirm | `RAM_Capacity` is marked as a 1:1 matching field. The group annotation says `int`; confirm whether that also means a numeric code for `RAM_Type`. |
| Storage | `Storage_Capacity`, `Storage_Type` | `Storage_Capacity`: `int`; `Storage_Type`: representation to confirm | `Storage_Capacity` is marked as a 1:1 matching field. The group annotation says `int`; confirm whether that also means a numeric code for `Storage_Type`. |
| Graphics | `GPU Type`, `GPU_Brand`, `GPU_Model`, `VRAM` | Not specified | `GPU_Model` is marked as a 1:1 matching field. |
| Other product details | `OS`, `Battery_Capacity`, `Build_Material` | Not specified | `OS` is marked as a 1:1 matching field. |
| Retailer prices | `DBZ_Price`, `LF_Price`, `PCX_Price`, `PCC_Price`, `OCT_Price` | `float` | One price field per retailer code. Define retailer names and currency handling before implementation. |

## User-designated 1:1 matching fields

The user marked these fields in bold and specified that they must match 1:1. They are now the `TARGET_IDENTITY_COLUMNS` in `src/laptop_tracker/target_master.py`:

```text
Model
Screen Size
Resolution
Display Type
Refresh Rate
CPU_Series
RAM_Capacity
Storage_Capacity
GPU_Model
OS
```

The master column names are `model`, `screen_size_in`, `resolution`, `display_type`, `refresh_rate_hz`, `cpu_series`, `ram_capacity_gb`, `storage_capacity_gb`, `gpu_model`, and `os`. `Brand` is stored as `brand`, required for product context, but is not in the identity key because it was not marked bold.

Do not silently add fuzzy matching or tolerances to this requirement. Phase 8 must define how values are normalized and what happens when a required matching value is missing. A missing value must not be treated as an equal value.

## Relationship to the Phase 5 target schema

This is a requested downstream output schema, separate from the Phase 6 `OfferObservation` model. The Phase 5 target identity has been revised to use the 1:1 key above. `manufacturer_model_code` and `cpu_model` remain optional evidence fields; they no longer determine whether two target rows have the same identity. Keep all requested detail fields in the output even when they are not part of the 1:1 key.

Phase 9 must also define retailer-code names, currencies, units, numeric precision, and missing-price representation before generating comparison files. The retailer price fields are requested as floats; the Phase 6 scraper currently stores observed prices as `Decimal`, so conversion should happen only at a deliberately defined output boundary.
