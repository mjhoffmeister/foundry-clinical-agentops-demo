import json

import gate
import pytest

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
