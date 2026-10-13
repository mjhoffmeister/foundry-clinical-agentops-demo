<#
.SYNOPSIS
  Read or set the evaluation profile used by future same-repository PR runs.
.DESCRIPTION
  No arguments only reports PR_EVAL_PROFILE. Set -Profile to full or quick to
  update the current GitHub repository variable. Release and scheduled workflows
  always use full; this command never starts or reruns an evaluation.
#>
param(
    [ValidateSet('full', 'quick')]
    [string]$Profile
)

. "$PSScriptRoot/_common.ps1"
Initialize-Gh
$repository = Invoke-Native gh repo view --json nameWithOwner --jq .nameWithOwner
if (-not $repository) { throw 'Unable to determine the current GitHub repository.' }

function Get-PrEvalProfile {
    $json = Invoke-Native gh variable list --repo $repository --json 'name,value'
    $variables = @($json | ConvertFrom-Json)
    $match = $variables | Where-Object { $_.name -ceq 'PR_EVAL_PROFILE' } | Select-Object -First 1
    if ($null -eq $match) { return $null }
    return [string]$match.value
}

$stored = Get-PrEvalProfile
if ($PSBoundParameters.ContainsKey('Profile')) {
    Invoke-Native gh variable set PR_EVAL_PROFILE --repo $repository --body $Profile
    $stored = Get-PrEvalProfile
    if ($stored -cne $Profile) {
        throw "GitHub did not confirm PR_EVAL_PROFILE='$Profile' for $repository."
    }
} elseif ($stored -and $stored -cnotin @('full', 'quick')) {
    throw "PR_EVAL_PROFILE has unsupported value '$stored'; set it to full or quick."
}

$effective = if (-not $stored) { 'full' } else { $stored }
$display = if ($stored) { $stored } else { '(unset)' }
Write-Host "Repository: $repository"
Write-Host "Stored PR_EVAL_PROFILE: $display"
Write-Host "Effective PR evaluation profile: $effective"
Write-Host 'This setting affects future PR evaluations only. Release and scheduled evaluations stay full.'
