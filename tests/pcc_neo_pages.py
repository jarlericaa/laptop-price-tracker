"""PC Central MacBook Neo page, as the retailer serves it for each ``?variant=`` (offline).

``scraping_fixtures/pcc_neo/pcc_neo_variants.html`` is the saved 2026-10-22 page of the Neo
product (six colour/capacity variants in ONE option named after the product, no ``SSD`` or
``RAM`` words in any option), trimmed of scripts, styles and navigation. It was saved with the
first variant selected. The retailer renders the selected variant server-side (checked radio,
selected ``<option>``, hidden form ID, price block, button), so ``page_for`` re-renders those
parts for another variant of the same saved page. Prices, availability and option titles are
read from the fixture's own variant JSON; nothing here is a hand-typed price.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

HOST = "pccentralcomputerstore.com"
HANDLE = "apple-macbook-neo-apple-a18-pro-chip-13-retina-ips-display-8gb-unified-memory-touch-id-pc-central"
PRODUCT_URL = f"https://{HOST}/products/{HANDLE}"
FIXTURE = Path(__file__).resolve().parent / "scraping_fixtures" / "pcc_neo" / "pcc_neo_variants.html"


def saved_page() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def variant_rows(text: str) -> list[dict]:
    """The picker's own variant JSON (id, title, price in cents, availability)."""
    match = re.search(r'<variant-radios.*?<script type="application/json">\s*(\[.*?\])\s*</script>',
                      text, re.S)
    assert match, "the fixture has no variant picker JSON"
    return json.loads(match[1])


def peso(cents: int) -> str:
    return f"₱{cents / 100:,.2f}"


def page_for(variant_id: str, text: str | None = None) -> str:
    """The saved page with ``variant_id`` selected the way Dawn renders it server-side."""
    text = saved_page() if text is None else text
    rows = {str(row["id"]): row for row in variant_rows(text)}
    chosen, first = rows[variant_id], next(iter(rows.values()))

    def radio(match: re.Match) -> str:
        tag = re.sub(r"\n\s*checked\b", "", match[0])
        return tag.replace(">", " checked>", 1) if f'value="{chosen["title"]}"' in tag else tag

    text = re.sub(r'<input\s+type="radio".*?>', radio, text, flags=re.S)
    text = text.replace('selected="selected"', "")
    text = re.sub(rf'(<option\s*)(\s*value="{variant_id}")', r'\1 selected="selected" \2', text)
    text = re.sub(rf'(name="id"\s+value="){first["id"]}(")', rf"\g<1>{variant_id}\2", text)
    text = text.replace(f'data-variant-id="{first["id"]}"', f'data-variant-id="{variant_id}"')
    old_price, new_price = peso(first["price"]), peso(chosen["price"])
    start = text.index('id="price-template')
    end = text.index("</div>\n</div>", start)
    block = text[start:end].replace(old_price, new_price)
    if not chosen["available"]:
        block = block.replace("price price--large price--show-badge",
                              "price price--large price--show-badge price--sold-out")
    text = text[:start] + block + text[end:]
    if not chosen["available"]:
        text = text.replace("<span>Add to cart\n</span>", "<span>Sold out\n</span>")
        text = text.replace('name="add"\n', 'name="add" disabled\n')
    return text
