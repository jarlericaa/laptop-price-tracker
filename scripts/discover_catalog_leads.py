#!/usr/bin/env python3
"""Catalog leads: product URLs from retailer sitemaps that show a scoped target's model code."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from datetime import UTC
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from laptop_tracker.catalog_index import parse_product_feed
from laptop_tracker.catalog_leads import (
    CATALOG_SOURCES,
    LEAD_COLUMNS,
    SitemapError,
    catalog_leads,
    lead_targets,
    parse_robots,
    parse_sitemap,
    product_entries,
    product_sitemaps,
    robots_allows,
)
from laptop_tracker.product_names import default_registry
from laptop_tracker.retailer_search import BudgetedTransport
from laptop_tracker.scraping import (
    Clock,
    FetchConfig,
    Fetcher,
    FetchError,
    ScrapeRequest,
    SystemClock,
    Transport,
    UrllibTransport,
)

REQUEST_COLUMNS = ("retailer_id", "kind", "url", "outcome", "http_status", "retrieved_at",
                   "content_sha256", "evidence_file", "entries", "message")
ENTRY_COLUMNS = ("retailer_id", "sitemap", "url", "lastmod", "title")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(
        "Read the retailers' advertised sitemaps (robots.txt checked first) and list product "
        "URLs that show a scoped MODEL_CODE target's complete code, as a supplied-URL CSV for "
        "discover_products.py --supplied-urls. A lead is re-fetched and re-matched there; it is "
        "never a match. --preview sends nothing; --live fetches robots.txt and sitemaps within "
        "--max-total-requests (no product page); --from-saved re-reads an earlier run offline."))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preview", action="store_true", help="Plan only; no requests.")
    mode.add_argument("--live", action="store_true", help="Fetch robots.txt and sitemaps.")
    mode.add_argument("--from-saved", type=Path, metavar="RUN_DIR",
                      help="Parse the saved responses of an earlier --live run; no requests.")
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--scope-file", type=Path, help="CSV with a target_id column.")
    scope.add_argument("--targets", help="Comma-separated target IDs.")
    parser.add_argument("--target-master", type=Path, required=True,
                        help="Validated TARGET_MASTER snapshot CSV.")
    parser.add_argument("--retailers", default=",".join(CATALOG_SOURCES))
    parser.add_argument("--max-total-requests", type=int,
                        help="Hard ceiling on requests (required with --live).")
    parser.add_argument("--max-sitemaps-per-retailer", type=int, default=5,
                        help="Product sitemaps read per retailer (default 5).")
    parser.add_argument("--include-product-feed", action="store_true",
                        help="Cache Laptop Factory's public feed for title/spec clues.")
    parser.add_argument("--max-feed-pages", type=int, default=20)
    parser.add_argument("--min-seconds-between-requests", type=float,
                        default=FetchConfig().min_request_interval_seconds)
    parser.add_argument("--output-dir", type=Path, required=True, help="New run folder.")
    args = parser.parse_args(argv)
    if not 1 <= args.max_feed_pages <= 100:
        parser.error("--max-feed-pages must be between 1 and 100.")
    if args.live and not args.max_total_requests:
        parser.error("--live requires --max-total-requests.")
    if args.min_seconds_between_requests < FetchConfig().min_request_interval_seconds:
        parser.error("--min-seconds-between-requests may not be below the default spacing.")
    unknown = set(args.retailers.split(",")) - set(CATALOG_SOURCES)
    if unknown:
        parser.error(f"Unknown retailer(s): {', '.join(sorted(unknown))}.")
    return args


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, columns, rows) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns))
        writer.writeheader()
        writer.writerows({c: r.get(c, "") for c in columns} for r in rows)


class Session:
    """Fetches live (budgeted, throttled, evidence saved) or replays a saved run."""

    def __init__(self, args, out: Path, transport: Transport | None, clock: Clock) -> None:
        self.args = args
        self.out = out
        self.requests: list[dict[str, str]] = []
        self.saved: dict[str, dict[str, str]] = {}
        if args.from_saved:
            for row in read_rows(args.from_saved / "catalog_requests.csv"):
                if row["evidence_file"]:
                    self.saved[row["url"]] = row
            self.budget = None
            self.fetcher = None
        else:
            self.budget = BudgetedTransport(transport or UrllibTransport(),
                                            args.max_total_requests or 0)
            self.fetcher = Fetcher(self.budget, FetchConfig(
                min_request_interval_seconds=args.min_seconds_between_requests), clock)

    def get(self, retailer: str, kind: str, url: str) -> bytes | None:
        row = {"retailer_id": retailer, "kind": kind, "url": url}
        if self.args.from_saved:
            saved = self.saved.get(url)
            if saved is None:
                self.requests.append(row | {"outcome": "NOT_SENT_REPLAY_ONLY"})
                return None
            body = (self.args.from_saved / saved["evidence_file"]).read_bytes()
            self.requests.append(row | {"outcome": "SAVED", "evidence_file":
                                        saved["evidence_file"],
                                        "content_sha256": hashlib.sha256(body).hexdigest()})
            return body
        try:
            page = self.fetcher.fetch(ScrapeRequest(retailer, url),
                                      frozenset({urlsplit(url).netloc}))
        except FetchError as error:
            self.requests.append(row | {"outcome": "FETCH_FAILED",
                                        "http_status": str(getattr(error, "http_status", "")
                                                           or ""),
                                        "message": f"{error.code}: {error}"[:300]})
            return None
        suffix = "txt" if kind == "robots" else "json" if kind == "product_feed" else "xml"
        name = f"evidence/{page.content_sha256}.{suffix}"
        (self.out / name).write_bytes(page.body)
        self.requests.append(row | {
            "outcome": "FETCHED", "http_status": str(page.status_code),
            "retrieved_at": page.retrieved_at.astimezone(UTC).isoformat(),
            "content_sha256": page.content_sha256, "evidence_file": name})
        return page.body


def main(argv: list[str] | None = None, *, transport: Transport | None = None,
         clock: Clock | None = None) -> int:
    args = parse_args(argv)
    clock = clock or SystemClock()
    out = args.output_dir
    out.mkdir(parents=True, exist_ok=False)
    (out / "evidence").mkdir()
    wanted = ([r["target_id"].strip() for r in read_rows(args.scope_file)] if args.scope_file
              else [t.strip() for t in args.targets.split(",") if t.strip()])
    catalog = {r["target_id"]: r for r in read_rows(args.target_master)}
    missing = [t for t in wanted if t not in catalog]
    if missing:
        print(f"Targets not in the catalog snapshot: {', '.join(missing)}", file=sys.stderr)
        return 1
    rows = [catalog[t] for t in dict.fromkeys(wanted)]
    registry = default_registry()
    kinds = {r["target_id"]: registry.kind(r.get("brand", ""), r["model"]) for r in rows}
    targets = lead_targets(rows, kinds)
    retailers = args.retailers.split(",")
    summary: dict = {
        "mode": "PREVIEW" if args.preview else "SAVED_REPLAY" if args.from_saved else "LIVE",
        "started_at_utc": clock.now().astimezone(UTC).isoformat(),
        "scope_targets": len(rows), "model_code_targets": len(targets),
        "family_or_name_targets_without_leads": len(rows) - len(targets),
        "limits": {"max_total_requests": args.max_total_requests,
                   "max_sitemaps_per_retailer": args.max_sitemaps_per_retailer,
                   "include_product_feed": args.include_product_feed,
                   "max_feed_pages": args.max_feed_pages,
                   "min_seconds_between_requests": args.min_seconds_between_requests},
        "retailers": {},
    }
    if args.preview:
        for retailer in retailers:
            source = CATALOG_SOURCES[retailer]
            host = urlsplit(source.root_sitemap).netloc
            feed_pages = args.max_feed_pages if (
                args.include_product_feed and retailer == "laptop_factory") else 0
            summary["retailers"][retailer] = {
                "would_request": [f"https://{host}/robots.txt", source.root_sitemap,
                                  f"up to {args.max_sitemaps_per_retailer} product sitemaps "
                                  f"matching {source.product_sitemap}"],
                "worst_case_requests": 2 + args.max_sitemaps_per_retailer + feed_pages}
        (out / "catalog_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        worst = sum(status["worst_case_requests"] for status in summary["retailers"].values())
        print(f"Preview: {len(targets)} MODEL_CODE target(s) of {len(rows)}; worst case "
              f"{worst} requests. Nothing sent.")
        return 0

    session = Session(args, out, transport, clock)
    entries_out: list[dict[str, str]] = []
    leads: list[dict[str, str]] = []
    day = clock.now().astimezone(UTC).date().isoformat()
    for retailer in retailers:
        source = CATALOG_SOURCES[retailer]
        host = urlsplit(source.root_sitemap).netloc
        status = {"robots": "", "root_advertised": False, "product_sitemaps_listed": 0,
                  "product_sitemaps_read": 0, "product_urls": 0, "leads": 0}
        summary["retailers"][retailer] = status
        robots = session.get(retailer, "robots", f"https://{host}/robots.txt")
        if robots is None:
            status["robots"] = "UNAVAILABLE_NOTHING_ELSE_REQUESTED"
            continue
        rules = parse_robots(robots.decode("utf-8", "replace"), FetchConfig().user_agent)
        status["root_advertised"] = source.root_sitemap in rules.sitemaps
        if not robots_allows(rules, source.root_sitemap):
            status["robots"] = "ROOT_SITEMAP_DISALLOWED"
            continue
        status["robots"] = "ALLOWED"
        body = session.get(retailer, "sitemap_index", source.root_sitemap)
        if body is None:
            continue
        try:
            index = parse_sitemap(body)
        except SitemapError as error:
            session.requests[-1]["message"] = str(error)
            continue
        children = [source.root_sitemap] if index.kind == "urlset" else product_sitemaps(
            source, index)
        status["product_sitemaps_listed"] = len(children)
        found = []
        for child in children[: args.max_sitemaps_per_retailer]:
            if not robots_allows(rules, child):
                session.requests.append({"retailer_id": retailer, "kind": "product_sitemap",
                                         "url": child, "outcome": "ROBOTS_DISALLOWED"})
                continue
            data = index if child == source.root_sitemap else None
            if data is None:
                raw = session.get(retailer, "product_sitemap", child)
                if raw is None:
                    continue
                try:
                    data = parse_sitemap(raw)
                except SitemapError as error:
                    session.requests[-1]["message"] = str(error)
                    continue
            status["product_sitemaps_read"] += 1
            products = product_entries(source, data)
            session.requests[-1]["entries"] = str(len(products))
            found += [(child, entry) for entry in products]
            entries_out += [{"retailer_id": retailer, "sitemap": child, "url": e.loc,
                             "lastmod": e.lastmod, "title": e.title} for e in products]
        for child in children[args.max_sitemaps_per_retailer:]:
            session.requests.append({"retailer_id": retailer, "kind": "product_sitemap",
                                     "url": child, "outcome": "NOT_SENT_SITEMAP_LIMIT"})
        if args.include_product_feed and retailer == "laptop_factory":
            status.update(feed_pages_read=0, feed_products=0, feed_complete=False)
            for number in range(1, args.max_feed_pages + 1):
                feed_url = (f"https://{host}/wp-json/wc/store/v1/products?per_page=100"
                            f"&page={number}&orderby=id&order=asc")
                if not robots_allows(rules, feed_url):
                    session.requests.append({"retailer_id": retailer, "kind": "product_feed",
                                             "url": feed_url, "outcome": "ROBOTS_DISALLOWED"})
                    break
                raw = session.get(retailer, "product_feed", feed_url)
                if raw is None:
                    break
                try:
                    products = parse_product_feed(raw, retailer)
                except ValueError:
                    session.requests[-1]["message"] = "Invalid public product-feed response"
                    break
                status["feed_pages_read"] += 1
                status["feed_products"] += len(products)
                session.requests[-1]["entries"] = str(len(products))
                if len(products) < 100:
                    status["feed_complete"] = True
                    break
        status["product_urls"] = len(found)
        mine = catalog_leads(retailer, found, targets, supplied_on=day)
        status["leads"] = len(mine)
        leads += mine
    write_csv(out / "catalog_requests.csv", REQUEST_COLUMNS, session.requests)
    write_csv(out / "catalog_entries.csv", ENTRY_COLUMNS, entries_out)
    write_csv(out / "catalog_leads.csv", LEAD_COLUMNS, leads)
    summary["requests_sent"] = session.budget.sent if session.budget else 0
    summary["leads"] = len(leads)
    summary["finished_at_utc"] = clock.now().astimezone(UTC).isoformat()
    (out / "catalog_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"{len(leads)} lead(s) for {len(targets)} MODEL_CODE target(s); requests sent "
          f"{summary['requests_sent']}. Use: discover_products.py --supplied-urls "
          f"{out / 'catalog_leads.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
