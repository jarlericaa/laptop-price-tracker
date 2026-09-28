"""Rule-based and manually overridable laptop eligibility classification."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

RULE_COLUMNS = ["rule_type", "rule", "description"]
DECISION_COLUMNS = ["model_id", "decision", "reason", "reviewer", "reviewed_at", "notes"]
VALID_DECISIONS = {"ELIGIBLE", "EXCLUDED"}
ACCESSORY_TERMS = ("bag", "sleeve", "charger")


@dataclass(frozen=True)
class EligibilityRules:
    include: tuple[str, ...]
    exclude: tuple[str, ...]


def normalize_text(value: object) -> str:
    text = "" if value is None else str(value)
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text.lower())).strip()


def load_rules(path: str | Path) -> EligibilityRules:
    rules = pd.read_csv(path, dtype="string", keep_default_na=False)
    missing = [column for column in RULE_COLUMNS if column not in rules.columns]
    if missing:
        raise ValueError(f"Eligibility rules are missing columns: {', '.join(missing)}")
    normalized = rules.assign(
        rule_type=rules["rule_type"].str.strip().str.upper(),
        rule=rules["rule"].map(normalize_text),
    )
    invalid = set(normalized["rule_type"]) - {"INCLUDE", "EXCLUDE"}
    if invalid:
        raise ValueError(f"Unsupported eligibility rule types: {', '.join(sorted(invalid))}")
    return EligibilityRules(
        include=tuple(
            value for value in normalized.loc[normalized.rule_type == "INCLUDE", "rule"] if value
        ),
        exclude=tuple(
            value for value in normalized.loc[normalized.rule_type == "EXCLUDE", "rule"] if value
        ),
    )


def load_manual_decisions(path: str | Path) -> pd.DataFrame:
    file_path = Path(path)
    if not file_path.exists():
        return pd.DataFrame(columns=DECISION_COLUMNS)
    decisions = pd.read_csv(file_path, dtype="string", keep_default_na=False)
    missing = [column for column in DECISION_COLUMNS if column not in decisions.columns]
    if missing:
        raise ValueError(f"Eligibility decisions are missing columns: {', '.join(missing)}")
    decisions = decisions[DECISION_COLUMNS].copy()
    decisions["model_id"] = decisions["model_id"].str.strip()
    decisions["decision"] = decisions["decision"].str.strip().str.upper()
    populated = decisions["model_id"].ne("")
    invalid = set(decisions.loc[populated, "decision"]) - VALID_DECISIONS
    if invalid:
        raise ValueError(f"Unsupported manual decisions: {', '.join(sorted(invalid))}")
    return decisions.loc[populated].drop_duplicates("model_id", keep="last").reset_index(drop=True)


def _matched_rules(text: str, candidates: tuple[str, ...]) -> list[str]:
    padded = f" {text} "
    return [rule for rule in candidates if f" {rule} " in padded]


def _is_included_accessory(text: str, rule: str) -> bool:
    """Recognize accessory mentions that describe bundle contents, not the product itself."""
    if not any(term in rule.split() for term in ACCESSORY_TERMS):
        return False
    contexts = (
        f"{rule} included",
        f"includes {rule}",
        f"with {rule}",
        f"free {rule}",
    )
    return any(context in text for context in contexts)


def classify_listing(
    item_name: object,
    model_name: object,
    rules: EligibilityRules,
    manual_decision: dict[str, str] | None = None,
) -> dict[str, str]:
    """Classify one listing. A valid manual decision always takes precedence."""
    if manual_decision:
        decision = manual_decision["decision"]
        return {
            "eligibility_status": decision,
            "eligibility_reason": f"Manual decision: {manual_decision.get('reason', '')}".rstrip(),
            "matched_include_rule": "",
            "matched_exclude_rule": "",
        }
    text = normalize_text(f"{item_name or ''} {model_name or ''}")
    include_matches = _matched_rules(text, rules.include)
    exclude_matches = [
        rule
        for rule in _matched_rules(text, rules.exclude)
        if not _is_included_accessory(text, rule)
    ]
    # An accessory phrase such as "laptop charger" is a definitive product description;
    # its incidental generic word "laptop" must not turn it into a manual-review laptop.
    # A specific laptop-family match plus an exclusion remains an ambiguous REVIEW case.
    generic_laptop_only = set(include_matches).issubset({"laptop"})
    if exclude_matches and generic_laptop_only:
        status, reason = "EXCLUDED", "Product-level exclusion rule matched."
    elif include_matches and exclude_matches:
        status, reason = "REVIEW", "Both inclusion and exclusion rules matched."
    elif exclude_matches:
        status, reason = "EXCLUDED", "Exclusion rule matched."
    elif include_matches:
        status, reason = "ELIGIBLE", "Inclusion rule matched."
    else:
        status, reason = "REVIEW", "Insufficient evidence for automatic classification."
    return {
        "eligibility_status": status,
        "eligibility_reason": reason,
        "matched_include_rule": ";".join(include_matches),
        "matched_exclude_rule": ";".join(exclude_matches),
    }


def apply_eligibility(
    frame: pd.DataFrame, rules: EligibilityRules, decisions: pd.DataFrame
) -> pd.DataFrame:
    result = frame.copy()
    decision_map = {
        str(row.model_id): row._asdict()
        for row in decisions.itertuples(index=False)
        if row.model_id
    }
    classifications = [
        classify_listing(
            row.item_name,
            row.shopee_model_name,
            rules,
            decision_map.get(str(row.model_id)),
        )
        for row in result.itertuples(index=False)
    ]
    classified = pd.DataFrame(classifications, index=result.index)
    return result.join(classified)
