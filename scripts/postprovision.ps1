#!/usr/bin/env pwsh
# azd postprovision hook: Foundry IQ knowledge base + toolbox connection + toolbox.
# Idempotent: safe on every `azd provision`.
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)

function Invoke-Checked([scriptblock] $Script, [string] $What) {
    & $Script
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit $LASTEXITCODE)." }
}

if (-not $env:AZURE_SEARCH_ENDPOINT) { throw 'AZURE_SEARCH_ENDPOINT is not set (expected from Terraform outputs).' }

Write-Host '==> Building corpus + provisioning Foundry IQ knowledge base'
if (-not (Test-Path data/corpus.jsonl)) {
    Invoke-Checked { uv run --no-dev python data/ingest.py } 'data/ingest.py'
}
Invoke-Checked { uv run --no-dev python kb/provision_kb.py } 'kb/provision_kb.py'

$kb = azd env get-value KB_MCP_ENDPOINT
if (-not $kb) { throw 'provision_kb.py did not set KB_MCP_ENDPOINT.' }

Write-Host '==> knowledge-base-mcp connection (agentic identity)'
Invoke-Checked {
    azd ai connection create knowledge-base-mcp --kind remote-tool --force `
        --target $kb --auth-type agentic-identity `
        --audience https://search.azure.com --metadata 'ApiType=Azure'
} 'connection create'

Write-Host '==> knowledge-base toolbox'
$prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
azd ai toolbox show knowledge-base *> $null
$exists = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = $prev
if ($exists) {
    Write-Host 'Toolbox knowledge-base already exists; skipping create.'
}
else {
    $resolved = (Get-Content ./kb/toolbox.yaml -Raw).Replace('${KB_MCP_ENDPOINT}', $kb)
    $tmp = Join-Path ([IO.Path]::GetTempPath()) "toolbox-$([guid]::NewGuid()).yaml"
    Set-Content -Path $tmp -Value $resolved -NoNewline
    try { Invoke-Checked { azd ai toolbox create knowledge-base --from-file $tmp } 'toolbox create' }
    finally { Remove-Item $tmp -ErrorAction SilentlyContinue }
}

$proj = (azd env get-value FOUNDRY_PROJECT_ENDPOINT).TrimEnd('/')
Invoke-Checked { azd env set TOOLBOX_ENDPOINT "$proj/toolboxes/knowledge-base/mcp?api-version=v1" } 'env set TOOLBOX_ENDPOINT'
Write-Host 'postprovision complete.'
