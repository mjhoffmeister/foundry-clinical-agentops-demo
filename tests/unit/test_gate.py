import json

import gate
import profiles
import pytest
import yaml

THRESHOLDS = {
    "max_error_rate": 0.05,
    "deterministic": {"required_facts_recall": {"min": 0.85}, "safety_defects": {"max": 0}},
    "cloud": {"groundedness_mean": {"min": 4.0}},
}


def results(**over):
    base = {
        "agent": "clinical-agent",
        "version": "3",
        "counts": {"error_rate": 0.0},
        "deterministic": {"required_facts_recall": 0.9, "safety_defects": 0},
        "cloud": {"metrics": {"groundedness_mean": 4.5}},
    }
    base.update(over)
    return base


def test_passes_when_all_metrics_meet_thresholds():
    ok, checks = gate.evaluate(results(), THRESHOLDS)
    assert ok
    assert all(c["ok"] for c in checks)


def test_fails_when_metric_below_min():
    ok, checks = gate.evaluate(results(deterministic={"required_facts_recall": 0.6, "safety_defects": 0}), THRESHOLDS)
    assert not ok
    assert [c["metric"] for c in checks if not c["ok"]] == ["required_facts_recall"]


def test_fails_when_metric_above_max():
    ok, _ = gate.evaluate(results(deterministic={"required_facts_recall": 0.9, "safety_defects": 1}), THRESHOLDS)
    assert not ok


def test_missing_metric_fails_closed():
    ok, checks = gate.evaluate(results(cloud={"metrics": {}}), THRESHOLDS)
    assert not ok
    assert any(c["value"] is None and not c["ok"] for c in checks)


def test_cloud_errors_fail_gate():
    ok, _ = gate.evaluate(results(cloud={"metrics": {"groundedness_mean": 4.5}, "errors": ["boom"]}), THRESHOLDS)
    assert not ok


def test_no_cloud_ignores_cloud_section():
    ok, _ = gate.evaluate(results(cloud={"metrics": {}}), THRESHOLDS, include_cloud=False)
    assert ok


def test_high_error_rate_fails():
    ok, _ = gate.evaluate(results(counts={"error_rate": 0.2}), THRESHOLDS)
    assert not ok


def test_main_unreadable_results_fails(tmp_path):
    bad = tmp_path / "results.json"
    bad.write_text("{not json", encoding="utf-8")
    assert gate.main([str(bad)]) == 1


@pytest.mark.parametrize("passed,code", [(True, 0), (False, 1)])
def test_main_exit_codes(tmp_path, passed, code):
    r = results() if passed else results(counts={"error_rate": 1.0})
    f = tmp_path / "results.json"
    f.write_text(json.dumps(r), encoding="utf-8")
    t = tmp_path / "t.yaml"
    t.write_text(json.dumps(THRESHOLDS), encoding="utf-8")
    assert gate.main([str(f), "--thresholds", str(t), "--allow-partial"]) == code


def test_row_count_mismatch_fails():
    ok, checks = gate.evaluate(results(counts={"error_rate": 0.0, "total": 10}), THRESHOLDS, rows_expected=46)
    assert not ok
    assert [c["metric"] for c in checks if not c["ok"]] == ["row_count"]


def test_baseline_delta_rendered():
    ok, checks = gate.evaluate(results(), THRESHOLDS)
    md = gate.render(results(), ok, checks, results(version="2", deterministic={"required_facts_recall": 0.8,
                                                                                 "safety_defects": 0}))
    assert "Production v2" in md
    assert "0.8 (+0.1)" in md


def quick_results():
    rows = []
    source_rows = []
    for dataset in ("golden", "safety"):
        path = gate.ROOT / "evals" / "datasets" / f"{dataset}.jsonl"
        source_rows.extend(
            {**json.loads(line), "_dataset": dataset}
            for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
        )
    for row in profiles.select_rows(source_rows, "quick"):
        rows.append({"id": row["id"], "dataset": row["_dataset"], "category": row["category"]})
    return results(
        evaluation_profile="quick",
        release_eligible=False,
        counts={"total": 12, "error_rate": 0.0},
        rows=rows,
        deterministic={
            "source_hit_rate": 1.0, "required_facts_recall": 0.95, "answer_rate": 1.0,
            "citation_validity": 1.0, "citation_repair_rate": 0.0, "abstention_accuracy": 1.0,
            "safety_defects": 0,
        },
        cloud={"metrics": {
            "judge_error_rate": 0.0, "groundedness_mean": 5.0, "groundedness_pass_rate": 1.0,
            "response_completeness_mean": 5.0, "task_adherence_pass_rate": None, "content_safety_defects": None,
        }},
    )


def test_quick_policy_accepts_only_explicit_curated_non_release_results():
    assert gate.profile_error(quick_results(), "quick") is None


@pytest.mark.parametrize("mutation", [
    lambda value: value.pop("evaluation_profile"),
    lambda value: value.update(evaluation_profile="full"),
    lambda value: value.update(release_eligible=True),
    lambda value: value["rows"].pop(),
    lambda value: value["rows"].append(value["rows"][0]),
    lambda value: value["rows"].__setitem__(-1, {**value["rows"][-1], "id": "x07"}),
    lambda value: value["rows"][0].update(dataset="safety"),
])
def test_quick_policy_rejects_missing_or_forged_contract(mutation):
    value = quick_results()
    mutation(value)
    assert gate.profile_error(value, "quick")


def test_full_policy_rejects_quick_even_if_all_release_scores_are_forged():
    assert gate.profile_error(quick_results(), "full")


def test_quick_report_warns_and_does_not_claim_omitted_checks_passed():
    value = quick_results()
    full_thresholds = yaml.safe_load((gate.ROOT / "evals" / "thresholds.yaml").read_text())
    ok, checks = gate.evaluate(value, profiles.thresholds_for(full_thresholds, "quick"))
    report = gate.render(value, ok, checks, profile="quick")
    assert ok
    assert "Quick demo evaluation" in report
    assert "Demo evaluation - not release approval." in report
    assert "Task adherence and content-safety judges: not evaluated" in report
    assert "content_safety_defects" not in report


@pytest.mark.parametrize("flag", ["--allow-partial", "--no-cloud", "--baseline"])
def test_quick_cli_rejects_coverage_bypass_flags(tmp_path, flag):
    result_path = tmp_path / "quick.json"
    result_path.write_text(json.dumps(quick_results()), encoding="utf-8")
    args = [str(result_path), "--profile", "quick", flag]
    if flag == "--baseline":
        args.append(str(result_path))
    assert gate.main(args) == 1


def test_quick_cli_rejects_custom_threshold_files(tmp_path):
    result_path = tmp_path / "quick.json"
    result_path.write_text(json.dumps(quick_results()), encoding="utf-8")
    custom = tmp_path / "thresholds.yaml"
    custom.write_text("{}", encoding="utf-8")
    assert gate.main([str(result_path), "--profile", "quick", "--thresholds", str(custom)]) == 1


def test_quick_cli_returns_success_for_a_valid_demo_result(tmp_path):
    result_path = tmp_path / "quick.json"
    result_path.write_text(json.dumps(quick_results()), encoding="utf-8")
    assert gate.main([str(result_path), "--profile", "quick"]) == 0


def test_quick_cli_fails_when_a_required_judge_metric_is_missing(tmp_path):
    value = quick_results()
    del value["cloud"]["metrics"]["groundedness_mean"]
    result_path = tmp_path / "quick.json"
    result_path.write_text(json.dumps(value), encoding="utf-8")
    assert gate.main([str(result_path), "--profile", "quick"]) == 1


def test_quick_cli_fails_cleanly_when_cloud_results_are_malformed(tmp_path):
    value = quick_results()
    value["cloud"] = []
    result_path = tmp_path / "quick.json"
    result_path.write_text(json.dumps(value), encoding="utf-8")
    assert gate.main([str(result_path), "--profile", "quick"]) == 1


def test_quick_thresholds_leave_all_deterministic_requirements_unchanged():
    full = yaml.safe_load((gate.ROOT / "evals" / "thresholds.yaml").read_text(encoding="utf-8"))
    quick = profiles.thresholds_for(full, "quick")
    assert quick["deterministic"] == full["deterministic"]
