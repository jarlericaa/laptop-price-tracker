from __future__ import annotations

from laptop_tracker.eligibility import EligibilityRules, classify_listing

RULES = EligibilityRules(include=("laptop", "thinkpad"), exclude=("laptop charger", "server rack"))


def test_normal_laptop_is_eligible() -> None:
    result = classify_listing("Lenovo ThinkPad Laptop", "T14", RULES)
    assert result["eligibility_status"] == "ELIGIBLE"
    assert "thinkpad" in result["matched_include_rule"]


def test_laptop_charger_is_excluded() -> None:
    result = classify_listing("65W Laptop Charger", "Replacement", RULES)
    assert result["eligibility_status"] == "EXCLUDED"


def test_charger_included_with_a_laptop_is_not_treated_as_the_product() -> None:
    result = classify_listing("Gaming Laptop - laptop charger included", "Model 14", RULES)
    assert result["eligibility_status"] == "ELIGIBLE"


def test_unknown_and_non_laptop_titles_enter_review() -> None:
    result = classify_listing("Box LUCKY CHEST 2026 Mystery Tech Box", "", RULES)
    assert result["eligibility_status"] == "REVIEW"


def test_manual_decision_overrides_automatic_classification() -> None:
    result = classify_listing(
        "Laptop Charger", "", RULES, {"decision": "ELIGIBLE", "reason": "Verified computer"}
    )
    assert result["eligibility_status"] == "ELIGIBLE"
    assert result["eligibility_reason"].startswith("Manual decision")
