"""Standing owner rule for conflicting duplicates: keep the latest sheet row.

The owner rule (2026-10-22, standing since 2026-11-03) applies to a key whose records share one
conflict date but differ in values: the record on the latest current sheet row is kept. It never
touches model-identity conflicts, keys with several conflict dates, or keys whose records cannot
all be located in the resolution log; those are reported and stay quarantined for human review.
"""

from __future__ import annotations

import pandas as pd

from laptop_tracker.data_quality import DUPLICATE_DECISION_COLUMNS, DUPLICATE_KEY_COLUMNS

RULE_NAME = "keep the latest sheet row"
REVIEWER = "tj.noval (standing owner rule: keep the latest sheet row)"
REASON = (
    "Same variation recorded more than once with conflicting values and the same date; standing "
    "owner rule (first stated 2026-10-22, made standing 2026-11-03) is to keep the latest sheet "
    "row. Applied by apply_latest_row_decisions.py at the row numbers of the run that quarantined "
    "the key; the earlier row is not used."
)


def propose_latest_row_decisions(
    review: pd.DataFrame, resolution_log: pd.DataFrame, reviewed_at: str
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (decisions, skipped) for the keys of ``review`` that have no decision yet."""
    open_rows = review[review["decision"].fillna("").astype(str).str.strip().eq("")]
    log = resolution_log.assign(
        current_source_row_number=pd.to_numeric(resolution_log["current_source_row_number"])
    )
    decisions: list[dict[str, object]] = []
    skipped: list[dict[str, str]] = []
    for _, row in open_rows.iterrows():
        key = {column: str(row[column]) for column in DUPLICATE_KEY_COLUMNS}
        label = "/".join(key.values())
        fingerprints = str(row["record_fingerprints"]).split(";")
        if ";" in str(row["conflict_dates"]):
            skipped.append({**key, "reason": "Several conflict dates: not covered by the rule."})
            continue
        mask = pd.Series(True, index=log.index)
        for column, value in key.items():
            mask &= log[column].astype(str).eq(value)
        located = log[mask & log["record_fingerprint"].isin(fingerprints)]
        if set(located["record_fingerprint"]) != set(fingerprints) or located[
            "current_source_row_number"
        ].duplicated().any():
            skipped.append(
                {**key, "reason": f"Records of {label} cannot be placed on distinct sheet rows."}
            )
            continue
        latest = located.loc[located["current_source_row_number"].idxmax(), "record_fingerprint"]
        decisions.append(
            {column: row[column] for column in DUPLICATE_KEY_COLUMNS}
            | {
                "conflict_dates": row["conflict_dates"],
                "differing_fields": row["differing_fields"],
                "record_fingerprints": row["record_fingerprints"],
                "fingerprint_version": row["fingerprint_version"],
                "decision": "KEEP_RECORD",
                "selected_record_fingerprint": latest,
                "reviewer": REVIEWER,
                "reviewed_at": reviewed_at,
                "reason": REASON,
            }
        )
    return (
        pd.DataFrame(decisions, columns=DUPLICATE_DECISION_COLUMNS),
        pd.DataFrame(skipped, columns=[*DUPLICATE_KEY_COLUMNS, "reason"]),
    )


def merge_decisions(existing: pd.DataFrame, proposed: pd.DataFrame) -> pd.DataFrame:
    """Replace any recorded decision for a proposed key (it was invalidated) and add the rest."""
    if proposed.empty:
        return existing
    keys = set(map(tuple, proposed[DUPLICATE_KEY_COLUMNS].astype(str).values))
    kept = existing[
        [tuple(r) not in keys for r in existing[DUPLICATE_KEY_COLUMNS].astype(str).values]
    ]
    combined = pd.concat([kept, proposed], ignore_index=True)
    return combined.reindex(columns=DUPLICATE_DECISION_COLUMNS)
