"""Offline example adapter for the synthetic fixtures in tests/scraping_fixtures/.

It only demonstrates the shared adapter contract; it is not a retailer integration. The host
``shop.example.test`` uses the reserved ``.test`` top-level domain (RFC 2606), so it can never
reach a real site.
"""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

from laptop_tracker.offers import Availability
from laptop_tracker.scraping import FetchedPage, ParsedOffer, ParseError

FIXTURES = Path(__file__).resolve().parent / "scraping_fixtures"
EXAMPLE_HOST = "shop.example.test"
EXAMPLE_URL = f"https://{EXAMPLE_HOST}/laptops"

_STOCK_WORDING = {
    "in stock": Availability.IN_STOCK,
    "out of stock": Availability.OUT_OF_STOCK,
    "pre-order": Availability.PREORDER,
}
_FIELDS = {"title", "config", "price", "stock"}


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class ExampleShopAdapter:
    retailer_id = "example_shop"
    adapter_version = "example-1"
    allowed_hosts = frozenset({EXAMPLE_HOST})

    def parse(self, page: FetchedPage) -> list[ParsedOffer]:
        parser = _OfferParser()
        parser.feed(page.text())
        parser.close()
        if not parser.offers:
            raise ParseError("NO_OFFERS_FOUND", "No offer elements found; layout may have changed.")
        offers = []
        for fields in parser.offers:
            stock = fields.get("stock", "")
            offers.append(
                ParsedOffer(
                    raw_title=fields.get("title", ""),
                    raw_configuration_text=fields.get("config", ""),
                    price_text=fields.get("price", ""),
                    currency="PHP",
                    availability=_STOCK_WORDING.get(stock.strip().lower(), Availability.UNKNOWN),
                    availability_evidence=stock,
                    offer_url=fields.get("href", ""),
                )
            )
        return offers


class _OfferParser(HTMLParser):
    """Collects ``<article class="offer">`` blocks and their classed child elements' text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.offers: list[dict[str, str]] = []
        self._offer: dict[str, str] | None = None
        self._field: tuple[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name: value or "" for name, value in attrs}
        css_class = attributes.get("class", "")
        if tag == "article" and css_class == "offer":
            self._offer = {"href": attributes.get("data-href", "")}
        elif self._offer is not None and css_class in _FIELDS:
            self._field = (tag, css_class)

    def handle_endtag(self, tag: str) -> None:
        if self._field is not None and tag == self._field[0]:
            self._field = None
        elif tag == "article" and self._offer is not None:
            self.offers.append(self._offer)
            self._offer = None

    def handle_data(self, data: str) -> None:
        if self._offer is not None and self._field is not None:
            name = self._field[1]
            self._offer[name] = self._offer.get(name, "") + data
