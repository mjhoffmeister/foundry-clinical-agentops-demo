<#
.SYNOPSIS
  Demo act 2: a well-intentioned prompt change that silently drops clinical facts.
.DESCRIPTION
  Creates branch demo/concise-answers, replaces the system prompt with the
  "concise" variant (PROMPT_VERSION 1.1.0) and opens a PR labelled "demo".
  CI deploys it as a candidate version (production untouched) and the eval
  gate should FAIL on required-facts recall / response completeness.
#>
param([switch]$NoPr)
. "$PSScriptRoot/_common.ps1"
Push-Location $RepoRoot
try {
    Assert-CleanTree
    Initialize-Gh
    Invoke-Git fetch origin main --quiet
    Invoke-Git switch -C $DemoBranch origin/main
    Copy-Item demo/prompts/concise-break.md $PromptPath -Force
    Invoke-Git add $PromptPath
    Invoke-Git commit -m 'Make clinical answers more concise for clinicians on mobile' -m 'Feedback from the nursing pilot: answers are too long to read between patients. Ask the model for 2-3 sentence answers.' --quiet
    Invoke-Git push -u origin $DemoBranch --force --quiet
    if (-not $NoPr) {
        gh label create demo --color FBCA04 --description 'Demo PRs closed by demo/reset.ps1' 2>$null | Out-Null
        $body = @'
Clinicians in the pilot said answers are too long to read on a phone between patients.
This asks the agent for 2-3 sentence answers and to skip background detail.

Prompt version 1.0.0 -> 1.1.0. No code changes.
'@
        Invoke-Native gh pr create --base main --head $DemoBranch --label demo --title 'Shorter answers for mobile clinicians' --body $body
        gh pr view $DemoBranch --web 2>$null
    }
    Write-Host "`nBreak PR opened. Watch the 'eval-gate' check: it deploys a candidate version and should fail." -ForegroundColor Yellow
} finally { Pop-Location }
