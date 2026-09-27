<#
.SYNOPSIS
  Copies the shared POC modules from src/_shared into every agent folder.

.DESCRIPTION
  Each hosted agent is deployed from its own directory, so that directory has to be a
  self-contained build context. Rather than let three copies of the evidence layer drift,
  src/_shared holds the source of truth and this script fans it out. Run it after editing
  anything under src/_shared, and before `azd deploy`.

.EXAMPLE
  ./scripts/sync-shared.ps1
  ./scripts/sync-shared.ps1 -Check   # fail if a copy is stale (use in CI)
#>
[CmdletBinding()]
param(
    [switch]$Check
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$sharedDir = Join-Path $root 'src/_shared'
$agents = @('supervisor-agent', 'research-agent', 'analysis-agent')

$stale = @()
foreach ($file in Get-ChildItem -Path $sharedDir -Filter '*.py' -File) {
    foreach ($agent in $agents) {
        $target = Join-Path $root "src/$agent/$($file.Name)"
        $sourceHash = (Get-FileHash $file.FullName -Algorithm SHA256).Hash
        $targetHash = if (Test-Path $target) { (Get-FileHash $target -Algorithm SHA256).Hash } else { '' }

        if ($sourceHash -eq $targetHash) {
            Write-Host "  ok      src/$agent/$($file.Name)"
            continue
        }

        if ($Check) {
            $stale += "src/$agent/$($file.Name)"
            Write-Host "  STALE   src/$agent/$($file.Name)"
        } else {
            Copy-Item $file.FullName $target -Force
            Write-Host "  synced  src/$agent/$($file.Name)"
        }
    }
}

if ($Check -and $stale.Count -gt 0) {
    throw "Shared modules are out of date: $($stale -join ', '). Run ./scripts/sync-shared.ps1."
}
