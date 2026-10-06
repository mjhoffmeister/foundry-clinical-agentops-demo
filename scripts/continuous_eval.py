"""Continuous evaluation of production traffic for the hosted agent.

Foundry *evaluation rules* (score every RESPONSE_COMPLETED event) reject hosted
agents ("Hosted and external agents are not supported"), so continuous
evaluation uses a Foundry *scheduled trace evaluation* - the "Recurring
evaluations" feature of the agent's Monitor tab. Every N hours Foundry pulls the
production agent's `invoke_agent` spans from Application Insights (data source
`azure_ai_traces`) and scores them with quality + safety evaluators. Results
appear on the agent's Monitor and Evaluations tabs.

Requirements (see infra/rbac.tf): the project managed identity needs Reader,
Log Analytics Reader and Privileged Monitoring Data Reader on Application
Insights. Without Reader, runs fail with "Something went wrong during data
generation" (or, with older data source types, hang in_progress).

`azure_ai_traces` matches gen_ai.agent.id exactly (`<agent>:<version>`), so the
schedule targets the *pinned production version*. Re-run this script after the
pinned version changes (demo/prep.ps1 does it).

Criteria are deliberately few. The platform's invoke_agent span carries the
system prompt + user turn and the final answer, but not the hosted agent's
knowledge-base tool calls or retrieved passages. So task_adherence ("no tool
call" false negatives) and groundedness (no evidence to ground against) are
left to the CI gate, which has full tool context. Coherence adds little.

Token cost: each trace yields 1-3 items (tool-call turns appear as items with an
empty response) and every item carries the ~1K-token system prompt, so a run
costs roughly 35K evaluation tokens per trace (about 90% of it the
Microsoft-hosted content-safety evaluators). Defaults keep this to one small run
per day (~350K tokens): --every-hours 24 starting 11:00 UTC, --max-traces 10.
The schedule start time is always in the future, because a start time of "now"
makes every update fire a run; an unchanged configuration is not re-submitted.

    uv run python scripts/continuous_eval.py [--mode schedule|rule|off] [--version N]
        [--every-hours 24] [--max-traces 10] [--force] [--run-now [--wait]]
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from azure.ai.projects import AIProjectClient
from azure.core.exceptions import HttpResponseError

sys.path.insert(0, str(Path(__file__).resolve().parent))
from foundry_agents import make_credential, pinned_version  # noqa: E402

QUALITY = ("intent_resolution",)
SAFETY = ("violence", "self_harm", "hate_unfairness")


def criteria(judge: str) -> list[dict]:
    mapping = {"query": "{{item.query}}", "response": "{{item.response}}"}
    return [
        {
            "type": "azure_ai_evaluator",
            "name": n,
            "evaluator_name": f"builtin.{n}",
            "initialization_parameters": {"model": judge},
            "data_mapping": mapping,
        }
        for n in QUALITY + SAFETY
    ]


RUN_HOUR_UTC = 11  # 6 AM US Central


def next_run_time() -> datetime:
    now = datetime.now(UTC)
    nxt = now.replace(hour=RUN_HOUR_UTC, minute=0, second=0, microsecond=0)
    return nxt if nxt > now + timedelta(minutes=5) else nxt + timedelta(days=1)


def eval_name(agent: str) -> str:
    return f"{agent} - continuous (production traces)"


def trace_source(agent: str, version: str, lookback_hours: int, max_traces: int) -> dict:
    return {
        "type": "azure_ai_traces",
        "agent_id": f"{agent}:{version}",
        "lookback_hours": lookback_hours,
        "max_traces": max_traces,
    }


def get_or_create_eval(oai, agent: str, judge: str) -> str:
    wanted = sorted(c["name"] for c in criteria(judge))
    for ev in oai.evals.list(limit=100, order="desc"):
        if ev.name == eval_name(agent):
            have = sorted(getattr(c, "name", None) or c["name"] for c in ev.testing_criteria or [])
            if have == wanted:
                return ev.id
    return oai.evals.create(
        name=eval_name(agent),
        data_source_config={"type": "azure_ai_source", "scenario": "traces"},  # type: ignore[arg-type]
        testing_criteria=criteria(judge),  # type: ignore[arg-type]
    ).id


def create_schedule(
    project: AIProjectClient,
    agent: str,
    version: str,
    eval_id: str,
    every_hours: int,
    lookback_hours: int,
    max_traces: int,
    force: bool = False,
) -> str:
    from azure.ai.projects.models import (
        EvaluationScheduleTask,
        HourlyRecurrenceSchedule,
        RecurrenceTrigger,
        Schedule,
    )

    run = {
        "eval_id": eval_id,
        "name": f"{agent}-v{version}-production-traces",
        "data_source": trace_source(agent, version, lookback_hours, max_traces),
    }
    schedule = Schedule(
        display_name=f"{agent} v{version} production trace evaluation (every {every_hours}h)",
        enabled=True,
        # A start time of "now" (the service default) fires a run on every update; start at
        # the next RUN_HOUR_UTC instead so updates are free and runs land before the workday.
        trigger=RecurrenceTrigger(
            interval=every_hours, schedule=HourlyRecurrenceSchedule(), start_time=next_run_time()
        ),
        task=EvaluationScheduleTask(eval_id=eval_id, eval_run=run),
    )
    schedule_id = f"{agent}-trace-eval"
    if not force:
        try:
            cur = project.beta.schedules.get(schedule_id).as_dict()
            task, trig = cur.get("task") or {}, cur.get("trigger") or {}
            ds = (task.get("evalRun") or {}).get("data_source") or {}
            if (
                cur.get("enabled")
                and task.get("evalId") == eval_id
                and trig.get("interval") == every_hours
                and ds.get("agent_id") == f"{agent}:{version}"
                and ds.get("max_traces") == max_traces
            ):
                return f"unchanged schedule {schedule_id}"
        except HttpResponseError as exc:
            if exc.status_code != 404:
                raise
    resp = project.beta.schedules.create_or_update(schedule_id=schedule_id, schedule=schedule)
    return f"created schedule {resp.schedule_id}"


def create_rule(project: AIProjectClient, agent: str, judge: str, oai) -> str:
    """Per-response evaluation rule. Rejected by the service for hosted agents (kept to demonstrate that)."""
    from azure.ai.projects.models import (
        ContinuousEvaluationRuleAction,
        EvaluationRule,
        EvaluationRuleEventType,
        EvaluationRuleFilter,
    )

    ev = oai.evals.create(
        name=f"{agent} - continuous (rule)",
        data_source_config={"type": "azure_ai_source", "scenario": "responses"},  # type: ignore[arg-type]
        testing_criteria=[{k: v for k, v in c.items() if k != "data_mapping"} for c in criteria(judge)],  # type: ignore[arg-type]
    )
    rule = project.evaluation_rules.create_or_update(
        id=f"{agent}-continuous",
        evaluation_rule=EvaluationRule(
            display_name=f"{agent} continuous evaluation",
            action=ContinuousEvaluationRuleAction(eval_id=ev.id, max_hourly_runs=100),
            event_type=EvaluationRuleEventType.RESPONSE_COMPLETED,
            filter=EvaluationRuleFilter(agent_name=agent),
            enabled=True,
        ),
    )
    return f"evaluation rule {rule.id}"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--agent", default=os.environ.get("AGENT_NAME", "clinical-agent"))
    p.add_argument("--judge", default=os.environ.get("AZURE_AI_JUDGE_DEPLOYMENT_NAME", "gpt-5.4"))
    p.add_argument("--mode", choices=("schedule", "rule", "off"), default="schedule")
    p.add_argument("--version", help="agent version to evaluate (default: pinned production version)")
    p.add_argument("--every-hours", type=int, default=24, help="schedule interval; scheduled runs look back this far")
    p.add_argument("--lookback-hours", type=int, default=24, help="window for --run-now")
    p.add_argument("--max-traces", type=int, default=10)
    p.add_argument("--force", action="store_true", help="update the schedule even if unchanged")
    p.add_argument("--run-now", action="store_true", help="also start one trace evaluation immediately")
    p.add_argument("--wait", action="store_true", help="with --run-now: wait for the run and fail if it fails")
    a = p.parse_args()

    project = AIProjectClient(endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"], credential=make_credential())
    oai = project.get_openai_client(max_retries=8)

    if a.mode == "off":
        for kind, delete in (
            ("schedule", lambda: project.beta.schedules.delete(f"{a.agent}-trace-eval")),
            ("evaluation rule", lambda: project.evaluation_rules.delete(f"{a.agent}-continuous")),
        ):
            try:
                delete()
                print(f"deleted {kind}")
            except HttpResponseError as exc:
                print(f"no {kind} to delete ({exc.status_code})")
        return 0

    if a.mode == "rule":
        print("created", create_rule(project, a.agent, a.judge, oai))
        return 0

    version = a.version or pinned_version(a.agent)
    if not version:
        print("error: no single pinned production version; pass --version", file=sys.stderr)
        return 1
    eval_id = get_or_create_eval(oai, a.agent, a.judge)
    print(
        create_schedule(project, a.agent, version, eval_id, a.every_hours, a.every_hours, a.max_traces, a.force),
        f"-> {eval_id} ({a.agent}:{version}, every {a.every_hours}h, max {a.max_traces} traces)",
    )
    if a.run_now:
        run = oai.evals.runs.create(
            eval_id=eval_id,
            name=f"{a.agent}-v{version}-production-traces-now",
            data_source=trace_source(a.agent, version, a.lookback_hours, a.max_traces),
        )  # type: ignore[arg-type]
        print(f"started run {run.id}")
        if a.wait:
            while run.status not in ("completed", "failed", "canceled"):
                time.sleep(15)
                run = oai.evals.runs.retrieve(run_id=run.id, eval_id=eval_id)
            print(f"run {run.status}: {run.result_counts} {getattr(run, 'report_url', '')}")
            if run.status != "completed":
                print(f"error: {run.error}", file=sys.stderr)
                return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
