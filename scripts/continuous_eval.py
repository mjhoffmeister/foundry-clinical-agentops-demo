"""Set up (idempotently) continuous evaluation of production traffic in Foundry.

Foundry evaluation *rules* (evaluate every RESPONSE_COMPLETED event) are not
supported for hosted agents, so this script creates a Foundry *scheduled trace
evaluation* instead: every N hours Foundry samples the agent's production traces
from Application Insights and scores them with quality + safety evaluators.
Results appear under the agent's Monitor / Evaluations tabs.

If a future service release enables rules for hosted agents, --mode rule (or
auto) will create the rule instead.

WARNING (Oct 2026): scheduled trace evaluations of *hosted* agents stayed
"in_progress" indefinitely in testing and blocked every other quality
evaluation in the project - including the CI eval gate. The schedule is
therefore NOT created by default; use --mode off to remove it, and rely on
.github/workflows/scheduled-eval.yml for continuous evaluation of production.

    uv run python scripts/continuous_eval.py [--mode auto|rule|schedule|off] [--every-hours 1]
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from azure.ai.projects import AIProjectClient
from azure.core.exceptions import HttpResponseError

sys.path.insert(0, str(Path(__file__).resolve().parent))
from foundry_agents import make_credential  # noqa: E402


def criteria(judge: str, traces: bool) -> list[dict]:
    init = {"model": judge}
    mapping = {"data_mapping": {"query": "{{item.query}}", "response": "{{item.response}}"}} if traces else {}
    quality = ("task_adherence", "intent_resolution", "coherence") + (("groundedness",) if traces else ())
    out = [{"type": "azure_ai_evaluator", "name": n, "evaluator_name": f"builtin.{n}",
            "initialization_parameters": init, **mapping} for n in quality]
    out += [{"type": "azure_ai_evaluator", "name": n, "evaluator_name": f"builtin.{n}",
             "initialization_parameters": init, **mapping} for n in ("violence", "self_harm", "hate_unfairness")]
    return out


def trace_run(agent: str, eval_id: str, hours: int, max_traces: int) -> dict:
    now = int(datetime.now(UTC).timestamp())
    return {
        "eval_id": eval_id,
        "name": f"{agent}-production-traces",
        "data_source": {
            "type": "azure_ai_trace_data_source",
            "trace_source": {
                "type": "agent_filter",
                "agent_name": agent,
                "start_time": now - hours * 3600,
                "end_time": now + 600,  # pad for App Insights ingestion delay
                "max_traces": max_traces,
                "filter_strategy": "smart_filtering",
            },
        },
    }


def create_rule(project: AIProjectClient, agent: str, eval_id: str, max_hourly: int) -> str:
    from azure.ai.projects.models import (
        ContinuousEvaluationRuleAction,
        EvaluationRule,
        EvaluationRuleEventType,
        EvaluationRuleFilter,
    )

    rule = project.evaluation_rules.create_or_update(
        id=f"{agent}-continuous",
        evaluation_rule=EvaluationRule(
            display_name=f"{agent} continuous evaluation",
            description="Quality + safety evaluators on completed production responses.",
            action=ContinuousEvaluationRuleAction(eval_id=eval_id, max_hourly_runs=max_hourly),
            event_type=EvaluationRuleEventType.RESPONSE_COMPLETED,
            filter=EvaluationRuleFilter(agent_name=agent),
            enabled=True,
        ),
    )
    return f"evaluation rule {rule.id}"


def create_schedule(project: AIProjectClient, agent: str, eval_id: str, every_hours: int, max_traces: int) -> str:
    from azure.ai.projects.models import (
        EvaluationScheduleTask,
        HourlyRecurrenceSchedule,
        RecurrenceTrigger,
        Schedule,
    )

    eval_run = trace_run(agent, eval_id, every_hours, max_traces)
    schedule = Schedule(
        display_name=f"{agent} production trace evaluation (every {every_hours}h)",
        enabled=True,
        trigger=RecurrenceTrigger(interval=every_hours, schedule=HourlyRecurrenceSchedule()),
        task=EvaluationScheduleTask(eval_id=eval_id, eval_run=eval_run),
    )
    resp = project.beta.schedules.create_or_update(schedule_id=f"{agent}-trace-eval", schedule=schedule)
    return f"schedule {resp.schedule_id}"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--agent", default=os.environ.get("AGENT_NAME", "clinical-agent"))
    p.add_argument("--judge", default=os.environ.get("AZURE_AI_JUDGE_DEPLOYMENT_NAME", "gpt-5.4"))
    p.add_argument("--mode", choices=("auto", "rule", "schedule", "off"), default="rule",
                   help="rule (default); auto = rule, else schedule; schedule (experimental); off = delete both")
    p.add_argument("--every-hours", type=int, default=1)
    p.add_argument("--max-traces", type=int, default=50)
    p.add_argument("--max-hourly-runs", type=int, default=100)
    p.add_argument("--run-now", action="store_true", help="also run one trace evaluation immediately")
    p.add_argument("--lookback-hours", type=int, default=24, help="window for --run-now")
    a = p.parse_args()

    project = AIProjectClient(endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"], credential=make_credential())
    oai = project.get_openai_client(max_retries=8)

    if a.mode == "off":
        for kind, delete in (("schedule", lambda: project.beta.schedules.delete(f"{a.agent}-trace-eval")),
                             ("evaluation rule", lambda: project.evaluation_rules.delete(f"{a.agent}-continuous"))):
            try:
                delete()
                print(f"deleted {kind}")
            except HttpResponseError as exc:
                print(f"no {kind} to delete ({exc.status_code})")
        return 0

    if a.mode in ("auto", "rule"):
        ev = oai.evals.create(
            name=f"{a.agent} - continuous (rule)",
            data_source_config={"type": "azure_ai_source", "scenario": "responses"},  # type: ignore[arg-type]
            testing_criteria=criteria(a.judge, traces=False),  # type: ignore[arg-type]
        )
        try:
            print("created", create_rule(project, a.agent, ev.id, a.max_hourly_runs), "->", ev.id)
            return 0
        except HttpResponseError as exc:
            if a.mode == "rule" or "not supported" not in str(exc):
                raise
            print("evaluation rules unsupported for this agent kind; using a scheduled trace evaluation")

    ev = oai.evals.create(
        name=f"{a.agent} - continuous (production traces)",
        data_source_config={"type": "azure_ai_source", "scenario": "traces"},  # type: ignore[arg-type]
        testing_criteria=criteria(a.judge, traces=True),  # type: ignore[arg-type]
    )
    print("created", create_schedule(project, a.agent, ev.id, a.every_hours, a.max_traces), "->", ev.id)
    if a.run_now:
        run_kw = trace_run(a.agent, ev.id, a.lookback_hours, a.max_traces)
        run = oai.evals.runs.create(eval_id=ev.id, name=run_kw["name"] + "-now",
                                    data_source=run_kw["data_source"])  # type: ignore[arg-type]
        while run.status not in ("completed", "failed", "canceled"):
            time.sleep(10)
            run = oai.evals.runs.retrieve(run_id=run.id, eval_id=ev.id)
        print(f"immediate run {run.status}: {run.result_counts} {getattr(run, 'report_url', '')}")
        if run.status != "completed":
            print(f"error: {run.error}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
