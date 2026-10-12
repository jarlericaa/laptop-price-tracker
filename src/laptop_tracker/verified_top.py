"""Top 100 verified configurations: a target-level working list (owner decision 2026-10-12).

The working list is not the overall sales Top 100. It ranks unique, ACTIVE ``CONFIGURATION``
targets with complete five-field identities by the combined GMV of their eligible, approved
mapped marketplace variations. The source-level sales Top 100 (Phase 4) stays unchanged as a
reference.

Inputs: the full Phase 4 eligible source ranking (``eligible_model_ranking.csv``), the accepted
source Top 100 (consistency check and reference flags), validated TARGET_MASTER,
SOURCE_MODEL_MAP, and MATCH_REVIEW tables, and an optional local identity review register. IDs
are exact text. GMV is summed with ``Decimal`` from the exact source text. Each source counts
once. Nothing here infers specifications, promotes candidates, creates mappings, or writes
Sheets.
"""

from __future__ import annotations

import csv
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .catalog_conventions import cpu_model_families
from .conventions import apple_chip_form, cpu_series_forms, is_model_code
from .normalization import CORE_MATCH_COLUMNS, core_key, core_value_key, text_key
from .supporting_tables import SOURCE_MODEL_ID_PATTERN, TableResult
from .target_master import (
    CONFIGURATION,
    LISTING_ONLY,
    TARGET_ID_PATTERN,
    TargetMasterResult,
    effective_tracking_mode,
)

DEFAULT_TOP_N = 100

# Columns the eligible ranking must provide (Phase 4 MODEL_SUMMARY_COLUMNS plus rank).
RANKING_REQUIRED_COLUMNS: tuple[str, ...] = (
    "rank",
    "run_id",
    "extract_date",
    "m_minus_1_month",
    "mtd_month",
    "shop_id",
    "item_id",
    "model_id",
    "item_name",
    "shopee_model_name",
    "m_minus_1_gmv_usd",
    "mtd_gmv_usd",
    "ranking_gmv_usd",
    "eligibility_status",
    "present_in_mtd",
)
SALES_PROVENANCE: tuple[str, ...] = ("run_id", "extract_date", "m_minus_1_month", "mtd_month")

CONFIGURATION_COLUMNS: tuple[str, ...] = (
    "rank",
    "target_id",
    "brand",
    "series",
    *CORE_MATCH_COLUMNS,
    "m_minus_1_gmv_usd",
    "mtd_gmv_usd",
    "ranking_gmv_usd",
    "contributing_source_count",
    "contributing_source_model_ids",
    "original_source_top_100_contributors",
    "excluded_mapped_source_count",
    "sales_run_id",
    "sales_extract_date",
    "sales_m_minus_1_month",
    "sales_mtd_month",
    "target_master_sha256",
)

LEDGER_COLUMNS: tuple[str, ...] = (
    "source_rank",
    "model_id",
    "shop_id",
    "item_id",
    "item_name",
    "shopee_model_name",
    "m_minus_1_gmv_usd",
    "mtd_gmv_usd",
    "ranking_gmv_usd",
    "in_original_source_top_100",
    "target_id",
    "contribution_status",
    "reason_code",
    "reason_detail",
)

EXCLUSION_COLUMNS: tuple[str, ...] = (
    "scope",
    "model_id",
    "target_id",
    "reason_code",
    "reason_detail",
    "ranking_gmv_usd",
    "in_original_source_top_100",
)

REVIEW_PRIORITY_COLUMNS: tuple[str, ...] = (
    "priority",
    "scope",
    "model_id",
    "target_id",
    "shop_id",
    "item_id",
    "item_name",
    "shopee_model_name",
    "ranking_gmv_usd",
    "mtd_gmv_usd",
    "in_original_source_top_100",
    "review_statuses",
    "review_row_numbers",
    "missing_core_fields",
    "known_register_notes",
    "suggested_action",
)

REGISTER_COLUMNS: tuple[str, ...] = (
    "source_model_id",
    "target_id",
    "field",
    "recorded_value",
    "stated_value",
    "status",
    "note",
    "recorded_at",
)
REGISTER_STATUSES = ("OPEN", "RESOLVED")

CONTRIBUTING = "CONTRIBUTING"
EXCLUDED = "EXCLUDED"

# Allowed difference between a source's ranking_gmv_usd and its m_minus_1 + mtd text. Phase 4
# writes floats, so a sum such as 900702.0753 is stored as 900702.0752999999.
_FLOAT_TOLERANCE = Decimal("0.0001")
_MONTH = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])\Z")
_DATE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])-[0-9]{2}\Z")


class VerifiedRankingInputError(ValueError):
    """An input is missing, inconsistent, or malformed; no output is produced."""


@dataclass(frozen=True)
class SourceRow:
    """One eligible-ranking row, all values exact text plus parsed GMV."""

    values: Mapping[str, str]
    m_minus_1_gmv: Decimal
    mtd_gmv: Decimal
    ranking_gmv: Decimal

    @property
    def model_id(self) -> str:
        return self.values["model_id"]


@dataclass(frozen=True)
class SalesSnapshot:
    rows: tuple[SourceRow, ...]
    run_id: str
    extract_date: str
    m_minus_1_month: str
    mtd_month: str

    def period_labels(self) -> dict[str, str]:
        return {
            "m_minus_1_gmv_usd": f"{self.m_minus_1_month} (full calendar month)",
            "mtd_gmv_usd": (f"{self.mtd_month} month-to-date, as extracted on {self.extract_date}"),
            "note": (
                "Historical sales snapshot from Phase 4 run "
                f"{self.run_id}; rebuilding this ranking does not refresh the sales data."
            ),
        }


@dataclass(frozen=True)
class RegisterEntry:
    source_model_id: str
    target_id: str
    field: str
    recorded_value: str
    stated_value: str
    status: str
    note: str
    recorded_at: str

    def describe(self) -> str:
        parts = [f"{self.field}: recorded {self.recorded_value!r}, stated {self.stated_value!r}"]
        if self.note:
            parts.append(self.note)
        return "; ".join(parts) + f" (register {self.status}, {self.recorded_at})"


@dataclass
class VerifiedRanking:
    configurations: list[dict[str, str]]
    top: list[dict[str, str]]
    ledger: list[dict[str, str]]
    exclusions: list[dict[str, str]]
    review_priority: list[dict[str, str]]
    counts: dict[str, int] = field(default_factory=dict)


OUTPUT_FILES = {
    "ranking": "verified_configuration_ranking.csv",
    "top": "top_100_verified_configurations.csv",
    "ledger": "verified_configuration_contributions.csv",
    "exclusions": "verified_configuration_exclusions.csv",
    "review_priority": "verified_configuration_review_priority.csv",
    "summary": "verified_configuration_run_summary.json",
}


def sha256_file(path: str | Path) -> str:
    import hashlib

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sha256_values(values: Sequence[Sequence[object]]) -> str:
    """Fingerprint of worksheet values read from Sheets, as UTF-8 CSV text."""
    import hashlib
    import io

    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerows(
        ["" if cell is None else str(cell) for cell in row] for row in values
    )
    return hashlib.sha256(buffer.getvalue().encode("utf-8")).hexdigest()


CATALOG_TABLES = ("TARGET_MASTER", "SOURCE_MODEL_MAP", "MATCH_REVIEW")


def load_catalog_tables(
    paths: Mapping[str, str | Path], *, from_sheets: bool = False
) -> tuple[dict[str, list[list[str]]], dict[str, tuple[str, str]]]:
    """The three catalog tables (local snapshots, or Sheets read-only) with fingerprints.

    Returns the worksheet-shaped values and, per table, ``(source label, SHA-256)``. Only these
    three tabs are read; ``raw`` is never read.
    """
    from datetime import UTC, datetime

    from .config import Settings
    from .sheets_source import read_target_table_values
    from .target_master import read_table_snapshot_csv

    if from_sheets:
        tables = read_target_table_values(Settings.from_env(), CATALOG_TABLES)
        missing = [name for name, values in tables.items() if values is None]
        if missing:
            raise VerifiedRankingInputError(f"Worksheet(s) not found: {', '.join(missing)}.")
        read_at = datetime.now(UTC).isoformat(timespec="seconds")
        return tables, {  # type: ignore[return-value]
            name: (f"Sheets tab {name}, read {read_at}", sha256_values(values))
            for name, values in tables.items()
        }
    tables = {name: read_table_snapshot_csv(paths[name]) for name in CATALOG_TABLES}
    return tables, {name: (str(paths[name]), sha256_file(paths[name])) for name in CATALOG_TABLES}


def write_csv(path: Path, columns: Sequence[str], rows: Iterable[Mapping[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns))
        writer.writeheader()
        writer.writerows(rows)


# Input reading and validation


def parse_gmv(value: str, *, label: str) -> Decimal:
    """A finite, non-negative decimal from exact text; anything else is an input error."""
    text = value.strip()
    if not text or text != value:
        raise VerifiedRankingInputError(f"{label} is blank or has surrounding whitespace.")
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise VerifiedRankingInputError(f"{label} is not a decimal number.") from None
    if not number.is_finite() or number < 0:
        raise VerifiedRankingInputError(f"{label} must be a finite, non-negative amount.")
    return number


def money(value: Decimal) -> str:
    """Exact decimal text without an exponent."""
    return format(value, "f")


def read_csv_rows(path: str | Path, required: Sequence[str], name: str) -> list[dict[str, str]]:
    """Read every cell as exact text; refuse missing columns and rows with extra cells."""
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        headers = reader.fieldnames or []
        missing = [column for column in required if column not in headers]
        if missing:
            raise VerifiedRankingInputError(f"{name} is missing columns: {', '.join(missing)}.")
        rows = []
        for row_number, row in enumerate(reader, start=2):
            if None in row:
                raise VerifiedRankingInputError(f"{name} row {row_number} has extra cells.")
            rows.append({key: value or "" for key, value in row.items()})
    return rows


def read_sales_snapshot(path: str | Path) -> SalesSnapshot:
    return build_sales_snapshot(read_csv_rows(path, RANKING_REQUIRED_COLUMNS, "Eligible ranking"))


def build_sales_snapshot(records: Sequence[Mapping[str, str]]) -> SalesSnapshot:
    """Validate the full eligible ranking: unique IDs, one run and period pair, sound GMV."""
    if not records:
        raise VerifiedRankingInputError("Eligible ranking has no data rows.")
    for name in SALES_PROVENANCE:
        values = {record[name] for record in records}
        if len(values) != 1:
            raise VerifiedRankingInputError(
                f"Eligible ranking mixes {len(values)} values of {name}; one run is required."
            )
    first = records[0]
    m1, mtd, extract_date = first["m_minus_1_month"], first["mtd_month"], first["extract_date"]
    if not (_MONTH.fullmatch(m1) and _MONTH.fullmatch(mtd) and _DATE.fullmatch(extract_date)):
        raise VerifiedRankingInputError("Eligible ranking periods are not YYYY-MM / YYYY-MM-DD.")
    year, month = (int(part) for part in mtd.split("-"))
    expected_m1 = f"{year - 1}-12" if month == 1 else f"{year}-{month - 1:02d}"
    if m1 != expected_m1 or not extract_date.startswith(mtd):
        raise VerifiedRankingInputError(
            "Eligible ranking periods are inconsistent: M-1 must precede the MTD month, and the "
            "extract date must fall in the MTD month."
        )

    seen: set[str] = set()
    rows = []
    for row_number, record in enumerate(records, start=2):
        model_id = record["model_id"]
        if not re.fullmatch(SOURCE_MODEL_ID_PATTERN, model_id):
            raise VerifiedRankingInputError(
                f"Eligible ranking row {row_number} has a blank or non-digit model_id."
            )
        if model_id in seen:
            raise VerifiedRankingInputError(
                f"Eligible ranking repeats model_id at row {row_number}; each source must "
                "appear once."
            )
        seen.add(model_id)
        label = f"Eligible ranking row {row_number}"
        m1_gmv = parse_gmv(record["m_minus_1_gmv_usd"], label=f"{label} m_minus_1_gmv_usd")
        mtd_gmv = parse_gmv(record["mtd_gmv_usd"], label=f"{label} mtd_gmv_usd")
        ranking_gmv = parse_gmv(record["ranking_gmv_usd"], label=f"{label} ranking_gmv_usd")
        if abs(ranking_gmv - (m1_gmv + mtd_gmv)) > _FLOAT_TOLERANCE:
            raise VerifiedRankingInputError(
                f"{label} ranking_gmv_usd does not equal m_minus_1_gmv_usd + mtd_gmv_usd."
            )
        rows.append(SourceRow(dict(record), m1_gmv, mtd_gmv, ranking_gmv))
    return SalesSnapshot(tuple(rows), first["run_id"], extract_date, m1, mtd)


def check_source_top_100(snapshot: SalesSnapshot, records: Sequence[Mapping[str, str]]) -> set[str]:
    """The accepted source Top 100 must be the same run, with the same GMV text, as the ranking.

    Returns its model IDs (exact text) for reference flags. It is never rebuilt or modified.
    """
    by_id = {row.model_id: row for row in snapshot.rows}
    ids: set[str] = set()
    for row_number, record in enumerate(records, start=2):
        model_id = record["model_id"]
        if model_id in ids:
            raise VerifiedRankingInputError(f"Source Top 100 repeats model_id at row {row_number}.")
        ids.add(model_id)
        source = by_id.get(model_id)
        if source is None:
            raise VerifiedRankingInputError(
                f"Source Top 100 row {row_number} is not in the eligible ranking."
            )
        for name in (*SALES_PROVENANCE, "m_minus_1_gmv_usd", "mtd_gmv_usd", "ranking_gmv_usd"):
            if record[name] != source.values[name]:
                raise VerifiedRankingInputError(
                    f"Source Top 100 row {row_number} disagrees with the eligible ranking on "
                    f"{name}; the two files are not from the same run."
                )
    return ids


def read_register(path: str | Path | None) -> list[RegisterEntry]:
    """Optional local register of known, unresolved identity conflicts (exact text)."""
    if path is None or not Path(path).is_file():
        return []
    entries = []
    keys: set[tuple[str, str, str]] = set()
    for row_number, record in enumerate(
        read_csv_rows(path, REGISTER_COLUMNS, "Identity review register"), start=2
    ):
        entry = RegisterEntry(**{name: record[name] for name in REGISTER_COLUMNS})
        label = f"Identity review register row {row_number}"
        if entry.status not in REGISTER_STATUSES:
            raise VerifiedRankingInputError(f"{label} status must be OPEN or RESOLVED.")
        if not entry.source_model_id and not entry.target_id:
            raise VerifiedRankingInputError(f"{label} needs a source_model_id or target_id.")
        if entry.source_model_id and not re.fullmatch(
            SOURCE_MODEL_ID_PATTERN, entry.source_model_id
        ):
            raise VerifiedRankingInputError(f"{label} source_model_id must be digits.")
        if entry.target_id and not re.fullmatch(TARGET_ID_PATTERN, entry.target_id):
            raise VerifiedRankingInputError(f"{label} target_id is not a TGT- identifier.")
        key = (entry.source_model_id, entry.target_id, entry.field)
        if key in keys:
            raise VerifiedRankingInputError(f"{label} repeats an earlier entry.")
        keys.add(key)
        entries.append(entry)
    return entries


# Identity checks


def _target_issue(row: Mapping[str, object]) -> tuple[str, str] | None:
    """Why a mapped target cannot be a verified configuration, or None."""
    if str(row["status"]) != "ACTIVE":
        return "TARGET_INACTIVE", "Target status is not ACTIVE."
    if effective_tracking_mode(row.get("tracking_mode")) == LISTING_ONLY:
        return "TARGET_LISTING_ONLY", "LISTING_ONLY targets are tracked as single listings."
    key, issues = core_key(row)
    if key is None:
        return "TARGET_CORE_INCOMPLETE", ", ".join(issues)
    cpu_series = str(row["cpu_series"])
    cpu_model = str(row.get("cpu_model") or "")
    families = cpu_model_families(cpu_model, text_key(row.get("brand")) == "apple")
    if families and cpu_series not in families:
        return (
            "TARGET_CPU_MODEL_CONFLICT",
            f"cpu_model {cpu_model!r} indicates {', '.join(sorted(families))}, but "
            f"cpu_series is {cpu_series!r}.",
        )
    return None


def _review_disagreements(review: Mapping[str, object], target: Mapping[str, object]) -> list[str]:
    """Core fields where a COMPLETE review row disagrees with its target.

    The 2026-10-08 conventions are applied: a target model that is a verified code agrees with a
    review row recording that code, and a bare Apple chip or uncanonical CPU wording in an older
    review row agrees with the target's canonical cpu_series.
    """
    differing = []
    for name in CORE_MATCH_COLUMNS:
        review_value, target_value = review.get(name), target.get(name)
        try:
            if core_value_key(name, review_value) == core_value_key(name, target_value):
                continue
        except ValueError:
            differing.append(name)
            continue
        if name == "model" and is_model_code(str(target_value)):
            if text_key(review.get("manufacturer_model_code")) == text_key(target_value):
                continue
        if name == "cpu_series":
            text = str(review_value or "")
            forms = cpu_series_forms(text)
            if apple := apple_chip_form(text):
                forms.add(apple)
            if forms == {str(target_value)}:
                continue
        differing.append(name)
    return differing


def _contribution_issue(
    source: SourceRow,
    link: Mapping[str, object] | None,
    targets: Mapping[str, Mapping[str, object]],
    target_issues: Mapping[str, tuple[str, str] | None],
    reviews: Sequence[Mapping[str, object]],
    register: Sequence[RegisterEntry],
) -> tuple[str, str]:
    """(reason_code, detail) for an excluded source, or ("", "") when it contributes."""
    values = source.values
    if values["eligibility_status"] != "ELIGIBLE" or values["present_in_mtd"] != "True":
        return "SOURCE_NOT_ELIGIBLE", "Phase 4 requires ELIGIBLE and present_in_mtd."
    if link is None:
        return "UNMAPPED", "No approved SOURCE_MODEL_MAP row."
    target_id = str(link["target_id"])
    target = targets.get(target_id)
    if target is None:
        return "TARGET_NOT_VALID", "Mapped target_id is not a valid TARGET_MASTER row."
    if issue := target_issues[target_id]:
        return issue
    open_entries = [entry for entry in register if entry.source_model_id == source.model_id]
    if open_entries:
        return "KNOWN_UNRESOLVED_CONFLICT", " | ".join(entry.describe() for entry in open_entries)
    complete = [review for review in reviews if review["status"] == "COMPLETE"]
    open_reviews = sorted({str(review["status"]) for review in reviews} - {"COMPLETE", "REJECTED"})
    if not complete:
        return "NO_COMPLETE_REVIEW", "No COMPLETE MATCH_REVIEW row records this source's identity."
    if open_reviews:
        return (
            "REVIEW_STATUS_UNRESOLVED",
            f"MATCH_REVIEW also has {', '.join(open_reviews)} rows for this source.",
        )
    for review in complete:
        if effective_tracking_mode(review.get("tracking_mode")) != CONFIGURATION:
            return "REVIEW_MODE_MISMATCH", "The COMPLETE review row is not a CONFIGURATION."
        if differing := _review_disagreements(review, target):
            return (
                "REVIEW_CORE_DIFFERS",
                "COMPLETE review row differs from the target on " + ", ".join(differing) + ".",
            )
        review_code = str(review.get("manufacturer_model_code") or "").strip()
        target_code = str(target.get("manufacturer_model_code") or "").strip()
        if (
            is_model_code(review_code)
            and is_model_code(target_code)
            and review_code.casefold() != target_code.casefold()
        ):
            return (
                "REVIEW_CODE_DIFFERS",
                f"Review row records {review_code}; the target records {target_code}.",
            )
    return "", ""


# Ranking


def build_verified_ranking(
    snapshot: SalesSnapshot,
    target_master: TargetMasterResult,
    source_model_map: TableResult,
    match_review: TableResult,
    *,
    source_top_100_ids: Iterable[str] = (),
    register: Sequence[RegisterEntry] = (),
    top_n: int = DEFAULT_TOP_N,
    target_master_sha256: str = "",
) -> VerifiedRanking:
    if top_n < 1:
        raise VerifiedRankingInputError("top_n must be at least 1.")
    for name, table in (
        ("TARGET_MASTER", target_master),
        ("SOURCE_MODEL_MAP", source_model_map),
        ("MATCH_REVIEW", match_review),
    ):
        if not table.is_valid:
            raise VerifiedRankingInputError(f"{name} did not pass validation.")

    targets = {
        str(row["target_id"]): row for row in target_master.valid_rows.to_dict(orient="records")
    }
    links = {
        str(row["source_model_id"]): row
        for row in source_model_map.valid_rows.to_dict(orient="records")
    }
    reviews: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in match_review.valid_rows.to_dict(orient="records"):
        reviews[str(row["source_model_id"])].append(row)
    open_register = [entry for entry in register if entry.status == "OPEN"]
    target_issues: dict[str, tuple[str, str] | None] = {}
    for target_id, row in targets.items():
        issue = _target_issue(row)
        target_entries = [
            entry
            for entry in open_register
            if entry.target_id == target_id and not entry.source_model_id
        ]
        if issue is None and target_entries:
            issue = (
                "KNOWN_UNRESOLVED_CONFLICT",
                " | ".join(entry.describe() for entry in target_entries),
            )
        target_issues[target_id] = issue
    original = set(source_top_100_ids)

    ledger: list[dict[str, str]] = []
    contributions: dict[str, list[SourceRow]] = defaultdict(list)
    excluded_mapped: Counter[str] = Counter()
    for source in snapshot.rows:
        link = links.get(source.model_id)
        code, detail = _contribution_issue(
            source, link, targets, target_issues, reviews.get(source.model_id, []), open_register
        )
        target_id = str(link["target_id"]) if link is not None else ""
        if code:
            if target_id:
                excluded_mapped[target_id] += 1
        else:
            contributions[target_id].append(source)
        values = source.values
        ledger.append(
            {
                "source_rank": values["rank"],
                "model_id": source.model_id,
                "shop_id": values["shop_id"],
                "item_id": values["item_id"],
                "item_name": values["item_name"],
                "shopee_model_name": values["shopee_model_name"],
                "m_minus_1_gmv_usd": values["m_minus_1_gmv_usd"],
                "mtd_gmv_usd": values["mtd_gmv_usd"],
                "ranking_gmv_usd": values["ranking_gmv_usd"],
                "in_original_source_top_100": str(source.model_id in original),
                "target_id": target_id,
                "contribution_status": EXCLUDED if code else CONTRIBUTING,
                "reason_code": code,
                "reason_detail": detail,
            }
        )

    # Aggregate the full qualifying pool by target, then sort, then select the first top_n.
    configurations = []
    for target_id, sources in contributions.items():
        target = targets[target_id]
        m1 = sum((source.m_minus_1_gmv for source in sources), Decimal(0))
        mtd = sum((source.mtd_gmv for source in sources), Decimal(0))
        ordered = sorted(sources, key=lambda s: (-s.ranking_gmv, -s.mtd_gmv, s.model_id))
        configurations.append(
            {
                "target_id": target_id,
                "brand": str(target.get("brand") or ""),
                "series": str(target.get("series") or ""),
                **{name: str(target[name]) for name in CORE_MATCH_COLUMNS},
                "_ranking": m1 + mtd,
                "_mtd": mtd,
                "m_minus_1_gmv_usd": money(m1),
                "mtd_gmv_usd": money(mtd),
                "ranking_gmv_usd": money(m1 + mtd),
                "contributing_source_count": str(len(sources)),
                "contributing_source_model_ids": ";".join(s.model_id for s in ordered),
                "original_source_top_100_contributors": str(
                    sum(source.model_id in original for source in sources)
                ),
                "excluded_mapped_source_count": str(excluded_mapped[target_id]),
                "sales_run_id": snapshot.run_id,
                "sales_extract_date": snapshot.extract_date,
                "sales_m_minus_1_month": snapshot.m_minus_1_month,
                "sales_mtd_month": snapshot.mtd_month,
                "target_master_sha256": target_master_sha256,
            }
        )
    configurations.sort(key=lambda c: (-c["_ranking"], -c["_mtd"], c["target_id"]))
    for rank, configuration in enumerate(configurations, start=1):
        configuration["rank"] = str(rank)
        del configuration["_ranking"], configuration["_mtd"]
    configurations = [{name: c[name] for name in CONFIGURATION_COLUMNS} for c in configurations]
    top = configurations[:top_n]

    exclusions = [
        {
            "scope": "SOURCE",
            "model_id": row["model_id"],
            "target_id": row["target_id"],
            "reason_code": row["reason_code"],
            "reason_detail": row["reason_detail"],
            "ranking_gmv_usd": row["ranking_gmv_usd"],
            "in_original_source_top_100": row["in_original_source_top_100"],
        }
        for row in ledger
        if row["contribution_status"] == EXCLUDED
    ]
    mapped_targets = {row["target_id"] for row in ledger if row["target_id"]}
    for target_id in sorted(mapped_targets):
        if issue := target_issues.get(target_id):
            gmv = sum(
                (
                    s.ranking_gmv
                    for s in snapshot.rows
                    if links.get(s.model_id, {}).get("target_id") == target_id
                ),
                Decimal(0),
            )
            exclusions.append(
                {
                    "scope": "TARGET",
                    "model_id": "",
                    "target_id": target_id,
                    "reason_code": issue[0],
                    "reason_detail": issue[1],
                    "ranking_gmv_usd": money(gmv),
                    "in_original_source_top_100": "",
                }
            )

    review_priority = _review_priority(snapshot, ledger, reviews, register, original)
    reasons = Counter(row["reason_code"] for row in ledger if row["reason_code"])
    verified_targets = {target_id for target_id, issue in target_issues.items() if issue is None}
    counts = {
        "catalog_targets": len(targets),
        "verified_configuration_targets": len(verified_targets),
        "verified_targets_without_eligible_contributors": len(
            verified_targets - set(contributions)
        ),
        "eligible_sources": len(snapshot.rows),
        "contributing_sources": sum(len(s) for s in contributions.values()),
        "excluded_sources": len(exclusions) - sum(1 for e in exclusions if e["scope"] == "TARGET"),
        "qualifying_configurations": len(configurations),
        "selected_configurations": len(top),
        "requested_top_n": top_n,
        "shortfall": max(0, top_n - len(configurations)),
        "configurations_with_multiple_sources": sum(
            1 for c in configurations if int(c["contributing_source_count"]) > 1
        ),
        "excluded_targets": sum(1 for e in exclusions if e["scope"] == "TARGET"),
        "original_source_top_100_contributing": sum(
            1
            for row in ledger
            if row["in_original_source_top_100"] == "True"
            and row["contribution_status"] == CONTRIBUTING
        ),
        "open_register_entries": len(open_register),
        "review_priority_rows": len(review_priority),
        **{f"excluded_{code}": count for code, count in sorted(reasons.items())},
    }
    return VerifiedRanking(configurations, top, ledger, exclusions, review_priority, counts)


def _missing_core(reviews: Sequence[Mapping[str, object]]) -> str:
    """Core fields blank in every review row for the source (structured fields only)."""
    if not reviews:
        return ";".join(CORE_MATCH_COLUMNS)
    return ";".join(
        name
        for name in CORE_MATCH_COLUMNS
        if all(not str(review.get(name) or "").strip() for review in reviews)
    )


def _review_priority(
    snapshot: SalesSnapshot,
    ledger: Sequence[Mapping[str, str]],
    reviews: Mapping[str, Sequence[Mapping[str, object]]],
    register: Sequence[RegisterEntry],
    original: set[str],
) -> list[dict[str, str]]:
    """Evidence-review queue, highest source GMV first. Nothing is inferred or promoted."""
    by_id = {row.model_id: row for row in snapshot.rows}
    notes = defaultdict(list)
    for entry in register:
        if entry.status == "OPEN" and entry.source_model_id:
            notes[entry.source_model_id].append(entry.describe())
    rows = []
    for row in ledger:
        code = row["reason_code"]
        if code not in {
            "UNMAPPED",
            "NO_COMPLETE_REVIEW",
            "REVIEW_STATUS_UNRESOLVED",
            "REVIEW_CORE_DIFFERS",
            "REVIEW_CODE_DIFFERS",
            "REVIEW_MODE_MISMATCH",
            "KNOWN_UNRESOLVED_CONFLICT",
        }:
            continue
        source_reviews = reviews.get(row["model_id"], [])
        statuses = sorted({str(r["status"]) for r in source_reviews})
        missing = _missing_core(source_reviews)
        if code == "UNMAPPED":
            if not source_reviews:
                action = "No MATCH_REVIEW row: add a candidate and research its identity."
            elif "COMPLETE" in statuses:
                action = "COMPLETE review row without a mapping: check promotion and mapping."
            elif missing:
                action = f"Research the missing core fields from product evidence: {missing}."
            else:
                action = "Resolve the recorded review status before promotion."
        else:
            action = "Resolve the recorded conflict before this source can contribute."
        rows.append(
            {
                "scope": "UNMAPPED_ELIGIBLE_SOURCE"
                if code == "UNMAPPED"
                else "EXCLUDED_MAPPED_SOURCE",
                "model_id": row["model_id"],
                "target_id": row["target_id"],
                "shop_id": row["shop_id"],
                "item_id": row["item_id"],
                "item_name": row["item_name"],
                "shopee_model_name": row["shopee_model_name"],
                "ranking_gmv_usd": row["ranking_gmv_usd"],
                "mtd_gmv_usd": row["mtd_gmv_usd"],
                "in_original_source_top_100": row["in_original_source_top_100"],
                "review_statuses": ";".join(statuses),
                "review_row_numbers": ";".join(
                    str(r.get("source_row_number", "")) for r in source_reviews
                ),
                "missing_core_fields": missing,
                "known_register_notes": " | ".join(notes.get(row["model_id"], [])),
                "suggested_action": action,
            }
        )
    rows.sort(
        key=lambda r: (
            -by_id[r["model_id"]].ranking_gmv,
            -by_id[r["model_id"]].mtd_gmv,
            r["model_id"],
        )
    )
    # Open register entries for sources outside the eligible ranking stay visible, without GMV.
    listed = {r["model_id"] for r in rows}
    for entry in register:
        if entry.status == "OPEN" and entry.source_model_id and entry.source_model_id not in listed:
            source_reviews = reviews.get(entry.source_model_id, [])
            rows.append(
                {
                    "scope": "KNOWN_REGISTER_ENTRY_NOT_IN_RANKING",
                    "model_id": entry.source_model_id,
                    "target_id": entry.target_id,
                    "shop_id": "",
                    "item_id": "",
                    "item_name": "",
                    "shopee_model_name": "",
                    "ranking_gmv_usd": "",
                    "mtd_gmv_usd": "",
                    "in_original_source_top_100": str(entry.source_model_id in original),
                    "review_statuses": ";".join(sorted({str(r["status"]) for r in source_reviews})),
                    "review_row_numbers": ";".join(
                        str(r.get("source_row_number", "")) for r in source_reviews
                    ),
                    "missing_core_fields": _missing_core(source_reviews),
                    "known_register_notes": entry.describe(),
                    "suggested_action": (
                        "Not in this eligible sales snapshot, so no GMV effect now; resolve "
                        "before a later snapshot uses it."
                    ),
                }
            )
            listed.add(entry.source_model_id)
    for priority, row in enumerate(rows, start=1):
        row["priority"] = str(priority)
    return [{name: row[name] for name in REVIEW_PRIORITY_COLUMNS} for row in rows]


# Audit


def audit_verified_ranking(
    configurations: Sequence[Mapping[str, str]],
    top: Sequence[Mapping[str, str]],
    ledger: Sequence[Mapping[str, str]],
    snapshot: SalesSnapshot,
    target_master: TargetMasterResult,
    source_model_map: TableResult,
    *,
    top_n: int = DEFAULT_TOP_N,
) -> list[str]:
    """Independent checks of published outputs; an empty list means the audit passed."""
    issues: list[str] = []
    targets = {
        str(row["target_id"]): row for row in target_master.valid_rows.to_dict(orient="records")
    }
    links = {
        str(row["source_model_id"]): str(row["target_id"])
        for row in source_model_map.valid_rows.to_dict(orient="records")
    }
    sources = {row.model_id: row for row in snapshot.rows}

    ids = [row["target_id"] for row in configurations]
    if len(ids) != len(set(ids)):
        issues.append("Configuration ranking repeats a target_id.")
    if [row["rank"] for row in configurations] != [str(n) for n in range(1, len(ids) + 1)]:
        issues.append("Configuration ranks are not 1..n in order.")

    ledger_ids = [row["model_id"] for row in ledger]
    if len(ledger_ids) != len(set(ledger_ids)):
        issues.append("Contribution ledger repeats a source model_id.")
    if set(ledger_ids) != set(sources):
        issues.append("Contribution ledger does not cover exactly the eligible ranking sources.")
    contributing: dict[str, list[SourceRow]] = defaultdict(list)
    for row in ledger:
        if row["contribution_status"] != CONTRIBUTING:
            continue
        source = sources.get(row["model_id"])
        if source is None:
            issues.append(f"Contributor {row['model_id']} is not in the eligible ranking.")
            continue
        if (
            source.values["eligibility_status"] != "ELIGIBLE"
            or source.values["present_in_mtd"] != "True"
        ):
            issues.append(f"Contributor {row['model_id']} is not ELIGIBLE and present in MTD.")
        if links.get(row["model_id"]) != row["target_id"]:
            issues.append(f"Contributor {row['model_id']} is not mapped to {row['target_id']}.")
        for name in ("m_minus_1_gmv_usd", "mtd_gmv_usd", "ranking_gmv_usd"):
            if row[name] != source.values[name]:
                issues.append(f"Contributor {row['model_id']} {name} differs from the ranking.")
        contributing[row["target_id"]].append(source)

    if set(ids) != set(contributing):
        issues.append("Ranked targets and contributing targets differ.")
    keys = []
    for row in configurations:
        target_id = row["target_id"]
        target = targets.get(target_id)
        if target is None:
            issues.append(f"{target_id} is not a valid TARGET_MASTER row.")
            continue
        if str(target["status"]) != "ACTIVE":
            issues.append(f"{target_id} is not ACTIVE.")
        if effective_tracking_mode(target.get("tracking_mode")) != CONFIGURATION:
            issues.append(f"{target_id} is not a CONFIGURATION target.")
        if core_key(target)[0] is None:
            issues.append(f"{target_id} has an incomplete core identity.")
        if any(row[name] != str(target[name]) for name in CORE_MATCH_COLUMNS):
            issues.append(f"{target_id} core values differ from TARGET_MASTER.")
        members = contributing.get(target_id, [])
        m1 = sum((s.m_minus_1_gmv for s in members), Decimal(0))
        mtd = sum((s.mtd_gmv for s in members), Decimal(0))
        try:
            published = (
                Decimal(row["m_minus_1_gmv_usd"]),
                Decimal(row["mtd_gmv_usd"]),
                Decimal(row["ranking_gmv_usd"]),
            )
        except InvalidOperation:
            issues.append(f"{target_id} has a malformed GMV total.")
            continue
        if published != (m1, mtd, m1 + mtd):
            issues.append(f"{target_id} GMV totals do not equal its contributors' sums.")
        if row["contributing_source_count"] != str(len(members)):
            issues.append(f"{target_id} contributing_source_count is wrong.")
        expected = {s.model_id for s in members}
        if set(filter(None, row["contributing_source_model_ids"].split(";"))) != expected:
            issues.append(f"{target_id} contributing_source_model_ids are wrong.")
        keys.append((-published[2], -published[1], target_id))
    if keys != sorted(keys):
        issues.append("Configuration ranking is not in deterministic order.")
    if [dict(row) for row in top] != [dict(row) for row in configurations[:top_n]]:
        issues.append(f"Top list is not the first {top_n} rows of the full ranking.")
    return issues
