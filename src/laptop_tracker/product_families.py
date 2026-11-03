"""Controlled product-family recognition for close-alternative comparisons (2026-10-27).

A close alternative is restricted to the same brand and a *recognized product family*. Family
membership is never inferred from a shared vague word: ``config/product_family_registry.csv``
lists each reviewed family with its approved naming variations, and a text belongs to a family only
when one of those names appears as whole words in order (case, spacing and punctuation are
presentation; a joined spelling such as ``VivoBook`` for ``Vivo Book`` is the same name, as in
``listed_spec``). The brand must agree as well: a name registered for one brand is never read
for another.

The most specific name wins: ``Aspire Lite`` names the ``Aspire Lite`` family although ``Aspire``
is also a registered (broader) family. A broader family lists the families it ``includes``
(``Aspire`` covers ``Aspire Go`` and ``Aspire Lite``, as the product-name registry records), so a
target recorded under the broad name accepts offers of its lines, and never the reverse.

Two families are the same only when they are equal, or the target's family includes the offer's.
MacBook Air and MacBook Pro are different families although both are Apple laptops. An
unrecognized text has no family, and a text naming two unrelated families of equal specificity is
ambiguous: neither is chosen.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .conventions import product_name_tokens
from .listed_spec import _contains

REGISTRY_PATH = Path(__file__).resolve().parents[2] / "config" / "product_family_registry.csv"
REGISTRY_COLUMNS = ("brand", "family", "names", "includes", "evidence", "reviewed_on")


class FamilyRegistryError(ValueError):
    """The family registry file is malformed."""


@dataclass(frozen=True)
class Family:
    brand: str
    family: str
    names: tuple[str, ...]
    includes: tuple[str, ...]
    evidence: str

    @property
    def key(self) -> tuple[tuple[str, ...], str]:
        return tuple(product_name_tokens(self.brand)), self.family.casefold()

    @property
    def label(self) -> str:
        return f"{self.brand} {self.family}"


@dataclass(frozen=True)
class FamilyRecognition:
    """The family a text names: ``family`` is set only when exactly one most-specific family
    matched; ``candidates`` lists every tied family when the text is ambiguous."""

    family: Family | None
    candidates: tuple[Family, ...] = ()

    @property
    def ambiguous(self) -> bool:
        return self.family is None and len(self.candidates) > 1


class FamilyRegistry:
    def __init__(self, families: Iterable[Family] = ()) -> None:
        self.families = tuple(families)
        self._by_brand: dict[tuple[str, ...], list[Family]] = {}
        for family in self.families:
            self._by_brand.setdefault(family.key[0], []).append(family)

    @classmethod
    def read(cls, path: Path = REGISTRY_PATH) -> FamilyRegistry:
        if not path.is_file():
            return cls(())
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            missing = [c for c in REGISTRY_COLUMNS if c not in (reader.fieldnames or [])]
            if missing:
                raise FamilyRegistryError(f"{path} lacks column(s): {', '.join(missing)}")
            families = []
            seen: set[tuple[tuple[str, ...], str]] = set()
            for number, row in enumerate(reader, start=2):
                brand, name = row["brand"].strip(), row["family"].strip()
                names = tuple(n.strip() for n in row["names"].split(";") if n.strip())
                if not brand or not name or not names or not row["evidence"].strip():
                    raise FamilyRegistryError(
                        f"{path} row {number}: brand, family, names and evidence are required")
                family = Family(
                    brand, name, names,
                    tuple(n.strip() for n in row["includes"].split(";") if n.strip()),
                    row["evidence"].strip(),
                )
                if family.key in seen:
                    raise FamilyRegistryError(f"{path} row {number}: repeats {family.label}")
                seen.add(family.key)
                families.append(family)
        known = {(f.key[0], f.key[1]) for f in families}
        for family in families:
            for included in family.includes:
                if (family.key[0], included.casefold()) not in known:
                    raise FamilyRegistryError(
                        f"{family.label} includes the unregistered family {included!r}")
        return cls(families)

    def find(self, brand: str, family: str) -> Family | None:
        key = (tuple(product_name_tokens(brand)), family.casefold())
        return next((f for f in self.families if f.key == key), None)

    def recognize(self, brand: str, *texts: str) -> FamilyRecognition:
        """The most specific registered family of ``brand`` named in any of ``texts``."""
        families = self._by_brand.get(tuple(product_name_tokens(brand)), ())
        if not families:
            return FamilyRecognition(None)
        token_lists = [product_name_tokens(text) for text in texts if text]
        scored: list[tuple[int, Family]] = []
        for family in families:
            best = max(
                (len(wanted) for name in family.names
                 if (wanted := product_name_tokens(name))
                 and any(_contains(words, wanted) for words in token_lists)),
                default=0,
            )
            if best:
                scored.append((best, family))
        if not scored:
            return FamilyRecognition(None)
        top = max(score for score, _ in scored)
        tied = tuple(f for score, f in scored if score == top)
        # Several families can match with equal specificity only when one includes the other's
        # name (never the case for the reviewed list) or the text names two lines at once.
        return FamilyRecognition(tied[0] if len(tied) == 1 else None, tied)

    def same_family(self, target: Family, offer: Family) -> bool:
        """The offer's family is the target's own, or one the target's broad family includes."""
        if target.key == offer.key:
            return True
        return offer.family.casefold() in {n.casefold() for n in target.includes} and (
            target.key[0] == offer.key[0])


@lru_cache(maxsize=1)
def default_family_registry() -> FamilyRegistry:
    return FamilyRegistry.read(REGISTRY_PATH)
