<#
.SYNOPSIS
  Put the demo back to its starting state so it can be run again.
.DESCRIPTION
  1. Closes open PRs labelled "demo" and deletes their branches.
  2. Restores agent/prompts/system.md on main from tag demo-baseline (commit
     with [skip ci], pushed by the repo admin - the workflow token cannot push
     to protected main by design).
  3. Pins production back to the baseline agent version (demo/baseline.json),
     locally or via the reset workflow (-ViaWorkflow, needs approval).
  4. Smoke tests production.
  Agent versions created during the demo are kept: they are the audit trail.
#>
param([switch]$ViaWorkflow, [switch]$SkipPin)
. "$PSScriptRoot/_common.ps1"
Push-Location $RepoRoot
try {
    Initialize-Gh
    $baseline = Get-Baseline

    Write-Host '1/4 Closing demo PRs' -ForegroundColor Cyan
    $prs = gh pr list --label demo --state open --json number,headRefName | ConvertFrom-Json
    foreach ($pr in $prs) {
        Invoke-Native gh pr close $pr.number --delete-branch --comment 'Closed by demo reset.'
    }
    git push origin --delete $DemoBranch 2>$null | Out-Null

    Write-Host '2/4 Restoring baseline prompt on main' -ForegroundColor Cyan
    Assert-CleanTree
    Invoke-Native git fetch origin main --tags --force --quiet
    Invoke-Native git switch main --quiet
    Invoke-Native git reset --hard origin/main --quiet
    git branch -D $DemoBranch 2>$null | Out-Null
    Invoke-Native git checkout demo-baseline -- $PromptPath
    if (git status --porcelain -- $PromptPath) {
        Invoke-Native git add $PromptPath
        Invoke-Native git commit -m "[skip ci] demo reset: restore prompt $($baseline.prompt_version)" --quiet
        Invoke-Native git push origin main --quiet
    } else {
        Write-Host '   prompt already at baseline'
    }

    if (-not $SkipPin) {
        Write-Host "3/4 Pinning production to v$($baseline.agent_version)" -ForegroundColor Cyan
        if ($ViaWorkflow) {
            Invoke-Native gh workflow run reset.yml --ref main
            Write-Host '   reset workflow started - approve the production deployment in GitHub.'
            return
        }
        Import-AzdEnv
        Invoke-Tools scripts/foundry_agents.py pin --version $baseline.agent_version
    }

    Write-Host '4/4 Smoke test' -ForegroundColor Cyan
    Import-AzdEnv
    Invoke-Tools scripts/smoke.py --expect-agent-version $baseline.agent_version --expect-prompt-version $baseline.prompt_version
    Write-Host "`nDemo reset complete: production = v$($baseline.agent_version), prompt $($baseline.prompt_version)." -ForegroundColor Green
} finally { Pop-Location }
