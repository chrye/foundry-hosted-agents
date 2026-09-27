<#
.SYNOPSIS
  Regenerates uv.lock for every hosted agent and normalises it to public PyPI.

.DESCRIPTION
  Foundry's remote builder resolves dependencies from the registry recorded in uv.lock.
  A developer machine that routes PyPI through a corporate mirror bakes that unreachable
  internal URL into the lock, and `azd deploy` then fails with a [CodeError] during the
  code build. This script regenerates each lock and normalises both the registry and
  artifact URLs. Public artifact URLs are looked up in PyPI metadata by their exact
  SHA256 hash; a missing match fails rather than trusting a different distribution.

  Run after changing any agent's pyproject.toml, then `azd deploy`.

.EXAMPLE
  ./scripts/lock-agents.ps1
  ./scripts/lock-agents.ps1 -Check   # verify locks are normalised, don't regenerate
#>
[CmdletBinding()]
param(
    [switch]$Check
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$agents = @('supervisor-agent', 'research-agent', 'analysis-agent')
$publicIndex = 'https://pypi.org/simple'
$publicArtifacts = @{}

$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
    $venvUv = Join-Path $root '.venv/Scripts/uv.exe'
    if (Test-Path $venvUv) { $uv = $venvUv } else { throw "uv not found. Install it with 'pip install uv'." }
} else {
    $uv = $uv.Source
}

$problems = @()
foreach ($agent in $agents) {
    $dir = Join-Path $root "src/$agent"
    $lock = Join-Path $dir 'uv.lock'

    if (-not $Check) {
        Write-Host "Locking $agent..."
        Push-Location $dir
        try {
            & $uv lock --python 3.13 | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "uv lock failed for $agent (exit $LASTEXITCODE)." }
        } finally { Pop-Location }
    }

    if (-not (Test-Path $lock)) { throw "Missing $lock" }

    $content = Get-Content $lock -Raw
    $foreign = [regex]::Matches($content, 'registry = "(?<url>[^"]+)"') |
        ForEach-Object { $_.Groups['url'].Value } |
        Where-Object { $_ -ne $publicIndex } |
        Select-Object -Unique

    if ($foreign) {
        if ($Check) {
            $problems += "src/$agent/uv.lock points at $($foreign -join ', ')"
            Write-Host "  NOT NORMALISED  src/$agent/uv.lock -> $($foreign -join ', ')"
            continue
        }
        foreach ($url in $foreign) {
            $content = $content.Replace("registry = `"$url`"", "registry = `"$publicIndex`"")
        }
        Write-Host "  rewrote registry $($foreign -join ', ') -> $publicIndex"
    } else {
        Write-Host "  ok  src/$agent/uv.lock already targets $publicIndex"
    }

    $packages = [regex]::Matches($content, '(?ms)^\[\[package\]\]\r?\n.*?(?=^\[\[package\]\]|\z)')
    foreach ($package in $packages) {
        $block = $package.Value
        $artifacts = [regex]::Matches($block, 'url = "(?<url>[^"]+)", hash = "sha256:(?<hash>[a-f0-9]+)"')
        $privateArtifacts = @($artifacts | Where-Object {
            ([uri]$_.Groups['url'].Value).Host -ne 'files.pythonhosted.org'
        })
        if ($privateArtifacts.Count -eq 0) { continue }
        $name = [regex]::Match($block, '(?m)^name = "([^"]+)"').Groups[1].Value
        $version = [regex]::Match($block, '(?m)^version = "([^"]+)"').Groups[1].Value
        if ($Check) {
            $problems += "src/$agent/uv.lock contains non-public artifact URLs for $name==$version"
            continue
        }

        $key = "$name/$version"
        if (-not $publicArtifacts.ContainsKey($key)) {
            $metadata = Invoke-RestMethod "https://pypi.org/pypi/$key/json"
            $byHash = @{}
            foreach ($artifact in $metadata.urls) {
                if (([uri]$artifact.url).Host -ne 'files.pythonhosted.org') {
                    throw "Unexpected public artifact host for $key."
                }
                $byHash[$artifact.digests.sha256] = $artifact.url
            }
            $publicArtifacts[$key] = $byHash
        }
        foreach ($artifact in $privateArtifacts) {
            $hash = $artifact.Groups['hash'].Value
            $publicUrl = $publicArtifacts[$key][$hash]
            if (-not $publicUrl) { throw "No public PyPI artifact matches SHA256 $hash for $key." }
            $content = $content.Replace($artifact.Groups['url'].Value, $publicUrl)
        }
    }
    if (-not $Check) {
        Set-Content -Path $lock -Value $content -Encoding utf8 -NoNewline
        Write-Host "  normalised artifact URLs with matching public PyPI hashes"
    }
    if ($problems.Count -eq 0) {
        & $uv lock --project $dir --check --offline --python 3.13 --default-index $publicIndex | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Lock is stale for $agent. Run ./scripts/lock-agents.ps1." }
    }
}

if ($Check -and $problems.Count -gt 0) {
    throw "Lock files are not normalised:`n  $($problems -join "`n  ")"
}
