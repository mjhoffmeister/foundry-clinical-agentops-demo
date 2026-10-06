"""Release gate: compare eval results with thresholds and fail closed.

  uv run python evals/gate.py evals/results/v3/results.json [--thresholds evals/thresholds.yaml] [--no-cloud]

Exit code 0 = pass, 1 = fail. Writes a markdown table to $GITHUB_STEP_SUMMARY
when running in GitHub Actions.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def expected_rows() -> int:
    total = 0
    for name in ("golden.jsonl", "safety.jsonl"):
        with open(ROOT / "evals" / "datasets" / name, encoding="utf-8") as fh:
            total += sum(1 for line in fh if line.strip())
    return total


def evaluate(results: dict, thresholds: dict, *, include_cloud: bool = True,
             rows_expected: int | None = None) -> tuple[bool, list[dict]]:
    checks: list[dict] = []
    counts = results.get("counts") or {}
    if rows_expected is not None:
        checks.append({"metric": "row_count", "value": counts.get("total"), "rule": f"== {rows_expected}",
                       "ok": counts.get("total") == rows_expected})
    err_rate = counts.get("error_rate")
    max_err = thresholds.get("max_error_rate", 0.05)
    checks.append(
        {"metric": "error_rate", "value": err_rate, "rule": f"<= {max_err}",
         "ok": err_rate is not None and err_rate <= max_err}
    )

    sections = [("deterministic", results.get("deterministic") or {})]
    if include_cloud:
        cloud = results.get("cloud") or {}
        sections.append(("cloud", cloud.get("metrics") or {}))
        for err in cloud.get("errors") or []:
            checks.append({"metric": "cloud_eval", "value": err[:80], "rule": "completes", "ok": False})

    for section, values in sections:
        for metric, rule in (thresholds.get(section) or {}).items():
            value = values.get(metric)
            if value is None:
                ok, text = False, "missing (fail closed)"
            elif "min" in rule:
                ok, text = value >= rule["min"], f">= {rule['min']}"
            elif "max" in rule:
                ok, text = value <= rule["max"], f"<= {rule['max']}"
            else:
                raise ValueError(f"threshold for {metric} needs min or max")
            checks.append({"metric": metric, "value": value, "rule": text, "ok": ok})
    return all(c["ok"] for c in checks), checks


def render(results: dict, passed: bool, checks: list[dict], baseline: dict | None = None) -> str:
    verdict = "✅ PASSED" if passed else "❌ FAILED"
    base_vals: dict = {}
    if baseline:
        base_vals = {**(baseline.get("deterministic") or {}), **((baseline.get("cloud") or {}).get("metrics") or {}),
                     "error_rate": (baseline.get("counts") or {}).get("error_rate")}
    head = f"| | Metric | Value | Threshold |{' Production v' + str(baseline.get('version')) + ' |' if baseline else ''}"
    sep = "|---|---|---|---|" + ("---|" if baseline else "")
    lines = [f"## Eval gate {verdict} - `{results.get('agent')}` version `{results.get('version')}`", "", head, sep]
    for c in checks:
        row = f"| {'✅' if c['ok'] else '❌'} | {c['metric']} | {c['value']} | {c['rule']} |"
        if baseline:
            row += f" {_delta(c['value'], base_vals.get(c['metric']))} |"
        lines.append(row)
    for kind, url in ((results.get("cloud") or {}).get("report_urls") or {}).items():
        if url:
            lines.append(f"\n[Open the Foundry {kind} evaluation report]({url})")
    tokens = ((results.get("cloud") or {}).get("usage") or {}).get("total_tokens")
    if tokens:
        lines.append(f"\nEvaluation tokens for this gate run: **{tokens:,}**")
    return "\n".join(lines) + "\n"


def _delta(value, base) -> str:
    if isinstance(value, (int, float)) and isinstance(base, (int, float)):
        d = value - base
        return f"{base} ({'+' if d >= 0 else ''}{round(d, 3)})"
    return "" if base is None else str(base)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("results")
    p.add_argument("--thresholds", default=str(ROOT / "evals" / "thresholds.yaml"))
    p.add_argument("--no-cloud", action="store_true", help="ignore cloud metrics (local smoke only)")
    p.add_argument("--baseline", help="results.json of the production version, for a delta column")
    p.add_argument("--allow-partial", action="store_true", help="skip the row-count check (results from --limit runs)")
    a = p.parse_args(argv)

    try:
        results = json.loads(Path(a.results).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Eval gate FAILED: cannot read results ({exc})", file=sys.stderr)
        return 1
    baseline = None
    if a.baseline:
        try:
            baseline = json.loads(Path(a.baseline).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            baseline = None
    thresholds = yaml.safe_load(Path(a.thresholds).read_text(encoding="utf-8"))
    passed, checks = evaluate(results, thresholds, include_cloud=not a.no_cloud,
                              rows_expected=None if a.allow_partial else expected_rows())
    md = render(results, passed, checks, baseline)
    print(md)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(md)
            details = Path(a.results).with_name("summary.md")
            if details.exists():
                fh.write("\n" + details.read_text(encoding="utf-8"))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
