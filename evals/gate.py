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
sys.path.insert(0, str(ROOT / "evals"))
import profiles  # noqa: E402


def expected_rows() -> int:
    total = 0
    for name in ("golden.jsonl", "safety.jsonl"):
        with open(ROOT / "evals" / "datasets" / name, encoding="utf-8") as fh:
            total += sum(1 for line in fh if line.strip())
    return total


def evaluate(results: dict, thresholds: dict, *, include_cloud: bool = True,
             rows_expected: int | None = None) -> tuple[bool, list[dict]]:
    checks: list[dict] = []
    counts = results.get("counts")
    counts = counts if isinstance(counts, dict) else {}
    if rows_expected is not None:
        checks.append({"metric": "row_count", "value": counts.get("total"), "rule": f"== {rows_expected}",
                       "ok": counts.get("total") == rows_expected})
    err_rate = counts.get("error_rate")
    max_err = thresholds.get("max_error_rate", 0.05)
    checks.append(
        {"metric": "error_rate", "value": err_rate, "rule": f"<= {max_err}",
         "ok": err_rate is not None and err_rate <= max_err}
    )

    deterministic = results.get("deterministic")
    sections = [("deterministic", deterministic if isinstance(deterministic, dict) else {})]
    if include_cloud:
        cloud = results.get("cloud")
        cloud = cloud if isinstance(cloud, dict) else {}
        cloud_metrics = cloud.get("metrics")
        sections.append(("cloud", cloud_metrics if isinstance(cloud_metrics, dict) else {}))
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


def render(
    results: dict,
    passed: bool,
    checks: list[dict],
    baseline: dict | None = None,
    profile: str = "full",
) -> str:
    verdict = "✅ PASSED" if passed else "❌ FAILED"
    title = "Quick demo evaluation" if profile == "quick" else "Eval gate"
    base_vals: dict = {}
    if baseline and profile == "full":
        base_vals = {**(baseline.get("deterministic") or {}), **((baseline.get("cloud") or {}).get("metrics") or {}),
                     "error_rate": (baseline.get("counts") or {}).get("error_rate")}
    elif profile == "quick":
        baseline = None
    head = f"| | Metric | Value | Threshold |{' Production v' + str(baseline.get('version')) + ' |' if baseline else ''}"
    sep = "|---|---|---|---|" + ("---|" if baseline else "")
    lines = [f"## {title} {verdict} - `{results.get('agent')}` version `{results.get('version')}`", ""]
    if profile == "quick":
        lines.extend([
            "**Demo evaluation - not release approval.**",
            "",
            "Coverage: 12 curated rows; clinical code checks, groundedness, and response completeness.",
            "Task adherence and content-safety judges: not evaluated in quick profile.",
            "",
        ])
    lines.extend([head, sep])
    for c in checks:
        row = f"| {'✅' if c['ok'] else '❌'} | {c['metric']} | {c['value']} | {c['rule']} |"
        if baseline:
            row += f" {_delta(c['value'], base_vals.get(c['metric']))} |"
        lines.append(row)
    cloud = results.get("cloud")
    cloud = cloud if isinstance(cloud, dict) else {}
    report_urls = cloud.get("report_urls")
    if isinstance(report_urls, dict):
        for kind, url in report_urls.items():
            if url:
                lines.append(f"\n[Open the Foundry {kind} evaluation report]({url})")
    usage = cloud.get("usage")
    tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
    if tokens:
        lines.append(f"\nEvaluation tokens for this gate run: **{tokens:,}**")
    return "\n".join(lines) + "\n"


def profile_error(results: dict, profile: str) -> str | None:
    try:
        profiles.validate_profile(profile)
        metadata_profile = results.get("evaluation_profile")
        eligible = results.get("release_eligible")
        if not isinstance(results.get("counts"), dict):
            return "Evaluation results are missing valid counts"
        if profile == "full":
            if metadata_profile == "quick" or eligible is False:
                return "Quick or release-ineligible results cannot be used as a full release evaluation"
            if metadata_profile not in (None, "full"):
                return f"Result profile {metadata_profile!r} does not match requested full profile"
            if metadata_profile == "full" and eligible is not True:
                return "Full-profile result is not explicitly marked release eligible"
            return None
        if metadata_profile != "quick":
            return "Quick gate requires results explicitly marked with evaluation_profile='quick'"
        if eligible is not False:
            return "Quick results must explicitly declare release_eligible=false"
        rows = results.get("rows")
        if not isinstance(rows, list):
            return "Quick results must contain the complete curated row list"
        profiles.validate_result_rows(rows, "quick")
        if results["counts"].get("total") != len(profiles.QUICK_ROW_IDS):
            return "Quick result count does not match the 12-row curated suite"
    except profiles.ProfileError as exc:
        return str(exc)
    return None


def write_failure(message: str, profile: str = "full") -> None:
    title = "Quick demo evaluation" if profile == "quick" else "Eval gate"
    warning = "\n\n**Demo evaluation - not release approval.**" if profile == "quick" else ""
    text = f"## {title} FAILED{warning}\n\n{message}\n"
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(text)


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
    p.add_argument("--profile", choices=profiles.PROFILES, default="full")
    a = p.parse_args(argv)

    try:
        results = json.loads(Path(a.results).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Eval gate FAILED: cannot read results ({exc})", file=sys.stderr)
        return 1
    if not isinstance(results, dict):
        write_failure("Evaluation results must be a JSON object.", a.profile)
        return 1
    if a.profile == "quick" and (a.allow_partial or a.no_cloud or a.baseline):
        write_failure("Quick profile does not allow --allow-partial, --no-cloud, or --baseline.", a.profile)
        return 1
    error = profile_error(results, a.profile)
    if error:
        cloud = results.get("cloud")
        cloud_errors = cloud.get("errors") or [] if isinstance(cloud, dict) else []
        if cloud_errors:
            error += "; evaluation errors: " + "; ".join(str(item) for item in cloud_errors)
        write_failure(error, a.profile)
        return 1
    default_thresholds = (ROOT / "evals" / "thresholds.yaml").resolve()
    if a.profile == "quick" and Path(a.thresholds).resolve() != default_thresholds:
        write_failure(
            "Quick profile uses the checked-in release thresholds and does not accept a custom threshold file.",
            a.profile,
        )
        return 1
    baseline = None
    if a.baseline:
        try:
            baseline = json.loads(Path(a.baseline).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            baseline = None
    try:
        thresholds = profiles.thresholds_for(yaml.safe_load(Path(a.thresholds).read_text(encoding="utf-8")), a.profile)
    except (OSError, yaml.YAMLError, profiles.ProfileError) as exc:
        write_failure(f"Cannot load thresholds for {a.profile} profile: {exc}", a.profile)
        return 1
    rows_expected = None if a.allow_partial else len(profiles.QUICK_ROW_IDS) if a.profile == "quick" else expected_rows()
    passed, checks = evaluate(results, thresholds, include_cloud=not a.no_cloud, rows_expected=rows_expected)
    md = render(results, passed, checks, baseline, a.profile)
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
