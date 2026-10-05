<#
.SYNOPSIS
  Demo act 3: keep the concise format but require the key facts.
.DESCRIPTION
  Pushes the corrected prompt (PROMPT_VERSION 1.1.1) to the open demo PR.
  The eval gate re-runs against a new candidate version and should PASS.
  Then merge the PR -> release workflow -> approve "production" -> pinned.
#>
. "$PSScriptRoot/_common.ps1"
Push-Location $RepoRoot
try {
    Initialize-Gh
    Invoke-Git fetch origin $DemoBranch --quiet
    Invoke-Git switch $DemoBranch
    Invoke-Git reset --hard "origin/$DemoBranch" --quiet
    Copy-Item demo/prompts/concise-fix.md $PromptPath -Force
    Invoke-Git add $PromptPath
    Invoke-Git commit -m 'Concise format without dropping key facts' -m 'Lead with a one-sentence answer, then up to 5 cited bullets. Never omit key facts (causes, symptoms, treatment, when to seek care) to save space.' --quiet
    Invoke-Git push origin $DemoBranch --quiet
    Write-Host "`nFix pushed. The eval gate re-runs on a new candidate version and should pass." -ForegroundColor Green
    Write-Host 'Then: merge the PR, approve the production deployment in the release run.'
} finally { Pop-Location }
