import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PWSH = shutil.which("pwsh")
pytestmark = pytest.mark.skipif(not PWSH or os.name != "nt", reason="requires PowerShell on Windows")

MOCK_GH = r"""
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
Path(os.environ["GH_MOCK_LOG"]).open("a", encoding="utf-8").write(json.dumps(args) + "\n")
fail = os.environ.get("GH_MOCK_FAIL_ON")
if fail == "auth" and args[:2] == ["auth", "token"]:
    raise SystemExit(2)
if fail == "repo" and args[:2] == ["repo", "view"]:
    raise SystemExit(3)
if fail == "list" and args[:2] == ["variable", "list"]:
    raise SystemExit(4)
if fail == "set" and args[:2] == ["variable", "set"]:
    raise SystemExit(5)

if args[:2] == ["auth", "token"]:
    print("mock-token")
elif args[:2] == ["repo", "view"]:
    print("mjhoffmeister/foundry-clinical-agentops-demo")
elif args[:2] == ["variable", "list"]:
    if args != [
        "variable", "list", "--repo", "mjhoffmeister/foundry-clinical-agentops-demo",
        "--json", "name,value",
    ]:
        raise SystemExit(7)
    state_path = Path(os.environ["GH_MOCK_STATE_FILE"])
    print(json.dumps(json.loads(state_path.read_text(encoding="utf-8"))))
elif args[:2] == ["variable", "set"]:
    state_path = Path(os.environ["GH_MOCK_STATE_FILE"])
    variables = json.loads(state_path.read_text(encoding="utf-8"))
    name = args[2]
    body = args[args.index("--body") + 1]
    variables = [item for item in variables if item["name"] != name]
    variables.append({"name": name, "value": body})
    state_path.write_text(json.dumps(variables), encoding="utf-8")
    print(name)
else:
    raise SystemExit(6)
"""


def harness(tmp_path, variables=None, fail_on=None):
    repo = tmp_path / "repo"
    demo = repo / "demo"
    bin_dir = tmp_path / "bin"
    demo.mkdir(parents=True)
    bin_dir.mkdir()
    shutil.copy2(ROOT / "demo" / "evals.ps1", demo / "evals.ps1")
    shutil.copy2(ROOT / "demo" / "_common.ps1", demo / "_common.ps1")
    mock_py = bin_dir / "gh_mock.py"
    mock_py.write_text(MOCK_GH, encoding="utf-8")
    (bin_dir / "gh.cmd").write_text(
        '@echo off\r\npython "%~dp0gh_mock.py" %*\r\n', encoding="utf-8",
    )
    state_file = tmp_path / "variables.json"
    state_file.write_text(json.dumps(variables or []), encoding="utf-8")
    log = tmp_path / "gh-args.jsonl"
    env = os.environ.copy()
    env.pop("GH_TOKEN", None)
    env.update({
        "PATH": str(bin_dir) + os.pathsep + env["PATH"],
        "GH_MOCK_STATE_FILE": str(state_file),
        "GH_MOCK_LOG": str(log),
    })
    if fail_on:
        env["GH_MOCK_FAIL_ON"] = fail_on
    return repo, demo / "evals.ps1", state_file, log, env


def run_script(script, env, *args):
    return subprocess.run(
        [PWSH, "-NoProfile", "-File", str(script), *args],
        cwd=script.parent.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_status_is_read_only_and_unset_means_full(tmp_path):
    _, script, state_file, log, env = harness(tmp_path)
    result = run_script(script, env)
    assert result.returncode == 0, result.stderr
    assert "Stored PR_EVAL_PROFILE: (unset)" in result.stdout
    assert "Effective PR evaluation profile: full" in result.stdout
    assert json.loads(state_file.read_text(encoding="utf-8")) == []
    commands = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert not any(command[:2] == ["variable", "set"] for command in commands)
    assert ["variable", "list", "--repo", "mjhoffmeister/foundry-clinical-agentops-demo",
            "--json", "name,value"] in commands


@pytest.mark.parametrize("profile", ["full", "quick"])
def test_status_reports_a_valid_stored_profile(tmp_path, profile):
    _, script, _, _, env = harness(tmp_path, [{"name": "PR_EVAL_PROFILE", "value": profile}])
    result = run_script(script, env)
    assert result.returncode == 0, result.stderr
    assert f"Effective PR evaluation profile: {profile}" in result.stdout


def test_setting_profile_writes_and_confirms_the_variable(tmp_path):
    _, script, state_file, log, env = harness(tmp_path)
    result = run_script(script, env, "-Profile", "quick")
    assert result.returncode == 0, result.stderr
    assert json.loads(state_file.read_text(encoding="utf-8")) == [
        {"name": "PR_EVAL_PROFILE", "value": "quick"},
    ]
    assert "Effective PR evaluation profile: quick" in result.stdout
    commands = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert any(command[:3] == ["variable", "set", "PR_EVAL_PROFILE"] for command in commands)


def test_status_rejects_invalid_stored_value_but_setting_can_repair_it(tmp_path):
    invalid = [{"name": "PR_EVAL_PROFILE", "value": "Quick"}]
    _, script, state_file, _, env = harness(tmp_path, invalid)
    status = run_script(script, env)
    assert status.returncode != 0
    assert "unsupported value" in status.stderr

    repaired = run_script(script, env, "-Profile", "full")
    assert repaired.returncode == 0, repaired.stderr
    assert json.loads(state_file.read_text(encoding="utf-8")) == [
        {"name": "PR_EVAL_PROFILE", "value": "full"},
    ]


@pytest.mark.parametrize("failure", ["auth", "repo", "list", "set"])
def test_github_errors_are_not_reported_as_unset_or_success(tmp_path, failure):
    _, script, _, _, env = harness(tmp_path, fail_on=failure)
    args = ("-Profile", "quick") if failure == "set" else ()
    result = run_script(script, env, *args)
    assert result.returncode != 0
    assert "Stored PR_EVAL_PROFILE: (unset)" not in result.stdout
