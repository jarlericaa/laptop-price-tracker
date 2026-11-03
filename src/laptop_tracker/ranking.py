"""Model-level M-1/MTD GMV aggregation, price selection, and deterministic ranking.

Owner decision 2026-11-03 (superseding the earlier rule that a model needs an MTD record):
the ranking pool is the ``ELIGIBLE`` models present in **either** reporting month. A month
without a record is not an observed zero and not proof that the listing is inactive: the
presence flags ``present_in_m_minus_1`` / ``present_in_mtd`` and the statuses
``m_minus_1_status`` / ``mtd_status`` keep the underlying missing-data fact, and a missing
month contributes ``0`` to ``ranking_gmv_usd`` only as labelled ranking arithmetic
(``NO_RECORD_TREATED_AS_ZERO``). The shown price is the model's own latest valid price: the
MTD record's, else the M-1 record's, with its period, record date and basis kept.
"""

from __future__ import annotations

import pandas as pd

TOP_100_COLUMNS = [
    "rank",
    "run_id",
    "extract_date",
    "m_minus_1_month",
    "mtd_month",
    "source_key",
    "shop_id",
    "item_id",
    "model_id",
    "item_name",
    "shopee_model_name",
    "m_minus_1_gmv_usd",
    "mtd_gmv_usd",
    "ranking_gmv_usd",
    "shopee_current_price_php",
    "eligibility_status",
    "eligibility_reason",
    "present_in_mtd",
]
MODEL_SUMMARY_COLUMNS = [
    *TOP_100_COLUMNS[1:],
    "price_status",
    "mtd_price_values",
    "matched_include_rule",
    "matched_exclude_rule",
    # Added 2026-11-03 (appended so earlier consumers keep their columns).
    "present_in_m_minus_1",
    "m_minus_1_status",
    "mtd_status",
    "price_period_month",
    "price_record_date",
    "price_basis",
    "m_minus_1_price_values",
]
PRESENT = "PRESENT"
NO_RECORD_AS_ZERO = "NO_RECORD_TREATED_AS_ZERO"
MTD_PRICE = "MTD_PRICE"
M_MINUS_1_PRICE = "M_MINUS_1_PRICE"


def _grouped_distinct_join(frame: pd.DataFrame, column: str) -> pd.Series:
    """Join each model's distinct non-empty string values in lexical order."""
    values = frame[["model_id", column]].copy()
    values[column] = values[column].astype(str)
    values = values.loc[values[column].ne("")].drop_duplicates()
    values = values.sort_values(["model_id", column], kind="stable")
    return values.groupby("model_id", sort=True, dropna=False)[column].agg(";".join)


def _period_prices(
    scoped: pd.DataFrame, mask: pd.Series, model_ids: pd.Index
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """Per model for one period: ``(distinct price count, the price if one, joined values, the
    latest record date of the priced rows)``. Prices are never averaged."""
    if "period_date" not in scoped.columns:  # fixtures built without source dates
        scoped = scoped.assign(period_date="")
    rows = scoped.loc[mask, ["model_id", "_price_numeric", "period_date"]].dropna(
        subset=["_price_numeric"]
    )
    rows = rows.assign(_price_numeric=rows["_price_numeric"].astype(float))
    distinct = rows[["model_id", "_price_numeric"]].drop_duplicates()
    distinct = distinct.sort_values(["model_id", "_price_numeric"], kind="stable")
    grouped = distinct.groupby("model_id", sort=True, dropna=False)["_price_numeric"]
    counts = grouped.size().reindex(model_ids, fill_value=0)
    first = grouped.first().reindex(model_ids)
    joined = (
        distinct.assign(_s=distinct["_price_numeric"].astype(str))
        .groupby("model_id", sort=True, dropna=False)["_s"]
        .agg(";".join)
        .reindex(model_ids)
        .fillna("")
    )
    dated = rows.assign(_date=pd.to_datetime(rows["period_date"], errors="coerce"))
    latest = dated.groupby("model_id", sort=True, dropna=False)["_date"].max()
    latest = latest.dt.strftime("%Y-%m-%d").reindex(model_ids).fillna("")
    return counts, first, joined, latest


def aggregate_models(
    frame: pd.DataFrame,
    m_minus_1_month: str,
    mtd_month: str,
    extract_date: str,
) -> pd.DataFrame:
    """Aggregate the two current calendar periods at model level without averaging prices."""
    scoped = frame.loc[frame["period_month"].isin([m_minus_1_month, mtd_month])].copy()
    if scoped.empty:
        return pd.DataFrame(columns=MODEL_SUMMARY_COLUMNS)

    # Keep the model index in the same sorted order as the previous groupby loop.
    model_ids = scoped.groupby("model_id", sort=True, dropna=False).size().index
    m1_mask = scoped["period_month"].eq(m_minus_1_month)
    mtd_mask = scoped["period_month"].eq(mtd_month)

    # Aggregate each period once across the whole frame instead of filtering a new
    # DataFrame for every model.
    m1_gmv = (
        scoped.loc[m1_mask]
        .groupby("model_id", sort=True, dropna=False)["_gmv_numeric"]
        .sum()
        .reindex(model_ids, fill_value=0)
        .astype(float)
    )
    mtd_gmv = (
        scoped.loc[mtd_mask]
        .groupby("model_id", sort=True, dropna=False)["_gmv_numeric"]
        .sum()
        .reindex(model_ids, fill_value=0)
        .astype(float)
    )

    # MTD is the preferred representative period; within it, the earliest source
    # row wins. Models without MTD fall back to their earliest M-1 row.
    by_source_row = scoped.sort_values("source_row_number", kind="stable")
    m1_representatives = (
        by_source_row.loc[m1_mask]
        .drop_duplicates("model_id", keep="first")
        .set_index("model_id")
    )
    mtd_representatives = (
        by_source_row.loc[mtd_mask]
        .drop_duplicates("model_id", keep="first")
        .set_index("model_id")
    )
    representatives = pd.concat([m1_representatives, mtd_representatives])
    representatives = representatives.loc[
        ~representatives.index.duplicated(keep="last")
    ].reindex(model_ids)

    # A model with inconsistent listing classifications remains in REVIEW. Rule
    # and reason fields retain the prior sorted, distinct, semicolon-joined form.
    status_values = scoped[["model_id", "eligibility_status"]].copy()
    status_values["eligibility_status"] = status_values["eligibility_status"].astype(str)
    status_values = status_values.drop_duplicates()
    status_groups = status_values.groupby("model_id", sort=True, dropna=False)[
        "eligibility_status"
    ]
    status_counts = status_groups.size().reindex(model_ids, fill_value=0)
    statuses = status_groups.first().reindex(model_ids).astype(object)
    status_conflict = status_counts.gt(1)
    statuses.loc[status_conflict] = "REVIEW"

    reasons = _grouped_distinct_join(scoped, "eligibility_reason").reindex(model_ids).fillna("")
    reasons.loc[status_conflict] = (
        "Conflicting listing-level eligibility classifications for the same model."
    )
    include_rules = _grouped_distinct_join(scoped, "matched_include_rule").reindex(
        model_ids
    ).fillna("")
    exclude_rules = _grouped_distinct_join(scoped, "matched_exclude_rule").reindex(
        model_ids
    ).fillna("")

    # The model's own latest valid price: the MTD record's when it has a price, else the M-1
    # record's. Distinct numeric prices are never averaged; more than one value in the period
    # that supplies the price is a conflict (no fallback to the older period) and none at all in
    # either period is PRICE_MISSING. The record date is the source row's own period date.
    mtd_count, mtd_first, price_values, mtd_date = _period_prices(scoped, mtd_mask, model_ids)
    m1_count, m1_first, m1_price_values, m1_date = _period_prices(scoped, m1_mask, model_ids)
    current, status_values_by_model, basis, price_month, price_date = [], [], [], [], []
    for model_id in model_ids:
        for count, first, kind, month, date_text in (
            (mtd_count, mtd_first, MTD_PRICE, mtd_month, mtd_date),
            (m1_count, m1_first, M_MINUS_1_PRICE, m_minus_1_month, m1_date),
        ):
            if count.loc[model_id] == 0:
                continue
            ok = count.loc[model_id] == 1
            current.append(first.loc[model_id] if ok else pd.NA)
            status_values_by_model.append("PRICE_OK" if ok else "PRICE_CONFLICT")
            basis.append(kind)
            price_month.append(month)
            price_date.append(date_text.loc[model_id])
            break
        else:
            current.append(pd.NA)
            status_values_by_model.append("PRICE_MISSING")
            basis.append("")
            price_month.append("")
            price_date.append("")
    current_prices = pd.Series(current, index=model_ids, dtype=object)

    present_in_mtd = (
        scoped.loc[mtd_mask].groupby("model_id", sort=True, dropna=False).size().reindex(
            model_ids, fill_value=0
        )
        > 0
    )
    result = representatives[
        [
            "run_id",
            "source_key",
            "shop_id",
            "item_id",
            "item_name",
            "shopee_model_name",
        ]
    ].copy()
    result.insert(0, "model_id", pd.Series(model_ids.astype(str), index=model_ids))
    result.insert(1, "extract_date", extract_date)
    result.insert(2, "m_minus_1_month", m_minus_1_month)
    result.insert(3, "mtd_month", mtd_month)
    result["m_minus_1_gmv_usd"] = m1_gmv
    result["mtd_gmv_usd"] = mtd_gmv
    result["ranking_gmv_usd"] = m1_gmv + mtd_gmv
    result["shopee_current_price_php"] = current_prices
    result["price_status"] = pd.Series(status_values_by_model, index=model_ids)
    result["mtd_price_values"] = price_values
    result["eligibility_status"] = statuses
    result["eligibility_reason"] = reasons
    result["matched_include_rule"] = include_rules
    result["matched_exclude_rule"] = exclude_rules
    result["present_in_mtd"] = present_in_mtd
    present_in_m1 = (
        scoped.loc[m1_mask].groupby("model_id", sort=True, dropna=False).size().reindex(
            model_ids, fill_value=0
        )
        > 0
    )
    result["present_in_m_minus_1"] = present_in_m1
    result["m_minus_1_status"] = present_in_m1.map({True: PRESENT, False: NO_RECORD_AS_ZERO})
    result["mtd_status"] = present_in_mtd.map({True: PRESENT, False: NO_RECORD_AS_ZERO})
    result["price_period_month"] = pd.Series(price_month, index=model_ids)
    result["price_record_date"] = pd.Series(price_date, index=model_ids)
    result["price_basis"] = pd.Series(basis, index=model_ids)
    result["m_minus_1_price_values"] = m1_price_values
    return result.reindex(columns=MODEL_SUMMARY_COLUMNS).reset_index(drop=True)


def rank_eligible_models(model_summary: pd.DataFrame) -> pd.DataFrame:
    """Return the eligible models present in either month in the deterministic order.

    A model with an M-1 record and no MTD record is ranked (owner decision 2026-11-03); its missing
    month adds 0 to ``ranking_gmv_usd`` and stays flagged in ``mtd_status``.
    """
    if model_summary.empty:
        empty = model_summary.copy()
        empty.insert(0, "rank", pd.Series(dtype="int64"))
        return empty
    qualified = model_summary.loc[
        model_summary["eligibility_status"].eq("ELIGIBLE")
        & (
            model_summary["present_in_mtd"].astype(bool)
            | model_summary["present_in_m_minus_1"].astype(bool)
        )
    ].copy()
    qualified = qualified.sort_values(
        ["ranking_gmv_usd", "mtd_gmv_usd", "model_id"],
        ascending=[False, False, True],
        kind="stable",
    ).reset_index(drop=True)
    qualified.insert(0, "rank", range(1, len(qualified) + 1))
    return qualified


def top_100(ranking: pd.DataFrame) -> pd.DataFrame:
    result = ranking.head(100).copy()
    for column in TOP_100_COLUMNS:
        if column not in result.columns:
            result[column] = pd.NA
    return result[
        TOP_100_COLUMNS + [column for column in result.columns if column not in TOP_100_COLUMNS]
    ]


def review_queue(model_summary: pd.DataFrame, decisions: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "model_id",
        "shop_id",
        "item_id",
        "item_name",
        "shopee_model_name",
        "m_minus_1_gmv_usd",
        "mtd_gmv_usd",
        "ranking_gmv_usd",
        "shopee_current_price_php",
        "eligibility_status",
        "eligibility_reason",
        "matched_keyword",
        "review_decision",
        "review_notes",
    ]
    queue = model_summary.loc[model_summary["eligibility_status"].eq("REVIEW")].copy()
    if queue.empty:
        return pd.DataFrame(columns=columns)
    decision_lookup = decisions.set_index("model_id") if not decisions.empty else pd.DataFrame()
    queue["matched_keyword"] = queue.apply(
        lambda row: ";".join(
            value for value in [row["matched_include_rule"], row["matched_exclude_rule"]] if value
        ),
        axis=1,
    )
    queue["review_decision"] = (
        queue["model_id"]
        .map(
            decision_lookup["decision"] if not decision_lookup.empty else pd.Series(dtype="string")
        )
        .fillna("")
    )
    queue["review_notes"] = (
        queue["model_id"]
        .map(decision_lookup["notes"] if not decision_lookup.empty else pd.Series(dtype="string"))
        .fillna("")
    )
    queue = queue.sort_values(
        ["ranking_gmv_usd", "mtd_gmv_usd", "model_id"],
        ascending=[False, False, True],
        kind="stable",
    )
    return queue[columns].reset_index(drop=True)


def excluded_listings(model_summary: pd.DataFrame) -> pd.DataFrame:
    return (
        model_summary.loc[model_summary["eligibility_status"].eq("EXCLUDED")]
        .sort_values(
            ["ranking_gmv_usd", "mtd_gmv_usd", "model_id"],
            ascending=[False, False, True],
            kind="stable",
        )
        .reset_index(drop=True)
    )
