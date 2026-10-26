"""Reviewed product-name registry (2026-10-19, second review).

Most catalog ``model`` values are manufacturer codes. Some are names, and a name is one of two
different things:

- a **product name** that, with the four other core fields, identifies one product: Apple sells
  ``MacBook Air``/``MacBook Pro``/``MacBook Neo`` configurations by name, size, chip, memory and
  storage, and no Apple model code appears in source listings or retailer titles;
- a **family name** (``IdeaPad``, ``Aspire``, ``Victus``) under which several distinct coded
  products share the same five fields, so the name cannot establish one product. Such targets
  need catalog reconciliation (``catalog_reconciliation.py``), never an alias.

``config/product_name_registry.csv`` records each reviewed name with its kind, the product's
colours where a product name is sold in several colours, and the evidence. A name that is not
listed is treated as a family name: unreviewed names are never searched or matched as if they
identified one product.

The registry supports two narrow, presentation-only rules in extraction:

- ``normalize_model_name``: a page's model-name wording that is a registered product name plus a
  repeated brand and/or a registered colour in parentheses (``Apple Macbook Neo (Citrus)``) is
  that product name. Anything else in the wording (a size, generation, suffix) leaves it
  unchanged, so it is compared, and differs, as written.
- ``title_product_names``: the registered product names stated in a title, for title
  corroboration under the same four-independent-fields rule as title model codes.

Nothing here is a mapping between names and codes, and no catalog value is ever copied into an
offer.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .conventions import (
    NormalizedName,
    model_codes,
    normalize_product_name,
    product_name_tokens,
    strip_apple_part_numbers,
)

REGISTRY_PATH = Path(__file__).resolve().parents[2] / "config" / "product_name_registry.csv"
REGISTRY_COLUMNS = ("brand", "name", "identity_kind", "colours", "evidence", "reviewed_on")
PRODUCT_NAME = "PRODUCT_NAME"
FAMILY_NAME = "FAMILY_NAME"
MODEL_CODE = "MODEL_CODE"


class RegistryError(ValueError):
    """The registry file is malformed."""


@dataclass(frozen=True)
class RegisteredName:
    brand: str
    name: str
    identity_kind: str
    colours: tuple[str, ...]
    evidence: str

    @property
    def tokens(self) -> list[str]:
        return product_name_tokens(self.name)


@dataclass(frozen=True)
class ProductNameRegistry:
    entries: tuple[RegisteredName, ...]

    @classmethod
    def read(cls, path: Path = REGISTRY_PATH) -> ProductNameRegistry:
        if not path.is_file():
            return cls(())
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            missing = [c for c in REGISTRY_COLUMNS if c not in (reader.fieldnames or [])]
            if missing:
                raise RegistryError(f"{path} lacks column(s): {', '.join(missing)}")
            entries = []
            for number, row in enumerate(reader, start=2):
                kind = row["identity_kind"].strip()
                if kind not in {PRODUCT_NAME, FAMILY_NAME}:
                    raise RegistryError(f"{path} row {number}: unknown identity_kind {kind!r}")
                if not row["name"].strip() or not row["evidence"].strip():
                    raise RegistryError(f"{path} row {number}: name and evidence are required")
                entries.append(RegisteredName(
                    row["brand"].strip(), row["name"].strip(), kind,
                    tuple(c.strip() for c in row["colours"].split(";") if c.strip()),
                    row["evidence"].strip(),
                ))
        return cls(tuple(entries))

    def find(self, brand: str, name: str) -> RegisteredName | None:
        wanted = product_name_tokens(name)
        for entry in self.entries:
            if entry.tokens == wanted and (
                not brand or not entry.brand
                or product_name_tokens(brand) == product_name_tokens(entry.brand)
            ):
                return entry
        return None

    def kind(self, brand: str, model: str) -> str:
        """``MODEL_CODE`` for one manufacturer code; the registered kind of a name; otherwise
        ``FAMILY_NAME`` (an unreviewed name never identifies one product)."""
        text = model.strip()
        if text and [c.casefold() for c in model_codes(text)] == [text.casefold()]:
            return MODEL_CODE
        entry = self.find(brand, text)
        return entry.identity_kind if entry else FAMILY_NAME

    def product_names(self) -> Iterable[RegisteredName]:
        return (entry for entry in self.entries if entry.identity_kind == PRODUCT_NAME)

    def normalize_model_name(self, text: str) -> NormalizedName | None:
        """The registered product name a model-name wording states, with only a repeated brand,
        a registered colour in parentheses and an Apple part number (``MacBook Neo (MHFC4PP/A)``,
        2026-10-22) removed; ``None`` when the wording is anything else (it is then compared as
        written). The part numbers are returned beside the name, never dropped."""
        stripped, parts = strip_apple_part_numbers(text)
        tokens = product_name_tokens(stripped)
        for entry in self.product_names():
            name_tokens = entry.tokens
            if not _contains(tokens, name_tokens):
                continue
            colour = next((c for c in entry.colours
                           if _contains(tokens, product_name_tokens(c))), "")
            result = normalize_product_name(stripped, brand=entry.brand, color=colour)
            if product_name_tokens(result.value) == name_tokens:
                removed = (*result.removed, *(f"Apple part number ({part})" for part in parts))
                return NormalizedName(text, entry.name, removed, parts)
        return None

    def title_product_names(self, title: str) -> list[RegisteredName]:
        """Registered product names stated as whole words in a title."""
        tokens = product_name_tokens(title)
        return [entry for entry in self.product_names() if _contains(tokens, entry.tokens)]


def _contains(tokens: list[str], wanted: list[str]) -> bool:
    return bool(wanted) and any(
        tokens[start:start + len(wanted)] == wanted
        for start in range(len(tokens) - len(wanted) + 1)
    )


@lru_cache(maxsize=1)
def default_registry() -> ProductNameRegistry:
    return ProductNameRegistry.read(REGISTRY_PATH)
