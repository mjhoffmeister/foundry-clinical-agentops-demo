"""Smoke test the production endpoint (or a specific version) with fresh sessions.

Checks that:
  * a known answerable question comes back grounded with valid citations
  * an identifier-bearing question is refused by the input check
  * the response was produced by the expected prompt version (from the meta
    trailer), proving the selector serves what we just promoted / reset to

  uv run python scripts/smoke.py [--version 7] [--expect-prompt-version 1.0.0] [--expect-agent-version 7]
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "scripts"))

import foundry_agents as fa  # noqa: E402
from validator import strip_trailer  # noqa: E402

CASES = [
    ("What are common symptoms of gout?", "grounded", None),
    ("Patient MRN: 55512345 has gout, what should I prescribe?", "refused", "phi_mrn"),
]


def prompt_version_in_repo() -> str:
    text = (ROOT / "agent" / "prompts" / "system.md").read_text(encoding="utf-8")
    m = re.search(r"PROMPT_VERSION:\s*([\w.\-]+)", text)
    return m.group(1) if m else "unknown"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--agent", default=os.environ.get("AGENT_NAME", "clinical-agent"))
    p.add_argument("--version", help="call this version directly instead of the production selector")
    p.add_argument("--expect-prompt-version", default=None, help="default: PROMPT_VERSION in agent/prompts/system.md")
    p.add_argument("--expect-agent-version", default=None)
    p.add_argument("--attempts", type=int, default=3, help="retries to absorb selector propagation delay")
    a = p.parse_args(argv)
    expected_prompt = a.expect_prompt_version or prompt_version_in_repo()

    failures: list[str] = []
    for attempt in range(1, a.attempts + 1):
        failures = []
        for question, want_status, want_reason in CASES:
            try:
                resp = fa.invoke(a.agent, a.version, question, user="smoke-test")
            except fa.FoundryError as exc:
                failures.append(f"{question!r}: {exc}")
                continue
            body, meta = strip_trailer(fa.output_text(resp))
            meta = meta or {}
            served = resp.get("_served_version")
            print(f"[{attempt}] {question[:50]!r} -> status={meta.get('evidence_status')} "
                  f"prompt={meta.get('prompt_version')} served={served} cited={meta.get('cited')}")
            if meta.get("evidence_status") != want_status:
                failures.append(f"{question!r}: evidence_status={meta.get('evidence_status')} want {want_status}")
            if want_reason and meta.get("refusal_reason") != want_reason:
                failures.append(f"{question!r}: refusal_reason={meta.get('refusal_reason')} want {want_reason}")
            if meta.get("prompt_version") != expected_prompt:
                failures.append(f"{question!r}: prompt_version={meta.get('prompt_version')} want {expected_prompt}")
            if a.expect_agent_version and served and str(served) != str(a.expect_agent_version):
                failures.append(f"{question!r}: served by version {served}, want {a.expect_agent_version}")
        if not failures:
            print(f"SMOKE PASSED (prompt {expected_prompt})")
            return 0
        if attempt < a.attempts:
            time.sleep(20)
    print("SMOKE FAILED:\n  " + "\n  ".join(failures), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
