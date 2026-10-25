from __future__ import annotations

import json
from argparse import Namespace

import pytest
from test_catalog_leads import lf_routes, run_cli
from test_product_discovery import discover

from laptop_tracker.catalog_index import CatalogIndex, DiscoveryError, parse_product_feed
from laptop_tracker.scraping import TransportResponse

FEED = "https://laptopfactory.com.ph/wp-json/wc/store/v1/products?per_page=100&page=1&orderby=id&order=asc"
URL = "https://laptopfactory.com.ph/product/hp-laptop-15-example/"


def product(url=URL):
    return {"name": "HP Laptop 15 Ryzen 5 16GB 512GB SSD", "permalink": url,
            "sku": "EXAMPLE-15", "description": "<p>16GB installed RAM, 512GB SSD.</p>",
            "prices": {"price": "19999999"}, "is_in_stock": True}


def test_public_feed_retains_discovery_metadata_without_importing_prices():
    entries = parse_product_feed(json.dumps([product()]).encode(), "laptop_factory")
    assert entries[0]["url"] == URL
    assert "16GB installed RAM" in entries[0]["search_text"]
    assert "prices" not in entries[0] and "19999999" not in str(entries)
    with pytest.raises(DiscoveryError):
        parse_product_feed(json.dumps([product("https://evil.invalid/product/x/")]).encode(),
                           "laptop_factory")


def test_collector_caches_feed_for_local_discovery_within_shared_budget(tmp_path):
    routes = lf_routes()
    routes[FEED] = TransportResponse(200, FEED, json.dumps([product()]).encode(),
                                    {"content-type": "application/json"})
    code, transport, folder = run_cli(tmp_path, routes, "--include-product-feed")
    assert code == 0 and len(transport.calls) == 4
    index = CatalogIndex.from_run(folder)
    response = index.search('site:laptopfactory.com.ph "HP Laptop 15"', clues="16GB 512GB")
    assert response.results[0]["url"] == URL and response.search_calls == 0
    summary = json.loads((folder / "catalog_summary.json").read_text())
    assert summary["retailers"]["laptop_factory"]["feed_complete"] is True
    args = Namespace(supplied_urls=[], no_supplied_urls=True, catalog_index_run=folder,
                     catalog_feed_run=[])
    target = discover.SearchTarget("TGT-EXAMPLE", "1", "HP", "Laptop 15", "HP Laptop 15", "",
                                   {"cpu_series": "Ryzen 5", "ram_capacity_gb": "16",
                                    "storage_capacity_gb": "512"})
    supplied, report = discover.load_supplied(args, [target], ["laptop_factory"])
    assert supplied[("TGT-EXAMPLE", "laptop_factory")][0][1] == URL
    assert report[0]["assignment_basis"] == "SEARCH_TARGET_HINT"
    assert all("EXACT" not in row.values() for row in report)


def test_feed_does_not_evade_robots_or_request_ceiling(tmp_path):
    from test_catalog_leads import LF_ROBOTS

    routes = lf_routes(LF_ROBOTS + "Disallow: /wp-json/\n")
    _, transport, folder = run_cli(tmp_path, routes, "--include-product-feed")
    assert len(transport.calls) == 3
    assert "ROBOTS_DISALLOWED" in (folder / "catalog_requests.csv").read_text()
    _, transport, _ = run_cli(tmp_path, lf_routes(), "--include-product-feed",
                              "--max-total-requests", "3", out="budget")
    assert len(transport.calls) == 3


def test_clues_only_rank_candidates_and_capacity_units_are_equivalent():
    entries = [
        {"retailer_id": "pcx", "url": "https://pcx.com.ph/products/aaa-laptop",
         "title": "HP Laptop 15 Ryzen 3 8GB 256GB"},
        {"retailer_id": "pcx", "url": "https://pcx.com.ph/products/zzz-laptop",
         "title": "HP Laptop 15 Ryzen 5 16GB 1TB"},
    ]
    index = CatalogIndex(entries, retrieved_at="2026-10-25T00:00:00+00:00", max_results=1,
                         coverage={"pcx": {"product_sitemaps_read": 1,
                                           "product_sitemaps_listed": 1}})
    response = index.search('site:pcx.com.ph HP Laptop 15', clues="Ryzen 5 16.0GB 1000GB")
    assert response.results[0]["url"].endswith("zzz-laptop")
    assert index.search('site:pcx.com.ph DifferentModel', clues="Ryzen 5 16GB").results == []
