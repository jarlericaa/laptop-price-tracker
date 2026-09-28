"""Model-level M-1/MTD GMV aggregation, price selection, and deterministic ranking."""

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
]


def _grouped_distinct_join(frame: pd.DataFrame, column: str) -> pd.Series:
    """Join each model's distinct non-empty string values in lexical order."""
    values = frame[["model_id", column]].copy()
    values[column] = values[column].astype(str)
    values = values.loc[values[column].ne("")].drop_duplicates()
    values = values.sort_values(["model_id", column], kind="stable")
    return values.groupby("model_id", sort=True, dropna=False)[column].agg(";".join)


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

    # Distinct numeric prices are ordered numerically, matching sorted(set(...));
    # no averaging is performed and more than one value marks a conflict.
    price_rows = scoped.loc[mtd_mask, ["model_id", "_price_numeric"]].dropna(
        subset=["_price_numeric"]
    )
    price_rows = price_rows.assign(_price_numeric=price_rows["_price_numeric"].astype(float))
    price_rows = price_rows.drop_duplicates().sort_values(
        ["model_id", "_price_numeric"], kind="stable"
    )
    prices_by_model = price_rows.groupby("model_id", sort=True, dropna=False)[
        "_price_numeric"
    ]
    price_counts = prices_by_model.size().reindex(model_ids, fill_value=0)
    first_prices = prices_by_model.first().reindex(model_ids)
    current_prices = pd.Series(
        [
            first_prices.loc[model_id] if price_counts.loc[model_id] == 1 else pd.NA
            for model_id in model_ids
        ],
        index=model_ids,
        dtype=object,
    )
    price_strings = price_rows.assign(_price_string=price_rows["_price_numeric"].astype(str))
    price_values = (
        price_strings.groupby("model_id", sort=True, dropna=False)["_price_string"]
        .agg(";".join)
        .reindex(model_ids)
        .fillna("")
    )

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
    result["price_status"] = price_counts.gt(1).map(
        {True: "PRICE_CONFLICT", False: "PRICE_OK"}
    )
    result["mtd_price_values"] = price_values
    result["eligibility_status"] = statuses
    result["eligibility_reason"] = reasons
    result["matched_include_rule"] = include_rules
    result["matched_exclude_rule"] = exclude_rules
    result["present_in_mtd"] = present_in_mtd
    return result.reindex(columns=MODEL_SUMMARY_COLUMNS).reset_index(drop=True)


def rank_eligible_models(model_summary: pd.DataFrame) -> pd.DataFrame:
    """Return only active, eligible models in the deterministic publication order."""
    if model_summary.empty:
        empty = model_summary.copy()
        empty.insert(0, "rank", pd.Series(dtype="int64"))
        return empty
    qualified = model_summary.loc[
        model_summary["eligibility_status"].eq("ELIGIBLE")
        & model_summary["present_in_mtd"].astype(bool)
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
