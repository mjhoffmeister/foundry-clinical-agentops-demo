<#
.SYNOPSIS
  Pre-demo readiness check (run ~1 hour before, and again 10 minutes before).
.DESCRIPTION
  - Verifies Azure + GitHub sign-in and the azd environment
  - Verifies production is on the baseline version and no demo PR is open
  - Smoke tests production
  - Seeds traffic so monitoring / traces have fresh data (-SkipSeed to skip)
  - Points the continuous (trace) evaluation schedule at the baseline version and,
    after seeding, starts one run
  - Prints the links used in the demo script
  -RecordBaseline pins nothing; it writes the CURRENT production version and
  prompt into demo/baseline.json and moves the demo-baseline tag to HEAD.
#>
param([switch]$SkipSeed, [switch]$RecordBaseline, [int]$Rounds = 1)
. "$PSScriptRoot/_common.ps1"
Push-Location $RepoRoot
try {
    $ok = $true
    function Check($name, [scriptblock]$test) {
        try { $r = & $test; Write-Host ("  [ok]   {0} {1}" -f $name, $r) -ForegroundColor Green }
        catch { Write-Host ("  [FAIL] {0}: {1}" -f $name, $_.Exception.Message) -ForegroundColor Red; $script:ok = $false }
    }

    Write-Host 'Sign-in' -ForegroundColor Cyan
    Check 'azd env' { Import-AzdEnv; $env:FOUNDRY_PROJECT_ENDPOINT }
    Check 'Azure CLI' { $a = az account show --subscription $env:AZURE_SUBSCRIPTION_ID -o json | ConvertFrom-Json; if (-not $a) { throw "cannot access subscription $env:AZURE_SUBSCRIPTION_ID - run az login --tenant $env:AZURE_TENANT_ID" }; "$($a.user.name) ($($a.name))" }
    Check 'GitHub CLI' { Initialize-Gh; (gh api user --jq .login) }

    if ($RecordBaseline) {
        $sel = (uv run python scripts/foundry_agents.py selector | ConvertFrom-Json)
        $pv = ((Get-Content $PromptPath -TotalCount 1) -replace '.*PROMPT_VERSION:\s*([\w.\-]+).*', '$1')
        [ordered]@{ agent_name = $env:AGENT_NAME; agent_version = $sel.pinned_version; prompt_version = $pv;
            note = 'Production version the demo resets to. Update with demo/prep.ps1 -RecordBaseline after an intentional baseline change.' } |
            ConvertTo-Json | Set-Content demo/baseline.json -Encoding utf8NoBOM
        Invoke-Git tag -f demo-baseline
        Write-Host "Recorded baseline v$($sel.pinned_version) / prompt $pv. Commit demo/baseline.json, then: git push -f origin demo-baseline" -ForegroundColor Yellow
    }

    $baseline = Get-Baseline
    Write-Host 'Demo state' -ForegroundColor Cyan
    Check 'Production on baseline' {
        $sel = (uv run python scripts/foundry_agents.py selector | ConvertFrom-Json)
        if ($sel.pinned_version -ne $baseline.agent_version) { throw "pinned v$($sel.pinned_version), baseline v$($baseline.agent_version) - run demo/reset.ps1" }
        "v$($sel.pinned_version) (latest v$($sel.latest_version))"
    }
    Check 'No open demo PRs' { $n = (gh pr list --label demo --state open --json number | ConvertFrom-Json).Count; if ($n) { throw "$n open - run demo/reset.ps1" }; '' }
    Check 'Prompt on main = baseline' {
        $pv = ((Get-Content $PromptPath -TotalCount 1) -replace '.*PROMPT_VERSION:\s*([\w.\-]+).*', '$1')
        if ($pv -ne $baseline.prompt_version) { throw "main has $pv" }; $pv
    }
    Check 'Web app /healthz' { (Invoke-WebRequest "$env:WEB_APP_URL/healthz" -UseBasicParsing -TimeoutSec 60).StatusCode }

    Write-Host 'Smoke' -ForegroundColor Cyan
    Check 'Production smoke' { Invoke-Tools scripts/smoke.py --expect-agent-version $baseline.agent_version | Select-Object -Last 1 }

    if (-not $SkipSeed) {
        Write-Host 'Seeding traffic' -ForegroundColor Cyan
        Invoke-Tools demo/seed_traffic.py --rounds $Rounds
    }

    # Trace evaluations match gen_ai.agent.id exactly (clinical-agent:<version>), so the
    # schedule must follow the pinned version. After seeding, start one run so the Monitor
    # tab has fresh continuous-evaluation results (~5 min, not awaited).
    Write-Host 'Continuous evaluation' -ForegroundColor Cyan
    $ceArgs = @('scripts/continuous_eval.py', '--mode', 'schedule', '--version', $baseline.agent_version)
    if (-not $SkipSeed) { $ceArgs += '--run-now' }
    Check 'Trace evaluation schedule' { Invoke-Tools @ceArgs | Select-Object -Last 1 }

    $sub = $env:AZURE_SUBSCRIPTION_ID
    Write-Host "`nLinks" -ForegroundColor Cyan
    Write-Host "  Chat UI           $env:WEB_APP_URL"
    Write-Host '  Foundry portal    https://ai.azure.com  (project proj-clinical-agent -> Agents / Monitor / Evaluations / Guardrails)'
    Write-Host '  Operate (control) https://ai.azure.com/nextgen/operate'
    Write-Host "  GitHub repo       https://github.com/mjhoffmeister/foundry-clinical-agentops-demo"
    Write-Host "  App Insights      https://portal.azure.com/#@/resource$env:AZURE_APP_INSIGHTS_ID/overview"
    Write-Host "  Resource group    https://portal.azure.com/#@/resource/subscriptions/$sub/resourceGroups/$env:AZURE_RESOURCE_GROUP/overview"

    if (-not $ok) { Write-Host "`nNOT READY - fix the failures above." -ForegroundColor Red; exit 1 }
    Write-Host "`nREADY." -ForegroundColor Green
} finally { Pop-Location }
