# Shared helpers for the demo scripts. Dot-source: . "$PSScriptRoot/_common.ps1"
$ErrorActionPreference = 'Stop'
$Script:RepoRoot = Split-Path $PSScriptRoot -Parent
$Script:DemoBranch = 'demo/concise-answers'
$Script:PromptPath = 'agent/prompts/system.md'

function Invoke-Native {
    param([Parameter(Mandatory)][string]$File, [Parameter(ValueFromRemainingArguments)][string[]]$Rest)
    & $File @Rest
    if ($LASTEXITCODE -ne 0) { throw "$File $($Rest -join ' ') failed with exit code $LASTEXITCODE" }
}

function Import-AzdEnv {
    # Loads the azd environment values (endpoints, names) into this process.
    Push-Location $Script:RepoRoot
    try {
        foreach ($line in (azd env get-values 2>$null)) {
            if ($line -match '^([A-Za-z_][A-Za-z0-9_]*)="?(.*?)"?$') {
                Set-Item -Path "Env:$($Matches[1])" -Value $Matches[2]
            }
        }
    } finally { Pop-Location }
    if (-not $env:FOUNDRY_PROJECT_ENDPOINT) { throw 'FOUNDRY_PROJECT_ENDPOINT not set - run `azd env select` / `azd provision` first.' }
}

function Initialize-Gh {
    if (-not $env:GH_TOKEN) {
        $user = if ($env:DEMO_GH_USER) { $env:DEMO_GH_USER } else { 'mjhoffmeister' }
        $env:GH_TOKEN = (gh auth token -u $user)
    }
}

function Get-Baseline {
    Get-Content (Join-Path $Script:RepoRoot 'demo/baseline.json') -Raw | ConvertFrom-Json
}

function Invoke-Tools {
    param([Parameter(ValueFromRemainingArguments)][string[]]$Rest)
    Push-Location $Script:RepoRoot
    try {
        if (-not $env:UV_INDEX_URL -and $env:DEMO_UV_INDEX_URL) { $env:UV_INDEX_URL = $env:DEMO_UV_INDEX_URL }
        & uv run python @Rest
        if ($LASTEXITCODE -ne 0) { throw "python $($Rest -join ' ') failed ($LASTEXITCODE)" }
    } finally { Pop-Location }
}

function Assert-CleanTree {
    Push-Location $Script:RepoRoot
    try {
        $dirty = git status --porcelain -- agent evals web .github
        if ($dirty) { throw "Uncommitted changes under agent/ evals/ web/ .github/ - commit or stash first:`n$dirty" }
    } finally { Pop-Location }
}
