#!/usr/bin/env pwsh
# azd postdeploy hook: the hosted agent's identity exists only after its first
# deploy. Grant it Search Index Data Reader so the KB MCP call (agentic
# identity) can read the index. Idempotent.
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)

$agent = if ($env:AGENT_NAME) { $env:AGENT_NAME } else { 'clinical-agent' }
$searchId = $env:AZURE_SEARCH_ID
# Pin the subscription: the az CLI default may point elsewhere (other tenants/sessions).
$sub = ($searchId -split '/')[2]
if (-not $searchId) { throw 'AZURE_SEARCH_ID is not set.' }

$json = uv run --no-dev python scripts/foundry_agents.py --agent $agent identity
if ($LASTEXITCODE -ne 0) { Write-Warning "Agent '$agent' not found yet; skipping identity grant."; exit 0 }
$principal = ($json | ConvertFrom-Json).principal_id
Write-Host "Agent identity: $principal"

$role = 'Search Index Data Reader'
$existing = az role assignment list --subscription $sub --scope $searchId --assignee $principal --role $role --query '[0].id' -o tsv 2>$null
if ($existing) {
    Write-Host "$role already assigned."
}
else {
    az role assignment create --subscription $sub --assignee-object-id $principal --assignee-principal-type ServicePrincipal `
        --role $role --scope $searchId --output none
    if ($LASTEXITCODE -ne 0) { throw 'role assignment failed' }
    Write-Host "Granted $role on search to agent identity."
}
