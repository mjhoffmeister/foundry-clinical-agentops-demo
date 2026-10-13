"""Prepare the official Foundry Action and collect its results for the release gate."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
import os
import sys
import time
import uuid
from pathlib import Path
from statistics import mean

from azure.ai.projects import AIProjectClient
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError, ServiceRequestError, ServiceResponseError
from openai import APIError, OpenAI

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "scripts"))

import foundry_agents as fa  # noqa: E402
import graders  # noqa: E402
import profiles  # noqa: E402
from run_eval import summary_markdown  # noqa: E402
from validator import strip_trailer  # noqa: E402

QUALITY = ("groundedness", "response_completeness", "task_adherence")
STATE_FILE = "action-state.json"


class ActionContractError(ValueError):
    pass


def read_rows(limit: int = 0, profile: str = "full") -> list[dict]:
    profiles.validate_profile(profile)
    if limit < 0:
        raise ActionContractError("Dataset limit must not be negative")
    if profile == "quick" and limit:
        raise ActionContractError("--limit cannot be combined with --profile quick")
    rows = []
    for dataset in ("golden", "safety"):
        path = ROOT / "evals" / "datasets" / f"{dataset}.jsonl"
        items = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        rows.extend({**item, "_dataset": dataset} for item in (items[:limit] if limit else items))
    ids = [row.get("id") for row in rows]
    if not rows or any(not isinstance(value, str) or not value for value in ids) or len(set(ids)) != len(ids):
        raise ActionContractError("Evaluation datasets must contain nonempty, unique row IDs")
    if any(not isinstance(row.get("query"), str) or not row["query"] for row in rows):
        raise ActionContractError("Every evaluation row must contain a nonempty query")
    try:
        return profiles.select_rows(rows, profile)
    except profiles.ProfileError as exc:
        raise ActionContractError(str(exc)) from exc


def validate_state(state: dict) -> str:
    if not isinstance(state, dict):
        raise ActionContractError("Action state must be a JSON object")
    schema = state.get("schema")
    if schema == 1:
        profile = "full"
    elif schema == 2:
        try:
            profile = profiles.validate_profile(state.get("evaluation_profile"))
        except profiles.ProfileError as exc:
            raise ActionContractError(str(exc)) from exc
        if state.get("builtin_evaluators") != list(profiles.BUILTIN_EVALUATORS[profile]):
            raise ActionContractError("Action state built-in evaluators do not match its evaluation profile")
        limit = state.get("limit", 0)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ActionContractError("Action state has an invalid dataset limit")
        if state.get("release_eligible") is not profiles.release_eligible(profile, limit=limit):
            raise ActionContractError("Action state release eligibility does not match its evaluation profile")
        evaluator_names = state.get("evaluators")
        if not isinstance(evaluator_names, dict) or evaluator_names.keys() != set(graders.CODE_METRICS):
            raise ActionContractError("Action state is missing clinical code evaluators")
        expected_names = {
            metric: f"clinical_{metric}_{hashlib.sha256(evaluator_code(metric).encode()).hexdigest()[:16]}"
            for metric in graders.CODE_METRICS
        }
        if evaluator_names != expected_names:
            raise ActionContractError("Action state clinical evaluators do not match the checked-out grading contract")
        try:
            selected = read_rows(limit=limit, profile=profile)
        except (ActionContractError, OSError, ValueError) as exc:
            raise ActionContractError(f"Cannot validate Action state rows: {exc}") from exc
        if state.get("rows") != selected:
            raise ActionContractError("Action state rows do not match the checked-in profile dataset")
    else:
        raise ActionContractError(f"Unsupported Action state schema {schema!r}")
    if not isinstance(state.get("rows"), list) or not state["rows"]:
        raise ActionContractError("Action state must contain evaluation rows")
    return profile


def evaluator_code(metric: str) -> str:
    if metric not in graders.CODE_METRICS:
        raise ActionContractError(f"Unknown clinical metric {metric!r}")
    validator = ast.parse((ROOT / "agent" / "validator.py").read_text(encoding="utf-8"))
    nodes = []
    for node in validator.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in ("TRAILER_PATTERN", "ABSTAIN_MARKER") for target in node.targets
        ):
            nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in ("strip_trailer", "is_abstention"):
            nodes.append(node)
    clinical = ast.parse(Path(graders.__file__).read_text(encoding="utf-8"))
    for node in clinical.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in ("DOSE_PATTERN", "_DECLINE_PATTERN", "BLOCK_HINTS")
            for target in node.targets
        ):
            nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in (
            "grade_golden", "grade_safety", "_without_declines", "applicable", "code_score",
        ):
            nodes.append(node)
    source = "from __future__ import annotations\nimport json\nimport re\n" + ast.unparse(
        ast.Module(body=nodes, type_ignores=[])
    )
    wrapper = f"""

def grade(sample, item) -> float:
    generated = item.get("sample")
    if not isinstance(generated, dict):
        raise ValueError("Missing native agent sample")
    raw = generated.get("output_text")
    if not isinstance(raw, str):
        raise ValueError("Missing native agent response text")
    body, meta = strip_trailer(raw)
    blocked = any(hint in json.dumps(generated.get("error") or {{}}).lower() for hint in BLOCK_HINTS)
    if not isinstance(meta, dict) and not blocked:
        raise ValueError("Missing or invalid clinical metadata trailer")
    result = {{"response": body, "meta": meta or {{}}, "blocked": blocked}}
    if not applicable({metric!r}, item, result):
        return 1.0
    values = grade_safety(item, result, item["_fingerprints"]) if item["_dataset"] == "safety" else grade_golden(item, result)
    return code_score({metric!r}, values)
"""
    code = source + wrapper
    compile(code, f"<clinical {metric}>", "exec")
    if len(code.encode("utf-8")) >= 256 * 1024:
        raise ActionContractError("Clinical evaluator exceeds the Foundry code-size limit")
    return code


def register_evaluator(project: AIProjectClient, metric: str, code: str) -> str:
    name = f"clinical_{metric}_{hashlib.sha256(code.encode()).hexdigest()[:16]}"
    try:
        existing = project.beta.evaluators.get_version(name=name, version="latest")
    except ResourceNotFoundError:
        definition = {
            "name": name,
            "categories": ["quality"],
            "display_name": metric.replace("_", " ").capitalize(),
            "description": "Clinical release check; the release gate aggregates only applicable dataset rows.",
            "definition": {
                "type": "code",
                "code_text": code,
                "init_parameters": {
                    "type": "object",
                    "required": ["deployment_name", "pass_threshold"],
                    "properties": {"deployment_name": {"type": "string"}, "pass_threshold": {"type": "number"}},
                },
                "metrics": {
                    "result": {"type": "continuous", "desirable_direction": "increase", "min_value": 0.0, "max_value": 1.0}
                },
                "data_schema": {
                    "type": "object",
                    "required": ["query", "response"],
                    "properties": {"query": {"type": "string"}, "response": {"type": "string"}},
                },
            },
        }
        try:
            project.beta.evaluators.create_version(name=name, evaluator_version=definition)
        except HttpResponseError as exc:
            if exc.status_code != 409:
                raise
        deadline = time.monotonic() + 180
        while True:
            try:
                existing = project.beta.evaluators.get_version(name=name, version="latest")
                break
            except ResourceNotFoundError:
                if time.monotonic() >= deadline:
                    raise ActionContractError(f"Registered evaluator {name} did not become readable") from None
                time.sleep(5)
    if existing.definition.as_dict().get("code_text") != code:
        raise ActionContractError(f"Evaluator {name} does not match the checked-out grading contract")
    return name


def prepare(
    project: AIProjectClient,
    client: OpenAI,
    version: str,
    tag: str,
    out: Path,
    limit: int = 0,
    profile: str = "full",
) -> dict:
    profiles.validate_profile(profile)
    if limit < 0:
        raise ActionContractError("Dataset limit must not be negative")
    if profile == "quick" and limit:
        raise ActionContractError("--limit cannot be combined with --profile quick")
    agent = os.environ.get("AGENT_NAME", "clinical-agent").strip()
    judge = (os.environ.get("AZURE_AI_JUDGE_DEPLOYMENT_NAME") or os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME") or "").strip()
    if not agent or not version or not judge:
        raise ActionContractError("Agent name, explicit version, and judge deployment are required")
    rows = read_rows(limit, profile)
    prompt_fingerprints = graders.fingerprints((ROOT / "agent" / "prompts" / "system.md").read_text(encoding="utf-8"))
    names = {metric: register_evaluator(project, metric, evaluator_code(metric)) for metric in graders.CODE_METRICS}
    previous = client.evals.list(order="desc", limit=1).data
    marker = f"clinical-eval-{uuid.uuid4().hex}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").unlink(missing_ok=True)
    data = [
        {
            **row,
            "expected_source_prefixes": row.get("expected_source_prefixes") or [],
            "required_facts": row.get("required_facts") or [],
            "expect": row.get("expect") or [],
            "_fingerprints": prompt_fingerprints,
            "ci_run_marker": marker,
            "ground_truth": "A complete answer covers these key facts: "
            + "; ".join(facts[0] for facts in row.get("required_facts") or []),
        }
        for row in rows
    ]
    builtin_evaluators = profiles.BUILTIN_EVALUATORS[profile]
    config = {
        "name": tag,
        "evaluators": [*(f"builtin.{metric}" for metric in builtin_evaluators), *names.values()],
        "evaluator_parameters": {
            name: {"deployment_name": judge, "pass_threshold": 1.0} for name in names.values()
        },
        "data": data,
    }
    data_path = out / f"{marker}.json"
    data_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    state = {
        "schema": 2,
        "evaluation_profile": profile,
        "release_eligible": profiles.release_eligible(profile, limit=limit),
        "builtin_evaluators": list(builtin_evaluators),
        "limit": limit,
        "agent": agent,
        "version": version,
        "tag": tag,
        "git_sha": os.environ.get("GITHUB_SHA", "local"),
        "marker": marker,
        "dataset_name": data_path.stem,
        "earliest_eval_created_at": previous[0].created_at if previous else 0,
        "rows": rows,
        "fingerprints": prompt_fingerprints,
        "evaluators": names,
    }
    (out / STATE_FILE).write_text(json.dumps(state, indent=2), encoding="utf-8")
    return {"data_path": str(data_path.resolve()), "agent_id": f"{agent}:{version}", "deployment_name": judge}


def as_dict(value) -> dict:
    return value.model_dump(mode="json", warnings=False) if hasattr(value, "model_dump") else value


def find_run(project: AIProjectClient, client: OpenAI, state: dict):
    datasets = list(project.datasets.list_versions(name=state["dataset_name"]))
    if len(datasets) != 1:
        raise ActionContractError(f"Expected one dataset version for this workflow, found {len(datasets)}")
    matches = []
    for evaluation in client.evals.list(order="desc", limit=100):
        if evaluation.created_at < state["earliest_eval_created_at"]:
            break
        for run in client.evals.runs.list(eval_id=evaluation.id, limit=100):
            source = as_dict(run.data_source)
            if (source.get("source") or {}).get("id") != datasets[0].id:
                continue
            target = source.get("target") or {}
            if source.get("type") != "azure_ai_target_completions" or (
                target.get("name"), str(target.get("version"))
            ) != (state["agent"], state["version"]):
                raise ActionContractError("The dataset's evaluation targets an unexpected agent/version")
            matches.append(run)
    if len(matches) != 1:
        raise ActionContractError(f"Expected one exact dataset-bound Action run, found {len(matches)}")
    run = client.evals.runs.retrieve(eval_id=matches[0].eval_id, run_id=matches[0].id)
    if run.status != "completed":
        raise ActionContractError(f"Action evaluation {run.id} is {run.status}: {run.error}")
    return run


def result_error(result: dict) -> bool:
    return result.get("status") in ("failed", "error", "canceled") or bool((result.get("sample") or {}).get("error"))


def passed(result: dict) -> bool:
    if result.get("passed") is not None:
        return bool(result["passed"])
    return str(result.get("label", "")).lower() == "pass"


def retry_code_errors(client: OpenAI, state: dict, outputs: list[dict]) -> dict | None:
    expected = {row["id"]: row for row in state["rows"]}
    affected, names = {}, set()
    for output in outputs:
        item = output.get("datasource_item") or {}
        row = expected.get(item.get("id"))
        if row is None or item.get("ci_run_marker") != state["marker"]:
            raise ActionContractError("Cannot retry grading for an unrelated dataset row")
        raw = item.get("sample.output_text")
        if not isinstance(raw, str):
            continue
        body, meta = strip_trailer(raw)
        if not isinstance(meta, dict) or (output.get("sample") or {}).get("error"):
            continue
        res = {"response": body, "meta": meta, "blocked": False}
        for metric, name in state["evaluators"].items():
            result = next((result for result in output.get("results") or [] if result.get("name") == name), None)
            if graders.applicable(metric, row, res) and result is not None and result_error(result):
                names.add(name)
                affected[row["id"]] = output
    if not affected:
        return None
    judge = os.environ.get("AZURE_AI_JUDGE_DEPLOYMENT_NAME") or os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME")
    if not judge:
        raise ActionContractError("A judge deployment is required for the code-only retry")
    print(f"Retrying errored clinical grading for {sorted(affected)} using captured responses; no agent invocation")
    evaluation = client.evals.create(
        name=f"Clinical code retry {state['marker']}",
        data_source_config={
            "type": "custom",
            "item_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}, "response": {"type": "string"}, "sample": {"type": "object"}},
                "required": ["query", "response", "sample"],
            },
        },
        testing_criteria=[
            {
                "type": "azure_ai_evaluator", "name": name, "evaluator_name": name,
                "initialization_parameters": {"deployment_name": judge, "pass_threshold": 1.0},
                "data_mapping": {"query": "{{item.query}}", "response": "{{item.response}}"},
            }
            for name in sorted(names)
        ],
    )
    run = client.evals.runs.create(
        eval_id=evaluation.id,
        name="Retry captured clinical outputs only",
        data_source={
            "type": "jsonl",
            "source": {
                "type": "file_content",
                "content": [
                    {"item": {
                        **output["datasource_item"],
                        "response": output["datasource_item"]["sample.output_text"],
                        "sample": {"output_text": output["datasource_item"]["sample.output_text"]},
                    }}
                    for output in affected.values()
                ],
            },
        },
    )
    deadline = time.monotonic() + 600
    while run.status not in ("completed", "failed", "canceled"):
        if time.monotonic() >= deadline:
            client.evals.runs.cancel(eval_id=evaluation.id, run_id=run.id)
            raise ActionContractError("Code-only grading retry timed out and was canceled")
        time.sleep(5)
        run = client.evals.runs.retrieve(eval_id=evaluation.id, run_id=run.id)
    if run.status != "completed":
        raise ActionContractError(f"Code-only grading retry {run.id} is {run.status}: {run.error}")
    seen = set()
    for listed in client.evals.runs.output_items.list(eval_id=evaluation.id, run_id=run.id, limit=100):
        detail = as_dict(client.evals.runs.output_items.retrieve(as_dict(listed)["id"], eval_id=evaluation.id, run_id=run.id))
        original = (detail.get("datasource_item") or {}).get("item") or {}
        row_id = original.get("id")
        if row_id not in affected or row_id in seen or original.get("ci_run_marker") != state["marker"]:
            raise ActionContractError("Code-only grading retry returned an unrelated or duplicate row")
        captured = affected[row_id]["datasource_item"]
        if (
            (detail.get("eval_id"), detail.get("run_id")) != (evaluation.id, run.id)
            or (original.get("query"), original.get("agent_name"), str(original.get("agent_version")))
            != (captured["query"], state["agent"], state["version"])
            or original.get("response") != captured["sample.output_text"]
        ):
            raise ActionContractError("Code-only grading retry does not match the captured agent response")
        seen.add(row_id)
        results = {result["name"]: result for result in detail.get("results") or []}
        for index, previous in enumerate(affected[row_id]["results"]):
            name = previous.get("name")
            if name in names and result_error(previous):
                retry = results.get(name)
                if retry is None or result_error(retry):
                    raise ActionContractError(f"Code-only retry did not recover {name} for {row_id}")
                affected[row_id]["results"][index] = retry
    if seen != set(affected):
        raise ActionContractError("Code-only grading retry is missing output rows")
    return {"eval_id": evaluation.id, "run_id": run.id, "rows": sorted(affected), "report_url": run.report_url}


def normalize(state: dict, outputs: list[dict], run: dict) -> dict:
    profile = validate_state(state)
    expected = {row["id"]: row for row in state["rows"]}
    seen = set()
    rows, cloud_scores = [], {metric: [] for metric in (*QUALITY, *graders.SAFETY_EVALUATORS)}
    for output in outputs:
        item, sample = output.get("datasource_item") or {}, output.get("sample") or {}
        row_id = item.get("id")
        if row_id not in expected or row_id in seen:
            raise ActionContractError(f"Unexpected or duplicate evaluation row {row_id!r}")
        seen.add(row_id)
        row = expected[row_id]
        if item.get("ci_run_marker") != state["marker"] or item.get("query") != row["query"]:
            raise ActionContractError(f"Row {row_id} is not from this workflow's dataset")
        if (item.get("agent_name"), str(item.get("agent_version"))) != (state["agent"], state["version"]):
            raise ActionContractError(f"Row {row_id} was served by an unexpected agent/version")
        error = sample.get("error") or output.get("error")
        error_text = json.dumps(error) if error else ""
        blocked = any(hint in error_text.lower() for hint in graders.BLOCK_HINTS)
        raw = item.get("sample.output_text")
        if not isinstance(raw, str):
            if not error:
                raise ActionContractError(f"Row {row_id} is missing its native response text")
            raw = ""
        body, meta = strip_trailer(raw)
        invocation_error = None if blocked else error_text or (None if isinstance(meta, dict) else "missing meta trailer")
        res = {
            "id": row_id,
            "dataset": row["_dataset"],
            "category": row["category"],
            "query": row["query"],
            "required_facts": row.get("required_facts"),
            "response": body,
            "meta": meta if isinstance(meta, dict) else {},
            "served_version": str(item["agent_version"]),
            "response_id": item.get("response_id"),
            "blocked": blocked,
            "error": invocation_error,
            "latency_s": round(sample["latency_ms"] / 1000, 2) if sample.get("latency_ms") is not None else None,
        }
        results = {}
        for result in output.get("results") or []:
            name = result.get("name")
            if name in results:
                raise ActionContractError(f"Row {row_id} has duplicate results for {name}")
            results[name] = result
        if not invocation_error:
            res["grades"] = (
                graders.grade_safety(row, res, state["fingerprints"])
                if row["_dataset"] == "safety" else graders.grade_golden(row, res)
            )
            for metric in graders.CODE_METRICS:
                if not graders.applicable(metric, row, res):
                    continue
                scored = results.get(state["evaluators"][metric])
                if blocked and scored is None:
                    res["grading_source"] = "explicit guardrail block"
                    continue
                if scored is None or result_error(scored):
                    raise ActionContractError(f"Row {row_id} is missing a successful clinical {metric} evaluation")
                value = scored.get("score")
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
                    raise ActionContractError(f"Row {row_id} has an invalid {metric} code-evaluator score")
                if not math.isclose(value, graders.code_score(metric, res["grades"]), abs_tol=1e-8):
                    raise ActionContractError(f"Row {row_id} cloud/local {metric} grading does not agree")
        if body:
            applicable_cloud = (
                profiles.BUILTIN_EVALUATORS[profile]
                if row["_dataset"] == "golden" and row["category"] == "answer"
                else graders.SAFETY_EVALUATORS if profile == "full" else ()
            )
            for metric in applicable_cloud:
                if metric not in results:
                    raise ActionContractError(f"Row {row_id} is missing the required {metric} result")
                cloud_scores[metric].append(results[metric])
        rows.append(res)
    if seen != set(expected):
        raise ActionContractError(f"Missing evaluation rows: {sorted(set(expected) - seen)}")

    def score_mean(metric: str):
        values = [float(result["score"]) for result in cloud_scores[metric] if result.get("score") is not None]
        if any(not math.isfinite(value) for value in values):
            raise ActionContractError(f"Non-finite {metric} evaluation score")
        return round(mean(values), 3) if values else None

    def pass_rate(metric: str):
        values = [result for result in cloud_scores[metric] if not result_error(result)]
        return round(sum(passed(result) for result in values) / len(values), 4) if values else None

    safety = [result for metric in graders.SAFETY_EVALUATORS for result in cloud_scores[metric]]
    judged = [result for metric in profiles.BUILTIN_EVALUATORS[profile] if metric in QUALITY
              for result in cloud_scores[metric]]
    all_judges = judged + safety
    per_model = {
        usage["model_name"]: usage["total_tokens"] for usage in run.get("per_model_usage") or []
        if usage["model_name"] != "azure_ai_evaluation"
    }
    latencies = [row["latency_s"] for row in rows if row["latency_s"] is not None]
    return {
        "evaluation_profile": profile,
        "release_eligible": state.get("release_eligible", profile == "full"),
        "agent": state["agent"],
        "version": state["version"],
        "git_sha": state["git_sha"],
        "tag": state["tag"],
        "counts": {
            "total": len(rows),
            "errors": sum(bool(row["error"]) for row in rows),
            "error_rate": round(sum(bool(row["error"]) for row in rows) / len(rows), 4),
            "mean_latency_s": round(mean(latencies), 2) if latencies else None,
        },
        "deterministic": graders.deterministic_metrics(rows),
        "rows": rows,
        "cloud": {
            "report_urls": {"action": run.get("report_url")},
            "metrics": {
                "judge_error_rate": round(sum(result_error(result) for result in all_judges) / len(all_judges), 4)
                if all_judges else None,
                "groundedness_mean": score_mean("groundedness"),
                "groundedness_pass_rate": pass_rate("groundedness"),
                "response_completeness_mean": score_mean("response_completeness"),
                "task_adherence_pass_rate": pass_rate("task_adherence"),
                "content_safety_defects": sum(not passed(result) for result in safety if not result_error(result))
                if safety else None,
            },
            "usage": {"total_tokens": sum(per_model.values()), "by_model": per_model},
            "per_item": cloud_scores,
            "eval_id": run["eval_id"],
            "run_id": run["id"],
            "ci_run_marker": state["marker"],
        },
    }


def collect(project: AIProjectClient, client: OpenAI, out: Path) -> dict:
    state = json.loads((out / STATE_FILE).read_text(encoding="utf-8"))
    validate_state(state)
    run = find_run(project, client, state)
    outputs = []
    for item in client.evals.runs.output_items.list(eval_id=run.eval_id, run_id=run.id, limit=100):
        preview = as_dict(item)
        item_id = preview.get("id")
        if not isinstance(item_id, str) or not item_id:
            raise ActionContractError("An evaluation output item has no stable identifier")
        # List responses are previews and truncate strings, including metadata trailers.
        detail = as_dict(client.evals.runs.output_items.retrieve(item_id, eval_id=run.eval_id, run_id=run.id))
        if (
            (detail.get("eval_id"), detail.get("run_id")) != (run.eval_id, run.id)
            or detail.get("datasource_item_id") != preview.get("datasource_item_id")
            or (detail.get("datasource_item") or {}).get("id") != (preview.get("datasource_item") or {}).get("id")
        ):
            raise ActionContractError("Evaluation detail does not match the listed output item")
        outputs.append(detail)
    retry = retry_code_errors(client, state, outputs)
    result = normalize(state, outputs, as_dict(run))
    if retry:
        result["cloud"]["code_retry"] = retry
        result["cloud"]["report_urls"]["clinical code-only retry"] = retry["report_url"]
    (out / "results.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    (out / "summary.md").write_text(summary_markdown(result), encoding="utf-8")
    print(f"Collected {len(outputs)} rows from {run.id} for {state['agent']}:{state['version']}")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare")
    prep.add_argument("--version", required=True)
    prep.add_argument("--tag", required=True)
    prep.add_argument("--limit", type=int, default=0, help="limit each dataset for compatibility probes only")
    prep.add_argument("--profile", choices=profiles.PROFILES, default="full")
    prep.add_argument("--github-output", action="store_true")
    collector = commands.add_parser("collect")
    collector.add_argument("--action-status", choices=("success", "failure", "cancelled", "skipped"), default="success")
    for command in (prep, collector):
        command.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "collect" and args.action_status != "success":
            raise ActionContractError(f"Official Foundry Action was {args.action_status}; release is blocked")
        if args.command == "prepare":
            profiles.validate_profile(args.profile)
            args.out.mkdir(parents=True, exist_ok=True)
            (args.out / "results.json").unlink(missing_ok=True)
            if args.limit < 0:
                raise ActionContractError("Dataset limit must not be negative")
            if args.profile == "quick" and args.limit:
                raise ActionContractError("--limit cannot be combined with --profile quick")
        with (
            fa.make_credential() as credential,
            AIProjectClient(endpoint=fa.project_endpoint(), credential=credential) as project,
            project.get_openai_client(max_retries=3, timeout=90) as client,
        ):
            if args.command == "prepare":
                fa.emit(prepare(project, client, args.version, args.tag, args.out, args.limit, args.profile), args.github_output)
            else:
                collect(project, client, args.out)
    except (
        ActionContractError, HttpResponseError, ServiceRequestError, ServiceResponseError,
        APIError, fa.FoundryError, OSError, ValueError,
    ) as exc:
        print(f"Foundry Action {args.command} FAILED: {exc}", file=sys.stderr)
        if args.command == "collect":
            args.out.mkdir(parents=True, exist_ok=True)
            failed = {
                "counts": {"total": 0, "error_rate": 1.0},
                "release_eligible": False,
                "cloud": {"errors": [str(exc)]},
            }
            try:
                state = json.loads((args.out / STATE_FILE).read_text(encoding="utf-8"))
                failed["evaluation_profile"] = validate_state(state)
            except (ActionContractError, OSError, ValueError):
                pass
            (args.out / "results.json").write_text(
                json.dumps(failed),
                encoding="utf-8",
            )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
