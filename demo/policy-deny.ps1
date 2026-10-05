<#
.SYNOPSIS
  Governance moment: try to deploy a model that is not on the allow-list.
.DESCRIPTION
  Sends the same ARM request a portal or IaC deployment would. The deployment
  carries the approved guardrail, so the only policy it violates is the model
  allow-list: Azure Policy denies it synchronously with the custom message
  "Model is not on the approved list ...". Nothing is created.
#>
param([string]$Model = 'gpt-4.1-nano', [string]$Version = '2025-04-14')
. "$PSScriptRoot/_common.ps1"
Import-AzdEnv
$id = "/subscriptions/$env:AZURE_SUBSCRIPTION_ID/resourceGroups/$env:AZURE_RESOURCE_GROUP/providers/Microsoft.CognitiveServices/accounts/$env:AZURE_AI_ACCOUNT_NAME/deployments/not-approved"
$body = @{
    sku        = @{ name = 'GlobalStandard'; capacity = 1 }
    properties = @{ model = @{ format = 'OpenAI'; name = $Model; version = $Version }; raiPolicyName = 'clinical-guardrail' }
} | ConvertTo-Json -Depth 5 -Compress
$file = New-TemporaryFile
try {
    Set-Content $file $body
    Write-Host "PUT deployment 'not-approved' ($Model $Version) on $env:AZURE_AI_ACCOUNT_NAME ..." -ForegroundColor Cyan
    $out = az rest --method put --url "https://management.azure.com$($id)?api-version=2025-06-01" --body "@$file" 2>&1 | Out-String
    if ($LASTEXITCODE -eq 0) {
        Write-Warning 'Deployment was NOT denied - check the foundry-approved-models policy assignment. Deleting it.'
        az rest --method delete --url "https://management.azure.com$($id)?api-version=2025-06-01" | Out-Null
        exit 1
    }
    if ($out -match 'policyAssignments/([\w-]+):\\n([^"]+)') {
        Write-Host "DENIED by Azure Policy assignment '$($Matches[1])':" -ForegroundColor Red
        Write-Host "  $($Matches[2])"
    } else {
        Write-Host $out
    }
} finally { Remove-Item $file -ErrorAction SilentlyContinue }
