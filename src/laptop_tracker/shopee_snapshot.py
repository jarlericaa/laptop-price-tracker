"""Shopee snapshot prices for the weekly comparison (2026-10-20).

The comparison shows, beside each configuration, the **lowest eligible mapped-listing snapshot
price**: the lowest valid positive PHP price among the configuration's contributing Shopee
listings that are still mapped to the target, as recorded in the sales extract the selected list
was built from (``configuration_source_ledger.csv``; the price is ``shopee_current_price_php`` of
the eligible-model ranking). It is a historical snapshot dated by the extract, never a current
price, never stock-verified, and never part of a retailer minimum.

Selection rule:

- **Eligible listing**: a contributing source of the configuration (the selected list's
  contribution ledger) whose ``SOURCE_MODEL_MAP`` row still maps it to the target
  (``currently_mapped_to_target`` is ``True``).
- **Valid price**: price status ``PRICE_OK`` and a finite Decimal amount greater than zero. A
  ``PRICE_CONFLICT`` listing (several month-to-date prices in the extract), a blank or
  non-positive amount, or a missing sales ranking is never used, guessed or averaged.
- **Choice**: the lowest valid price. Equal lowest prices are a tie: the listing with the higher
  ranking GMV (USD), then the lower ``model_id`` (numeric), is shown; every tied listing is
  recorded (``tied_model_ids``) and every listing keeps its own price and role in the ledger.
- **Link**: ``https://shopee.ph/product/<shop_id>/<item_id>`` from the listing's verified numeric
  identifiers. It opens the product page; it does **not** select the variation, which is shown
  separately with its ``model_id``.

Configuration statuses: ``SNAPSHOT_PRICE`` (a price was selected), ``NO_MAPPED_SOURCE`` (no
contributing listing is still mapped), ``NO_VALID_SNAPSHOT_PRICE`` (eligible listings exist but
none has a valid price; the detail counts each reason), ``SALES_RANKING_NOT_SUPPLIED``.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

SNAPSHOT_PRICE = "SNAPSHOT_PRICE"
NO_MAPPED_SOURCE = "NO_MAPPED_SOURCE"
NO_VALID_SNAPSHOT_PRICE = "NO_VALID_SNAPSHOT_PRICE"
SALES_RANKING_NOT_SUPPLIED = "SALES_RANKING_NOT_SUPPLIED"
SELECTION_RULE = (
    "Lowest eligible mapped-listing snapshot price: the lowest valid positive PHP snapshot "
    "price among the configuration's contributing Shopee listings still mapped to the target "
    "(price status PRICE_OK); ties keep the higher-GMV listing, then the lower model_id."
)

# Roles of each ledger row in the selection.
ROLE_SELECTED = "SELECTED"
ROLE_TIED = "TIED_NOT_SHOWN"
ROLE_HIGHER = "HIGHER_PRICE"
ROLE_NOT_MAPPED = "NOT_ELIGIBLE: NOT_CURRENTLY_MAPPED"

SELECTION_COLUMNS: tuple[str, ...] = (
    "rank", "target_id", "shopee_price_status", "shopee_price_php_snapshot",
    "shopee_listing_url", "shopee_variation", "shopee_item_name", "model_id", "shop_id",
    "item_id", "eligible_sources", "priced_sources", "distinct_prices", "tied_model_ids",
    "excluded_sources", "price_detail", "sales_run_id", "sales_extract_date", "sales_period_label",
    "selection_rule",
)
_DIGITS = re.compile(r"[0-9]+")


def listing_url(shop_id: str, item_id: str) -> str:
    """The Shopee product page of a listing, from its verified numeric identifiers; '' when
    either identifier is not a plain number (no URL is guessed)."""
    shop, item = str(shop_id).strip(), str(item_id).strip()
    if _DIGITS.fullmatch(shop) and _DIGITS.fullmatch(item):
        return f"https://shopee.ph/product/{shop}/{item}"
    return ""


def price_text(amount: Decimal) -> str:
    """A snapshot amount as PHP text with two decimals (89990.0 -> 89990.00); an amount
    with more decimals is kept exactly, never rounded."""
    cents = amount.quantize(Decimal("0.01"))
    return format(cents if cents == amount else amount, "f")


def snapshot_amount(row: Mapping[str, str]) -> tuple[Decimal | None, str]:
    """``(amount, reason)``: a valid price and '' or None and why it is not usable."""
    status = row.get("shopee_price_status", "")
    if status == SALES_RANKING_NOT_SUPPLIED:
        return None, SALES_RANKING_NOT_SUPPLIED
    if status != "PRICE_OK":
        return None, status or "PRICE_STATUS_MISSING"
    text = str(row.get("shopee_price_php_snapshot", "") or "").strip()
    if not text:
        return None, "PRICE_MISSING"
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return None, "PRICE_INVALID"
    if not amount.is_finite() or amount <= 0:
        return None, "PRICE_INVALID"
    return amount, ""


@dataclass
class ShopeeSelection:
    """The Shopee snapshot price of one configuration, tied to one source row."""

    target_id: str
    status: str
    source: Mapping[str, str] | None = None
    amount: Decimal | None = None
    eligible: int = 0
    priced: int = 0
    distinct_prices: int = 0
    tied_model_ids: tuple[str, ...] = ()
    excluded: Counter = field(default_factory=Counter)

    @property
    def url(self) -> str:
        return listing_url(self.source["shop_id"], self.source["item_id"]) if self.source else ""

    def detail(self) -> str:
        """A short explanation of the selection or of the blank."""
        reasons = "; ".join(f"{reason} x{count}" for reason, count in sorted(self.excluded.items()))
        if self.status == SNAPSHOT_PRICE:
            parts = [f"lowest of {self.priced} priced eligible listing(s)"
                     + (f" ({self.distinct_prices} distinct prices)"
                        if self.distinct_prices > 1 else "")]
            if len(self.tied_model_ids) > 1:
                parts.append("tie at this price: model_id " + ", ".join(self.tied_model_ids))
            if reasons:
                parts.append("not used: " + reasons)
            return "; ".join(parts)
        if self.status == NO_VALID_SNAPSHOT_PRICE:
            return f"{self.eligible} eligible listing(s), none with a valid price: {reasons}"
        if self.status == NO_MAPPED_SOURCE:
            return "no contributing listing is still mapped to this target" + (
                f" ({reasons})" if reasons else "")
        return "the sales ranking was not supplied; no snapshot price is known"


def _model_number(model_id: str) -> tuple[int, str]:
    return (int(model_id), model_id) if _DIGITS.fullmatch(model_id) else (10**30, model_id)


def select_shopee_prices(
    ledger: Sequence[dict[str, str]],
) -> tuple[dict[str, ShopeeSelection], list[dict[str, str]]]:
    """``(target_id -> selection, ledger rows with shopee_price_role and shopee_listing_url)``.

    Every ledger row is kept; only the role columns are added (see the module docstring).
    """
    by_target: dict[str, list[dict[str, str]]] = {}
    for row in ledger:
        by_target.setdefault(row["target_id"], []).append(row)
    selections: dict[str, ShopeeSelection] = {}
    annotated: list[dict[str, str]] = []
    roles: dict[int, str] = {}
    for target_id, rows in by_target.items():
        excluded: Counter = Counter()
        candidates: list[tuple[Decimal, dict[str, str]]] = []
        eligible = 0
        for row in rows:
            if row.get("currently_mapped_to_target") != "True":
                roles[id(row)] = ROLE_NOT_MAPPED
                excluded["NOT_CURRENTLY_MAPPED"] += 1
                continue
            eligible += 1
            amount, reason = snapshot_amount(row)
            if amount is None:
                roles[id(row)] = f"NOT_ELIGIBLE: {reason}"
                excluded[reason] += 1
                continue
            candidates.append((amount, row))
        if not eligible:
            status = NO_MAPPED_SOURCE
        elif not candidates:
            status = SALES_RANKING_NOT_SUPPLIED if set(excluded) == {
                SALES_RANKING_NOT_SUPPLIED} else NO_VALID_SNAPSHOT_PRICE
        else:
            status = SNAPSHOT_PRICE
        selection = ShopeeSelection(target_id, status, eligible=eligible,
                                    priced=len(candidates), excluded=excluded)
        if candidates:
            lowest = min(amount for amount, _ in candidates)
            tied = [row for amount, row in candidates if amount == lowest]
            tied.sort(key=lambda row: (-_gmv(row), _model_number(row["model_id"])))
            chosen = tied[0]
            selection.source, selection.amount = chosen, lowest
            selection.distinct_prices = len({amount for amount, _ in candidates})
            selection.tied_model_ids = tuple(row["model_id"] for row in tied)
            for amount, row in candidates:
                roles[id(row)] = (ROLE_SELECTED if row is chosen else ROLE_TIED
                                  if amount == lowest else ROLE_HIGHER)
        selections[target_id] = selection
    for row in ledger:
        annotated.append(row | {
            "shopee_price_role": roles.get(id(row), ""),
            "shopee_listing_url": listing_url(row.get("shop_id", ""), row.get("item_id", "")),
        })
    return selections, annotated


def _gmv(row: Mapping[str, str]) -> Decimal:
    try:
        return Decimal(str(row.get("ranking_gmv_usd", "") or "0"))
    except InvalidOperation:
        return Decimal(0)


def selection_row(rank: str, selection: ShopeeSelection,
                  sales: Mapping[str, str]) -> dict[str, str]:
    """The supporting ``shopee_price_selection.csv`` row of one configuration."""
    source = selection.source or {}
    row = {
        "rank": rank,
        "target_id": selection.target_id,
        "shopee_price_status": selection.status,
        "shopee_price_php_snapshot": price_text(selection.amount)
        if selection.amount is not None else "",
        "shopee_listing_url": selection.url,
        "shopee_variation": source.get("shopee_model_name", ""),
        "shopee_item_name": source.get("item_name", ""),
        "model_id": source.get("model_id", ""),
        "shop_id": source.get("shop_id", ""),
        "item_id": source.get("item_id", ""),
        "eligible_sources": str(selection.eligible),
        "priced_sources": str(selection.priced),
        "distinct_prices": str(selection.distinct_prices),
        "tied_model_ids": ";".join(selection.tied_model_ids),
        "excluded_sources": ";".join(f"{k}:{v}" for k, v in sorted(selection.excluded.items())),
        "price_detail": selection.detail(),
        "sales_run_id": sales.get("sales_run_id", ""),
        "sales_extract_date": sales.get("sales_extract_date", ""),
        "sales_period_label": sales.get("sales_period_label", ""),
        "selection_rule": SELECTION_RULE,
    }
    return {column: row[column] for column in SELECTION_COLUMNS}
