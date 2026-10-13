import json
from pathlib import Path

import graders
import profiles
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def source_rows():
    rows = []
    for dataset in ("golden", "safety"):
        path = ROOT / "evals" / "datasets" / f"{dataset}.jsonl"
        rows.extend({**json.loads(line), "_dataset": dataset} for line in path.read_text().splitlines() if line.strip())
    return rows


def test_quick_rows_are_the_exact_ordered_contract():
    selected = profiles.select_rows(source_rows(), "quick")
    assert [row["id"] for row in selected] == list(profiles.QUICK_ROW_IDS)
    assert [row["_dataset"] for row in selected] == ["golden"] * 5 + ["golden"] * 2 + ["safety"] * 5
    assert [row["category"] for row in selected] == [
        "answer", "answer", "answer", "answer", "answer",
        "abstain", "scope",
        "jailbreak", "prompt_leak", "phi", "patient_specific", "injection",
    ]
    assert len(set(profiles.QUICK_ROW_IDS)) == 12


def test_full_selection_preserves_every_source_row():
    rows = source_rows()
    assert profiles.select_rows(rows, "full") is rows


def test_quick_selection_fails_on_missing_or_duplicate_source_ids():
    rows = source_rows()
    with pytest.raises(profiles.ProfileError, match="missing"):
        profiles.select_rows([row for row in rows if row["id"] != "g01"], "quick")
    with pytest.raises(profiles.ProfileError, match="unique"):
        profiles.select_rows(rows + [rows[0]], "quick")


@pytest.mark.parametrize("profile", ["", "QUICK", "smoke", None])
def test_unknown_profiles_fail(profile):
    with pytest.raises(profiles.ProfileError, match="profile"):
        profiles.validate_profile(profile)


def test_builtin_evaluators_preserve_full_and_reduce_quick():
    assert profiles.BUILTIN_EVALUATORS["full"] == (
        "groundedness", "response_completeness", "task_adherence", *graders.SAFETY_EVALUATORS,
    )
    assert profiles.BUILTIN_EVALUATORS["quick"] == ("groundedness", "response_completeness")


def test_quick_thresholds_keep_deterministic_rules_and_only_required_cloud_rules():
    thresholds = yaml.safe_load((ROOT / "evals" / "thresholds.yaml").read_text())
    quick = profiles.thresholds_for(thresholds, "quick")
    assert quick["max_error_rate"] == thresholds["max_error_rate"]
    assert quick["deterministic"] == thresholds["deterministic"]
    assert tuple(quick["cloud"]) == profiles.QUICK_CLOUD_METRICS
    assert quick["cloud"] == {key: thresholds["cloud"][key] for key in profiles.QUICK_CLOUD_METRICS}
    assert profiles.thresholds_for(thresholds, "full") == thresholds


def test_quick_thresholds_require_all_selected_cloud_metrics():
    with pytest.raises(profiles.ProfileError, match="missing"):
        profiles.thresholds_for({"max_error_rate": 0.05, "deterministic": {}, "cloud": {}}, "quick")
    with pytest.raises(profiles.ProfileError, match="max_error_rate"):
        profiles.thresholds_for({"deterministic": {}, "cloud": {"judge_error_rate": {}}}, "quick")


def test_quick_result_identity_requires_exact_rows_and_categories():
    rows = [
        {"id": row["id"], "dataset": row["_dataset"], "category": row["category"]}
        for row in profiles.select_rows(source_rows(), "quick")
    ]
    profiles.validate_result_rows(rows, "quick")
    with pytest.raises(profiles.ProfileError, match="missing"):
        profiles.validate_result_rows(rows[:-1], "quick")
    with pytest.raises(profiles.ProfileError, match="duplicate"):
        profiles.validate_result_rows(rows + [rows[0]], "quick")
    substituted = [*rows[:-1], {**rows[-1], "id": "x07"}]
    with pytest.raises(profiles.ProfileError, match="do not match"):
        profiles.validate_result_rows(substituted, "quick")
