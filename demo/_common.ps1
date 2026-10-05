# Shared helpers for the demo scripts. Dot-source: . "$PSScriptRoot/_common.ps1"
$ErrorActionPreference = 'Stop'
$Script:RepoRoot = Split-Path $PSScriptRoot -Parent
$Script:DemoBranch = 'demo/concise-answers'
$Script:PromptPath = 'agent/prompts/system.md'

function Invoke-Native {
    # Uses $args (no param block) so native flags like -f / -C are never bound as PowerShell parameters.
    $file, $rest = $args
    & $file @rest
    if ($LASTEXITCODE -ne 0) { throw "$file $($rest -join ' ') failed with exit code $LASTEXITCODE" }
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
    # Pin gh AND git to the demo repo owner for this process, even if the shell already has a
    # GH_TOKEN or a credential manager entry for another GitHub account.
    $user = if ($env:DEMO_GH_USER) { $env:DEMO_GH_USER } else { 'mjhoffmeister' }
    $token = gh auth token -u $user 2>$null
    if ($LASTEXITCODE -eq 0 -and $token) { $env:GH_TOKEN = $token }
    elseif (-not $env:GH_TOKEN) { throw "gh is not signed in as $user - run: gh auth login (or set DEMO_GH_USER)" }
}

function Invoke-Git {
    # git with gh (GH_TOKEN from Initialize-Gh) as the only credential helper. `-c` is applied last, so it
    # overrides credential-manager or IDE-injected helpers that may hold a different GitHub account.
    Invoke-Native git -c credential.helper= -c 'credential.helper=!gh auth git-credential' @args
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
