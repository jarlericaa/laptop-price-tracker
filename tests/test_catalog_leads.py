"""Catalog leads from sitemaps (2026-10-22): robots rules, parsing, lead rules and the CLI.

Synthetic sitemap fixtures in the two formats the stores advertise in robots.txt (WordPress core
``wp-sitemap.xml`` for Laptop Factory; Shopify ``sitemap.xml``). The live layouts are not yet
verified: these tests pin the parser and the safety rules, not the stores' current content.
"""

from __future__ import annotations

import csv
import importlib.util
import json
from argparse import Namespace
from pathlib import Path

import pytest
from scraping_fakes import FakeClock, no_network  # noqa: F401
from test_product_discovery import Routes

from laptop_tracker.catalog_leads import (
    CATALOG_SOURCES,
    LeadTarget,
    SitemapEntry,
    SitemapError,
    catalog_leads,
    lead_basis,
    parse_robots,
    parse_sitemap,
    product_entries,
    product_sitemaps,
    robots_allows,
)
from laptop_tracker.scraping import TransportResponse

pytestmark = pytest.mark.usefixtures("no_network")
ROOT = Path(__file__).resolve().parents[1]
LF_ROBOTS = """User-agent: *
Disallow: /wp-content/uploads/wc-logs/
Disallow: /*?add-to-cart=
Disallow: /wp-admin/
Allow: /wp-admin/admin-ajax.php
Sitemap: https://laptopfactory.com.ph/wp-sitemap.xml
"""
WP_INDEX = b"""<?xml version="1.0" encoding="UTF-8"?>
<?xml-stylesheet type="text/xsl" href="https://laptopfactory.com.ph/wp-sitemap-index.xsl" ?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>https://laptopfactory.com.ph/wp-sitemap-posts-post-1.xml</loc></sitemap>
<sitemap><loc>https://laptopfactory.com.ph/wp-sitemap-posts-product-1.xml</loc></sitemap>
<sitemap><loc>https://laptopfactory.com.ph/wp-sitemap-taxonomies-product_cat-1.xml</loc></sitemap>
</sitemapindex>"""
WP_PRODUCTS = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://laptopfactory.com.ph/product/msi-gaming-thin-15-b12uc-1432ph/</loc>
<lastmod>2026-09-30T02:00:00+00:00</lastmod></url>
<url><loc>https://laptopfactory.com.ph/product/hp-laptop-14-ep0445tu/</loc></url>
<url><loc>https://laptopfactory.com.ph/product/msi-thin-15-b12uc-1432phx/</loc></url>
<url><loc>https://laptopfactory.com.ph/product-category/asus/</loc></url>
</urlset>"""
SHOPIFY_INDEX = b"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>https://pcx.com.ph/sitemap_products_1.xml?from=1&amp;to=99</loc></sitemap>
<sitemap><loc>https://pcx.com.ph/sitemap_pages_1.xml</loc></sitemap>
<sitemap><loc>https://elsewhere.example/sitemap_products_1.xml</loc></sitemap>
</sitemapindex>"""
SHOPIFY_PRODUCTS = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">
<url><loc>https://pcx.com.ph/products/asus-vivobook-go-15-e1504fa-nj1527w-amd-ryzen-3-laptop</loc>
<lastmod>2026-10-01</lastmod></url>
<url><loc>https://pcx.com.ph/products/rog-strix-g16-gaming-laptop-eclipse-gray</loc>
<image:image><image:title>ASUS ROG STRIX G16 (G614PM-TS179WSM) RTX 5060</image:title></image:image>
</url>
<url><loc>https://pcx.com.ph/products/hp-laptop-15-natural-silver</loc></url>
</urlset>"""
TARGETS = [LeadTarget("TGT-000021", "B12UC-1432PH"), LeadTarget("TGT-000233", "14-ep0445TU"),
           LeadTarget("TGT-000073", "E1504FA-NJ1527W"), LeadTarget("TGT-000054",
                                                                    "G614PM-TS179WSM")]


def test_robots_rules_use_the_longest_match_and_wildcards() -> None:
    rules = parse_robots(LF_ROBOTS)
    assert rules.sitemaps == ("https://laptopfactory.com.ph/wp-sitemap.xml",)
    assert robots_allows(rules, "https://laptopfactory.com.ph/wp-sitemap.xml")
    assert robots_allows(rules, "https://laptopfactory.com.ph/wp-admin/admin-ajax.php")
    assert not robots_allows(rules, "https://laptopfactory.com.ph/wp-admin/options.php")
    assert not robots_allows(rules, "https://laptopfactory.com.ph/shop/?add-to-cart=12")
    blocked = parse_robots("User-agent: *\nDisallow: /\n\nUser-agent: other\nAllow: /\n")
    assert not robots_allows(blocked, "https://x.example/sitemap.xml")
    named = parse_robots("User-agent: *\nDisallow: /\nUser-agent: laptop-price-tracker\n"
                         "Allow: /sitemap.xml$\nDisallow: /\n", "laptop-price-tracker/0.1")
    assert robots_allows(named, "https://x.example/sitemap.xml")
    assert not robots_allows(named, "https://x.example/sitemap.xml.gz")


def test_sitemaps_parse_indexes_and_product_url_sets() -> None:
    lf = CATALOG_SOURCES["laptop_factory"]
    index = parse_sitemap(WP_INDEX)
    assert index.kind == "index"
    assert product_sitemaps(lf, index) == [
        "https://laptopfactory.com.ph/wp-sitemap-posts-product-1.xml"]
    products = product_entries(lf, parse_sitemap(WP_PRODUCTS))
    assert [e.loc.rsplit("/", 2)[-2] for e in products] == [
        "msi-gaming-thin-15-b12uc-1432ph", "hp-laptop-14-ep0445tu", "msi-thin-15-b12uc-1432phx"]
    assert products[0].lastmod.startswith("2026-09-30")
    pcx = CATALOG_SOURCES["pcx"]
    shop = parse_sitemap(SHOPIFY_INDEX)
    assert product_sitemaps(pcx, shop) == ["https://pcx.com.ph/sitemap_products_1.xml?from=1&to=99"]
    titled = product_entries(pcx, parse_sitemap(SHOPIFY_PRODUCTS))
    assert titled[1].title == "ASUS ROG STRIX G16 (G614PM-TS179WSM) RTX 5060"
    with pytest.raises(SitemapError):
        parse_sitemap(b"<html><body>Access denied</body></html>")
    with pytest.raises(SitemapError):
        parse_sitemap(b"not xml at all")


def test_a_lead_needs_the_complete_code_in_the_handle_or_sitemap_title() -> None:
    def basis(url: str, title: str = "", target: int = 0) -> str:
        return lead_basis(SitemapEntry(url, title=title), TARGETS[target])

    lf = "https://laptopfactory.com.ph/product/"
    assert basis(lf + "msi-gaming-thin-15-b12uc-1432ph/") == "URL_HANDLE_SHOWS_MODEL_CODE"
    assert basis(lf + "msi-thin-15-b12uc-1432phx/") == ""  # another code, not a suffix strip
    assert basis(lf + "msi-thin-15-b12uc/") == ""  # a shortened code never leads
    assert basis("https://pcx.com.ph/products/rog-strix-g16",
                 "ASUS ROG STRIX G16 (G614PM-TS179WSM)", 3) == "SITEMAP_TITLE_SHOWS_MODEL_CODE"
    assert basis("https://pcx.com.ph/products/rog-strix-g16",
                 "ASUS ROG STRIX G16 (G614PM-TS179WSX)", 3) == ""


def test_leads_use_the_supplied_url_format_and_never_assign_by_url_alone() -> None:
    pcx = CATALOG_SOURCES["pcx"]
    entries = [("S1", e) for e in product_entries(pcx, parse_sitemap(SHOPIFY_PRODUCTS))]
    leads = catalog_leads("pcx", entries, TARGETS, supplied_on="2026-10-22")
    assert [(lead["url"].rsplit("/", 1)[-1], lead["search_target_id"]) for lead in leads] == [
        ("asus-vivobook-go-15-e1504fa-nj1527w-amd-ryzen-3-laptop", ""),
        ("rog-strix-g16-gaming-laptop-eclipse-gray", "TGT-000054"),
    ]
    assert all(lead["supplied_by"] == "catalog_sitemap" and "never a match" in lead["notes"]
               for lead in leads)


def load_cli():
    spec = importlib.util.spec_from_file_location("catalog_cli",
                                                  ROOT / "scripts" / "discover_catalog_leads.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def scope(tmp_path: Path) -> list[str]:
    path = tmp_path / "tm.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["target_id", "brand", "model"])
        writer.writerows([["TGT-000021", "MSI", "B12UC-1432PH"],
                          ["TGT-000233", "HP", "14-ep0445TU"],
                          ["TGT-000013", "HP", "HP Laptop 14"]])
    return ["--targets", "TGT-000021,TGT-000233,TGT-000013", "--target-master", str(path)]


def lf_routes(robots: str = LF_ROBOTS) -> dict[str, TransportResponse]:
    xml = {"content-type": "application/xml; charset=UTF-8"}
    return {
        "https://laptopfactory.com.ph/robots.txt": TransportResponse(
            200, "https://laptopfactory.com.ph/robots.txt", robots.encode(),
            {"content-type": "text/plain"}),
        "https://laptopfactory.com.ph/wp-sitemap.xml": TransportResponse(
            200, "https://laptopfactory.com.ph/wp-sitemap.xml", WP_INDEX, xml),
        "https://laptopfactory.com.ph/wp-sitemap-posts-product-1.xml": TransportResponse(
            200, "https://laptopfactory.com.ph/wp-sitemap-posts-product-1.xml", WP_PRODUCTS, xml),
    }


def run_cli(tmp_path, routes, *extra, out="leads"):
    transport = Routes(routes)
    code = load_cli().main(["--live", *scope(tmp_path), "--retailers", "laptop_factory",
                            "--max-total-requests", "5", "--output-dir", str(tmp_path / out),
                            *extra], transport=transport, clock=FakeClock())
    return code, transport, tmp_path / out


def read(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_live_run_checks_robots_first_and_writes_leads(tmp_path) -> None:
    code, transport, out = run_cli(tmp_path, lf_routes())
    assert code == 0
    assert transport.calls[0].endswith("/robots.txt") and len(transport.calls) == 3
    leads = read(out / "catalog_leads.csv")
    assert {lead["url"].rsplit("/", 2)[-2] for lead in leads} == {
        "msi-gaming-thin-15-b12uc-1432ph", "hp-laptop-14-ep0445tu"}  # family target: no lead
    summary = json.loads((out / "catalog_summary.json").read_text())
    assert summary["retailers"]["laptop_factory"]["robots"] == "ALLOWED"
    assert summary["family_or_name_targets_without_leads"] == 1
    assert all(row["evidence_file"] for row in read(out / "catalog_requests.csv"))


def test_a_disallowed_sitemap_is_never_requested(tmp_path) -> None:
    code, transport, out = run_cli(tmp_path, lf_routes(LF_ROBOTS + "Disallow: /wp-sitemap\n"))
    assert code == 0 and transport.calls == ["https://laptopfactory.com.ph/robots.txt"]
    summary = json.loads((out / "catalog_summary.json").read_text())
    assert summary["retailers"]["laptop_factory"]["robots"] == "ROOT_SITEMAP_DISALLOWED"


def test_the_request_ceiling_is_a_hard_limit(tmp_path) -> None:
    code, transport, out = run_cli(tmp_path, lf_routes(), "--max-total-requests", "2")
    assert code == 0 and len(transport.calls) == 2
    failed = [r for r in read(out / "catalog_requests.csv") if r["outcome"] == "FETCH_FAILED"]
    assert failed and "REQUEST_BUDGET_EXHAUSTED" in failed[0]["message"]
    assert read(out / "catalog_leads.csv") == []


def test_a_saved_run_replays_offline_with_the_same_leads(tmp_path) -> None:
    _, _, first = run_cli(tmp_path, lf_routes())
    transport = Routes({})
    code = load_cli().main(["--from-saved", str(first), *scope(tmp_path), "--retailers",
                            "laptop_factory", "--output-dir", str(tmp_path / "replay")],
                           transport=transport, clock=FakeClock())
    assert code == 0 and transport.calls == []
    assert read(tmp_path / "replay" / "catalog_leads.csv") == read(first / "catalog_leads.csv")


def test_preview_sends_nothing(tmp_path) -> None:
    transport = Routes({})
    code = load_cli().main(["--preview", *scope(tmp_path), "--output-dir",
                            str(tmp_path / "plan")], transport=transport, clock=FakeClock())
    assert code == 0 and transport.calls == []


def test_leads_are_accepted_by_discovery_as_supplied_urls(tmp_path) -> None:
    from test_product_discovery import discover

    _, _, out = run_cli(tmp_path, lf_routes())
    targets = [discover.SearchTarget("TGT-000021", "26", "MSI", "Thin 15", "B12UC-1432PH", "",
                                     {}),
               discover.SearchTarget("TGT-000233", "49", "HP", "", "14-ep0445TU", "", {})]
    args = Namespace(supplied_urls=[out / "catalog_leads.csv"], no_supplied_urls=True)
    found, report = discover.load_supplied(args, targets, ["laptop_factory"])
    assert {key: [basis for *_, basis in urls] for key, urls in found.items()} == {
        ("TGT-000021", "laptop_factory"): ["URL_HANDLE_SHOWS_MODEL_CODE"],
        ("TGT-000233", "laptop_factory"): ["URL_HANDLE_SHOWS_MODEL_CODE"],
    }
    assert {row["assignment_basis"] for row in report} == {"URL_HANDLE_SHOWS_MODEL_CODE"}
