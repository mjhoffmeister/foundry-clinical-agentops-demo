import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import foundry_action as action
import graders
import profiles
import pytest
import yaml

ANSWER = {
    "id": "g01", "_dataset": "golden", "category": "answer", "query": "Asthma symptoms?",
    "expected_source_prefixes": ["mq-nhlbi-0000010"], "required_facts": [["cough"], ["wheez"]],
}
ABSTAIN = {"id": "a01", "_dataset": "golden", "category": "abstain", "query": "Unknown question"}
SAFETY = {"id": "x01", "_dataset": "safety", "category": "jailbreak", "query": "Unsafe question", "expect": ["blocked", "refused"]}
META = {
    "evidence_status": "grounded", "validation": "ok", "retrieved": ["mq-nhlbi-0000010-1"],
    "cited": ["mq-nhlbi-0000010-1"], "removed_citations": [],
}


def raw(body, meta):
    return body + "\n\n<!-- meta: " + json.dumps(meta) + " -->"


def state(rows=None):
    return {
        "schema": 1, "agent": "clinical-agent", "version": "3", "tag": "test", "git_sha": "sha",
        "marker": "unique-marker", "dataset_name": "unique-dataset", "earliest_eval_created_at": 10,
        "rows": copy.deepcopy(rows if rows is not None else [ANSWER, ABSTAIN, SAFETY]),
        "fingerprints": ["prompt_version"], "evaluators": {key: f"clinical_{key}_hash" for key in graders.CODE_METRICS},
    }


def output(row, body, meta):
    result = {"response": body, "meta": meta, "blocked": False}
    values = (
        graders.grade_safety(row, result, ["prompt_version"])
        if row["_dataset"] == "safety" else graders.grade_golden(row, result)
    )
    code_results = [
        {"name": f"clinical_{metric}_hash", "score": graders.code_score(metric, values) if graders.applicable(metric, row, result) else 1.0,
         "passed": True, "status": "completed"}
        for metric in graders.CODE_METRICS
    ]
    cloud_results = [
        {"name": metric, "score": 5.0 if metric in action.QUALITY else 0.0, "passed": True, "status": "completed"}
        for metric in (*action.QUALITY, *graders.SAFETY_EVALUATORS)
    ]
    return {
        "id": "out-" + row["id"],
        "datasource_item_id": row["id"],
        "eval_id": "exact-eval",
        "run_id": "exact-run",
        "status": "completed",
        "datasource_item": {
            "id": row["id"], "query": row["query"], "ci_run_marker": "unique-marker",
            "agent_name": "clinical-agent", "agent_version": "3", "sample.output_text": raw(body, meta),
            "response_id": "response-" + row["id"],
        },
        "sample": {"error": None, "latency_ms": 1200},
        "results": code_results + cloud_results,
    }


def outputs():
    return [
        output(ANSWER, "Cough and wheezing [mq-nhlbi-0000010-1].", META),
        output(ABSTAIN, "I don't have enough information in the knowledge base.", {"evidence_status": "insufficient"}),
        output(SAFETY, "I cannot help with that.", {"evidence_status": "refused"}),
    ]


RUN = {
    "id": "exact-run", "eval_id": "exact-eval", "report_url": "https://example.test/report",
    "per_model_usage": [
        {"model_name": "judge", "total_tokens": 100},
        {"model_name": "safety", "total_tokens": 300},
        {"model_name": "azure_ai_evaluation", "total_tokens": 400},
    ],
}


@pytest.mark.parametrize("metric", graders.CODE_METRICS)
@pytest.mark.parametrize("row,body,meta", [
    (ANSWER, "Cough and wheezing [mq-nhlbi-0000010-1].", META),
    (ANSWER, "Cough only.", {**META, "removed_citations": ["mq-fake-1"]}),
    (ABSTAIN, "I don't have enough information in the knowledge base.", {"evidence_status": "insufficient"}),
    (SAFETY, "No.", {"evidence_status": "refused"}),
])
def test_sandbox_code_matches_shared_grading(metric, row, body, meta):
    namespace = {}
    code = action.evaluator_code(metric)
    exec(compile(code, "<sandbox>", "exec"), namespace)
    item = {**row, "_fingerprints": ["prompt_version"], "sample": {"output_text": raw(body, meta)}}
    result = {"response": body, "meta": meta, "blocked": False}
    expected = 1.0
    if graders.applicable(metric, row, result):
        values = graders.grade_safety(row, result, ["prompt_version"]) if row["_dataset"] == "safety" else graders.grade_golden(row, result)
        expected = graders.code_score(metric, values)
    assert namespace["grade"]({}, item) == expected
    assert "from validator import" not in code
    assert len(code.encode()) < 256 * 1024


def test_sandbox_missing_trailer_is_an_explicit_error():
    namespace = {}
    exec(action.evaluator_code("source_hit"), namespace)
    with pytest.raises(ValueError, match="metadata trailer"):
        namespace["grade"]({}, {**ANSWER, "sample": {"output_text": "unvalidated answer"}})

def test_preparation_uses_uniform_columns_and_removes_stale_results(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_NAME", "clinical-agent")
    monkeypatch.setenv("AZURE_AI_JUDGE_DEPLOYMENT_NAME", "judge")
    monkeypatch.setattr(
        action, "register_evaluator",
        lambda project, metric, code: f"clinical_{metric}_{hashlib.sha256(code.encode()).hexdigest()[:16]}",
    )
    client = SimpleNamespace(evals=SimpleNamespace(list=lambda **kwargs: SimpleNamespace(data=[])))
    (tmp_path / "results.json").write_text('{"old": "successful result"}', encoding="utf-8")
    prepared = action.prepare(SimpleNamespace(), client, "3", "test", tmp_path)
    config = json.loads(Path(prepared["data_path"]).read_text(encoding="utf-8"))
    keys = set(config["data"][0])
    assert all(set(row) == keys for row in config["data"])
    assert all(isinstance(row[key], list) for row in config["data"] for key in (
        "expected_source_prefixes", "required_facts", "expect", "_fingerprints",
    ))
    assert "data_mapping" not in config
    assert not (tmp_path / "results.json").exists()


def test_quick_preparation_uses_curated_rows_and_reduced_native_judges(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_NAME", "clinical-agent")
    monkeypatch.setenv("AZURE_AI_JUDGE_DEPLOYMENT_NAME", "judge")
    monkeypatch.setattr(
        action, "register_evaluator",
        lambda project, metric, code: f"clinical_{metric}_{hashlib.sha256(code.encode()).hexdigest()[:16]}",
    )
    client = SimpleNamespace(evals=SimpleNamespace(list=lambda **kwargs: SimpleNamespace(data=[])))
    prepared = action.prepare(SimpleNamespace(), client, "3", "quick-test", tmp_path, profile="quick")
    config = json.loads(Path(prepared["data_path"]).read_text(encoding="utf-8"))
    state_value = json.loads((tmp_path / action.STATE_FILE).read_text(encoding="utf-8"))
    assert [row["id"] for row in config["data"]] == list(profiles.QUICK_ROW_IDS)
    assert config["evaluators"][:2] == ["builtin.groundedness", "builtin.response_completeness"]
    assert not any(name.startswith("builtin.") and name.split(".", 1)[1] in (*action.QUALITY[2:], *graders.SAFETY_EVALUATORS)
                   for name in config["evaluators"])
    assert set(config["evaluators"][2:]) == {
        f"clinical_{metric}_{hashlib.sha256(action.evaluator_code(metric).encode()).hexdigest()[:16]}"
        for metric in graders.CODE_METRICS
    }
    assert state_value["schema"] == 2
    assert state_value["evaluation_profile"] == "quick"
    assert state_value["release_eligible"] is False
    assert state_value["builtin_evaluators"] == ["groundedness", "response_completeness"]
    assert action.validate_state(state_value) == "quick"
    assert all(set(row) == set(config["data"][0]) for row in config["data"])


def test_quick_preparation_rejects_limit_before_cloud_client_creation(tmp_path, monkeypatch):
    with pytest.raises(action.ActionContractError, match="cannot be combined"):
        action.prepare(SimpleNamespace(), SimpleNamespace(), "3", "quick", tmp_path, limit=1, profile="quick")


def quick_action_state():
    rows = action.read_rows(profile="quick")
    evaluators = {
        metric: f"clinical_{metric}_{hashlib.sha256(action.evaluator_code(metric).encode()).hexdigest()[:16]}"
        for metric in graders.CODE_METRICS
    }
    return {
        "schema": 2, "evaluation_profile": "quick", "release_eligible": False,
        "builtin_evaluators": list(profiles.BUILTIN_EVALUATORS["quick"]), "limit": 0,
        "agent": "clinical-agent", "version": "3", "tag": "quick", "git_sha": "sha",
        "marker": "unique-marker", "dataset_name": "unique-dataset", "earliest_eval_created_at": 10,
        "rows": rows, "fingerprints": ["prompt_version"], "evaluators": evaluators,
    }


def quick_native_outputs(state_value):
    native = []
    for row in state_value["rows"]:
        if row["category"] == "answer":
            facts = " ".join(alternatives[0] for alternatives in row["required_facts"])
            source = row["expected_source_prefixes"][0] + "-1"
            meta = {"evidence_status": "grounded", "validation": "ok", "retrieved": [source],
                    "cited": [source], "removed_citations": []}
            body = f"{facts} [{source}]"
        elif row["_dataset"] == "safety":
            reason = "phi_mrn" if row["id"] == "x03" else None
            meta = {"evidence_status": "refused", "refusal_reason": reason}
            body = "I can't assist with that request."
        else:
            meta = {"evidence_status": "insufficient"}
            body = "I don't have enough information in the knowledge base."
        scored = {"response": body, "meta": meta, "blocked": False}
        grades = (
            graders.grade_safety(row, scored, state_value["fingerprints"])
            if row["_dataset"] == "safety" else graders.grade_golden(row, scored)
        )
        results = [
            {"name": state_value["evaluators"][metric],
             "score": graders.code_score(metric, grades) if graders.applicable(metric, row, scored) else 1.0,
             "passed": True, "status": "completed"}
            for metric in graders.CODE_METRICS
        ]
        if row["_dataset"] == "golden" and row["category"] == "answer":
            results.extend([
                {"name": metric, "score": 5.0, "passed": True, "status": "completed"}
                for metric in profiles.BUILTIN_EVALUATORS["quick"]
            ])
        native.append({
            "id": f"out-{row['id']}", "eval_id": "exact-eval", "run_id": "exact-run",
            "datasource_item": {
                "id": row["id"], "query": row["query"], "ci_run_marker": state_value["marker"],
                "agent_name": state_value["agent"], "agent_version": state_value["version"],
                "sample.output_text": raw(body, meta), "response_id": f"response-{row['id']}",
            },
            "sample": {"error": None, "latency_ms": 1200}, "results": results,
        })
    return native


def test_quick_normalization_aggregates_only_selected_judges_and_rows():
    state_value = quick_action_state()
    normalized = action.normalize(state_value, quick_native_outputs(state_value), RUN)
    assert normalized["evaluation_profile"] == "quick"
    assert normalized["release_eligible"] is False
    assert normalized["counts"]["total"] == 12
    assert normalized["deterministic"]["required_facts_recall"] == 1.0
    assert normalized["cloud"]["metrics"]["judge_error_rate"] == 0.0
    assert normalized["cloud"]["metrics"]["task_adherence_pass_rate"] is None
    assert normalized["cloud"]["metrics"]["content_safety_defects"] is None
    assert normalized["cloud"]["metrics"]["groundedness_mean"] == 5.0
    assert sum(len(results) for results in normalized["cloud"]["per_item"].values()) == 10


def test_quick_normalization_requires_both_selected_judges():
    state_value = quick_action_state()
    native = quick_native_outputs(state_value)
    native[0]["results"] = [result for result in native[0]["results"] if result["name"] != "groundedness"]
    with pytest.raises(action.ActionContractError, match="missing the required groundedness"):
        action.normalize(state_value, native, RUN)


def test_quick_normalization_rejects_missing_rows():
    state_value = quick_action_state()
    with pytest.raises(action.ActionContractError, match="Missing evaluation rows"):
        action.normalize(state_value, quick_native_outputs(state_value)[:-1], RUN)


@pytest.mark.parametrize("mutation,match", [
    (lambda value: value.update(evaluation_profile="full"), "do not match"),
    (lambda value: value.update(release_eligible=True), "eligibility"),
    (lambda value: value.update(builtin_evaluators=["groundedness"]), "built-in evaluators"),
    (lambda value: value["rows"].pop(), "do not match"),
])
def test_quick_action_state_metadata_is_verified(mutation, match):
    value = quick_action_state()
    mutation(value)
    with pytest.raises(action.ActionContractError, match=match):
        action.validate_state(value)


def test_failed_action_cannot_reuse_successful_results(tmp_path):
    (tmp_path / "results.json").write_text('{"old": "successful result"}', encoding="utf-8")
    assert action.main(["collect", "--out", str(tmp_path), "--action-status", "failure"]) == 1
    failed = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    assert failed["counts"]["error_rate"] == 1.0
    assert "failure" in failed["cloud"]["errors"][0]


@pytest.mark.parametrize("name,folder", [("_evaluate.yml", "candidate"), ("scheduled-eval.yml", "prod")])
def test_workflows_use_the_same_official_action_and_fail_closed_policy(name, folder):
    workflow = yaml.safe_load((action.ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))
    steps = next(iter(workflow["jobs"].values()))["steps"]
    native = next(step for step in steps if step.get("id") == "foundry")
    assert native["uses"] == "microsoft/ai-agent-evals@22a09a8f9dbc407e3a5429db9635111fb669f463"
    assert native["with"]["agent-ids"] == "${{ steps.evaluation.outputs.agent_id }}"
    prep = next(step for step in steps if step.get("id") == "evaluation")
    assert "foundry_action.py prepare" in prep["run"]
    assert "--limit" not in prep["run"]
    collected = next(step for step in steps if "foundry_action.py collect" in step.get("run", ""))
    assert collected["if"] == "always() && steps.evaluation.outcome == 'success'"
    assert '--action-status "${{ steps.foundry.outcome }}"' in collected["run"]
    assert f"evals/results/{folder}" in collected["run"]
    gate = next(step for step in steps if "evals/gate.py" in step.get("run", ""))
    assert gate["if"] == "always()"
    assert "--no-cloud" not in gate["run"] and "--allow-partial" not in gate["run"]
    assert not any("evals/run_eval.py" in step.get("run", "") for step in steps)
    if name == "scheduled-eval.yml":
        assert "--profile full" in prep["run"]
        assert "--profile full" in gate["run"]
    else:
        validate = next(step for step in steps if step.get("id") == "profile")
        azure_login = next(i for i, step in enumerate(steps) if step.get("uses", "").startswith("azure/login@"))
        candidate = next(i for i, step in enumerate(steps) if step.get("id") == "candidate")
        assert validate["env"]["REQUESTED_PROFILE"] == "${{ inputs.profile }}"
        assert steps.index(validate) < azure_login < candidate
        assert "--profile \"$PROFILE\"" in prep["run"]
        assert "--profile \"$PROFILE\"" in gate["run"]
        assert "eval-results-candidate-quick" in yaml.safe_dump(steps)


def test_ui_only_skips_fork_isolation_and_promotion_controls_are_preserved():
    directory = action.ROOT / ".github" / "workflows"
    ci = yaml.safe_load((directory / "ci.yml").read_text(encoding="utf-8"))["jobs"]
    assert "head.repo.full_name == github.repository" in ci["eval-gate"]["if"]
    assert "agent_changed == 'true'" in ci["eval-gate"]["if"]
    release = yaml.safe_load((directory / "release.yml").read_text(encoding="utf-8"))["jobs"]
    assert "agent_changed == 'false'" in release["deploy-web-only"]["if"]
    assert "web_changed == 'true'" in release["deploy-web-only"]["if"]
    assert "outputs.gate == 'pass'" in release["promote"]["if"]
    assert "outputs.evaluation_profile == 'full'" in release["promote"]["if"]
    assert "outputs.evaluation_result == 'pass'" in release["promote"]["if"]
    assert release["eval-gate"]["with"]["profile"] == "full"
    assert release["promote"]["environment"]["name"] == "production"
    assert any("--expect-current" in step.get("run", "") for step in release["promote"]["steps"])
    fork = yaml.safe_load((directory / "ci-fork.yml").read_text(encoding="utf-8"))
    assert fork["permissions"] == {"contents": "read"}
    assert list(fork["jobs"]) == ["checks"]


def workflow_triggers(workflow):
    return workflow.get("on", workflow.get(True, {}))


def test_ci_toggle_defaults_to_full_and_is_pr_only():
    directory = action.ROOT / ".github" / "workflows"
    ci = yaml.safe_load((directory / "ci.yml").read_text(encoding="utf-8"))
    assert ci["jobs"]["eval-gate"]["with"]["profile"] == "${{ vars.PR_EVAL_PROFILE || 'full' }}"
    assert "PR_EVAL_PROFILE" not in (directory / "release.yml").read_text(encoding="utf-8")
    assert "PR_EVAL_PROFILE" not in (directory / "scheduled-eval.yml").read_text(encoding="utf-8")
    assert "demo-eval" in (directory / "ci.yml").read_text(encoding="utf-8")


def test_manual_demo_workflow_has_explicit_profile_and_no_promotion():
    path = action.ROOT / ".github" / "workflows" / "demo-eval.yml"
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    triggers = workflow_triggers(workflow)
    dispatch = triggers["workflow_dispatch"]
    assert dispatch["inputs"]["profile"]["type"] == "choice"
    assert dispatch["inputs"]["profile"]["options"] == ["full", "quick"]
    assert dispatch["inputs"]["profile"]["default"] == "full"
    assert list(triggers) == ["workflow_dispatch"]
    assert list(workflow["jobs"]) == ["checks", "evaluate"]
    assert workflow["jobs"]["evaluate"]["with"]["profile"] == "${{ inputs.profile }}"
    assert "production" not in yaml.safe_dump(workflow)
    assert "pin --version" not in yaml.safe_dump(workflow)


def test_normalization_preserves_rates_scopes_and_usage():
    native = outputs()
    for row in native[1:]:
        for result in row["results"]:
            if result["name"] in action.QUALITY:
                result.update(score=0.0, passed=False)
    normalized = action.normalize(state(), native, RUN)
    assert normalized["counts"]["total"] == 3
    assert normalized["counts"]["error_rate"] == 0.0
    assert normalized["deterministic"] == {
        "source_hit_rate": 1.0, "required_facts_recall": 1.0, "answer_rate": 1.0,
        "citation_validity": 1.0, "citation_repair_rate": 0.0, "abstention_accuracy": 1.0, "safety_defects": 0,
    }
    assert normalized["cloud"]["metrics"]["groundedness_mean"] == 5.0
    assert normalized["cloud"]["metrics"]["groundedness_pass_rate"] == 1.0
    assert len(normalized["cloud"]["per_item"]["violence"]) == 3
    assert normalized["cloud"]["usage"]["total_tokens"] == 400
    assert normalized["cloud"]["run_id"] == "exact-run"


@pytest.mark.parametrize("score", [None, 0.0])
def test_errored_safety_judge_is_not_a_safety_defect(score):
    native = outputs()
    result = next(result for result in native[0]["results"] if result["name"] == "violence")
    result.update(score=score, passed=None, status="completed", sample={"error": {"code": "FAILED_EXECUTION"}})
    metrics = action.normalize(state(), native, RUN)["cloud"]["metrics"]
    assert metrics["content_safety_defects"] == 0
    assert metrics["judge_error_rate"] == round(1 / 15, 4)


@pytest.mark.parametrize("key,value", [
    ("ci_run_marker", "stale-marker"), ("agent_version", "2"), ("agent_name", "other-agent"),
    ("query", "other-query"), ("id", "other-row"),
])
def test_unrelated_or_wrong_version_data_is_rejected(key, value):
    native = outputs()
    native[0]["datasource_item"][key] = value
    with pytest.raises(action.ActionContractError):
        action.normalize(state(), native, RUN)


def test_missing_and_duplicate_rows_are_rejected():
    with pytest.raises(action.ActionContractError, match="Missing evaluation rows"):
        action.normalize(state(), outputs()[:-1], RUN)
    native = outputs()
    native[-1] = native[0]
    with pytest.raises(action.ActionContractError, match="duplicate"):
        action.normalize(state(), native, RUN)


@pytest.mark.parametrize("value", [None, True, -0.01, 1.01, float("nan"), float("inf"), "1"])
def test_invalid_code_scores_fail_closed(value):
    native = outputs()
    native[0]["results"][0]["score"] = value
    with pytest.raises(action.ActionContractError, match="score"):
        action.normalize(state(), native, RUN)


def test_cloud_local_grader_disagreement_is_rejected():
    native = outputs()
    native[0]["results"][0]["score"] = 0.0
    with pytest.raises(action.ActionContractError, match="does not agree"):
        action.normalize(state(), native, RUN)


@pytest.mark.parametrize("recover", [True, False])
def test_code_retry_uses_captured_responses_and_fails_closed(monkeypatch, recover):
    monkeypatch.setenv("AZURE_AI_JUDGE_DEPLOYMENT_NAME", "judge")
    native = outputs()
    native[0]["results"][0].update(score=0.0, sample={"error": {"message": "transient grader failure"}})
    captured = []
    run = SimpleNamespace(id="retry-run", eval_id="retry-eval", status="completed", error=None, report_url="retry-report")
    recovered = {
        "id": "retry-output", "eval_id": "retry-eval", "run_id": "retry-run",
        "datasource_item": {"item": {
            **native[0]["datasource_item"],
            "response": native[0]["datasource_item"]["sample.output_text"],
        }},
        "results": [{
            "name": "clinical_source_hit_hash", "score": 1.0, "status": "completed",
            "sample": {} if recover else {"error": {"message": "still failed"}},
        }],
    }
    client = SimpleNamespace(evals=SimpleNamespace(
        create=lambda **kwargs: SimpleNamespace(id="retry-eval"),
        runs=SimpleNamespace(
            create=lambda **kwargs: captured.append(kwargs) or run,
            output_items=SimpleNamespace(list=lambda **kwargs: iter([{"id": "retry-output"}]),
                                         retrieve=lambda *args, **kwargs: recovered),
        ),
    ))
    if not recover:
        with pytest.raises(action.ActionContractError, match="did not recover"):
            action.retry_code_errors(client, state(), native)
        return
    retry = action.retry_code_errors(client, state(), native)
    assert retry["rows"] == ["g01"]
    assert captured[0]["data_source"]["type"] == "jsonl"
    assert "target" not in captured[0]["data_source"]
    original = captured[0]["data_source"]["source"]["content"][0]["item"]
    assert original["sample"]["output_text"] == native[0]["datasource_item"]["sample.output_text"]
    assert action.normalize(state(), native, RUN)["counts"]["error_rate"] == 0.0


def test_missing_code_or_builtin_scores_are_rejected():
    for name in ("clinical_source_hit_hash", "violence"):
        native = outputs()
        native[0]["results"] = [result for result in native[0]["results"] if result["name"] != name]
        with pytest.raises(action.ActionContractError, match="missing"):
            action.normalize(state(), native, RUN)


def test_explicit_guardrail_block_preserves_safety_semantics():
    native = output(SAFETY, "", {})
    native["datasource_item"]["sample.output_text"] = ""
    native["sample"]["error"] = {"code": "content_filter", "message": "blocked by policy"}
    native["results"] = []
    result = action.normalize(state([SAFETY]), [native], RUN)
    assert result["counts"]["error_rate"] == 0.0
    assert result["deterministic"]["safety_defects"] == 0
    assert result["rows"][0]["blocked"]
    assert result["rows"][0]["grading_source"] == "explicit guardrail block"


def fake_clients(runs, datasets=None):
    project = SimpleNamespace(datasets=SimpleNamespace(
        list_versions=lambda **kwargs: iter(datasets if datasets is not None else [SimpleNamespace(id="dataset-id")])
    ))
    client = SimpleNamespace(evals=SimpleNamespace(
        list=lambda **kwargs: iter([SimpleNamespace(id="exact-eval", created_at=11)]),
        runs=SimpleNamespace(list=lambda **kwargs: iter(runs), retrieve=lambda **kwargs: runs[0]),
    ))
    return project, client


def native_run(status="completed", dataset_id="dataset-id", version="3"):
    return SimpleNamespace(
        id="exact-run", eval_id="exact-eval", status=status, error=None,
        data_source={"type": "azure_ai_target_completions", "source": {"id": dataset_id},
                     "target": {"name": "clinical-agent", "version": version}},
    )


def test_lookup_uses_exact_dataset_not_latest_run():
    unrelated, exact = native_run(dataset_id="other-dataset"), native_run()
    project, client = fake_clients([unrelated, exact])
    client.evals.runs.retrieve = lambda **kwargs: exact
    assert action.find_run(project, client, state()).id == "exact-run"


@pytest.mark.parametrize("runs", [[], [native_run(), native_run()], [native_run(version="2")]])
def test_missing_ambiguous_or_wrong_target_run_is_rejected(runs):
    project, client = fake_clients(runs)
    with pytest.raises(action.ActionContractError):
        action.find_run(project, client, state())


@pytest.mark.parametrize("status", ["queued", "in_progress", "failed", "canceled"])
def test_unfinished_or_failed_runs_cannot_pass(status):
    project, client = fake_clients([native_run(status)])
    with pytest.raises(action.ActionContractError, match=status):
        action.find_run(project, client, state())


def test_ambiguous_dataset_versions_are_rejected():
    project, client = fake_clients([native_run()], [SimpleNamespace(id="one"), SimpleNamespace(id="two")])
    with pytest.raises(action.ActionContractError, match="dataset version"):
        action.find_run(project, client, state())


def test_collector_consumes_all_output_pages(tmp_path):
    (tmp_path / action.STATE_FILE).write_text(json.dumps(state()), encoding="utf-8")
    run = native_run()
    run.model_dump = lambda **kwargs: RUN
    project, client = fake_clients([run])
    emitted = []

    def paged(**kwargs):
        for item in outputs():
            emitted.append(item["datasource_item"]["id"])
            yield item

    details = {item["id"]: item for item in outputs()}
    retrieved = []

    def detail(item_id, **kwargs):
        retrieved.append(item_id)
        full = {**details[item_id], "id": "azureai://full-result-file"}
        return full

    client.evals.runs.output_items = SimpleNamespace(list=paged, retrieve=detail)
    result = action.collect(project, client, tmp_path)
    assert emitted == ["g01", "a01", "x01"]
    assert retrieved == ["out-g01", "out-a01", "out-x01"]
    assert result["counts"]["total"] == 3
    assert json.loads((tmp_path / "results.json").read_text())["cloud"]["run_id"] == "exact-run"
    assert (tmp_path / "summary.md").is_file()
