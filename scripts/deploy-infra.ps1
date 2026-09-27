<#
.SYNOPSIS
  Creates or updates (idempotently) the Foundry infrastructure for the hosted-agents A2A POC.

.EXAMPLE
  ./scripts/deploy-infra.ps1                                   # prompts for the resource group
  ./scripts/deploy-infra.ps1 -ResourceGroupName rg-foundry-hostedagents
  ./scripts/deploy-infra.ps1 -ResourceGroupName rg-foundry-hostedagents -WhatIf
  ./scripts/deploy-infra.ps1 -ResourceGroupName rg-foundry-hostedagents -AgentPrincipalIds <oid1>,<oid2>
#>
[CmdletBinding()]
param(
    [string]$ResourceGroupName,
    [string]$Location = 'swedencentral',
    [string]$BaseName = 'fha',
    [string]$ProjectName = 'project-a2a-poc',
    [string]$SubscriptionId,
    [string]$AzdEnvironmentName,
    [string[]]$AgentPrincipalIds = @(),
    [switch]$RestoreSoftDeletedAccount,
    [switch]$WhatIf
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$template = Join-Path $root 'infra/main.bicep'

function Invoke-Az {
    $out = if ($SubscriptionId) { & az @args --subscription $SubscriptionId } else { & az @args }
    if ($LASTEXITCODE -ne 0) { throw "az $($args -join ' ') failed (exit $LASTEXITCODE)" }
    return $out
}

while ([string]::IsNullOrWhiteSpace($ResourceGroupName)) {
    $ResourceGroupName = (Read-Host 'Resource group name').Trim()
}

$acct = Invoke-Az account show -o json | ConvertFrom-Json
$SubscriptionId = $acct.id
Write-Host "Subscription : $($acct.name) ($($acct.id))"

# Resource group: create if missing, otherwise keep its existing location.
$rgExists = (Invoke-Az group exists --name $ResourceGroupName) -eq 'true'
if ($rgExists) {
    $rgLocation = Invoke-Az group show --name $ResourceGroupName --query location -o tsv
    if ($rgLocation -ne $Location) {
        Write-Warning "Resource group '$ResourceGroupName' already exists in '$rgLocation'; resources will be deployed to '$Location'."
    }
} else {
    if ($WhatIf) {
        throw "Resource group '$ResourceGroupName' does not exist. -WhatIf will not create it; run without -WhatIf to provision it."
    }
    Write-Host "Creating resource group '$ResourceGroupName' in '$Location'..."
    Invoke-Az group create --name $ResourceGroupName --location $Location --tags project=foundry-hosted-agents-a2a-poc -o none | Out-Null
}
Write-Host "ResourceGroup: $ResourceGroupName"

# Developer principal (user or service principal) that gets Foundry User + Foundry Project Manager.
$principalType = 'User'
$principalId = & az ad signed-in-user show --query id -o tsv 2>$null
if ($LASTEXITCODE -ne 0 -or -not $principalId) {
    $principalType = 'ServicePrincipal'
    $principalId = Invoke-Az ad sp show --id $acct.user.name --query id -o tsv
}
Write-Host "Developer    : $principalId ($principalType)"

$paramFile = New-TemporaryFile
@{
    '$schema'      = 'https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#'
    contentVersion = '1.0.0.0'
    parameters     = @{
        location                  = @{ value = $Location }
        baseName                  = @{ value = $BaseName }
        projectName               = @{ value = $ProjectName }
        developerPrincipalId      = @{ value = $principalId }
        developerPrincipalType    = @{ value = $principalType }
        agentPrincipalIds         = @{ value = [string[]]$AgentPrincipalIds }
        restoreSoftDeletedAccount = @{ value = [bool]$RestoreSoftDeletedAccount }
    }
} | ConvertTo-Json -Depth 10 | Set-Content -Path $paramFile -Encoding utf8

$deploymentName = 'foundry-a2a-infra'
try {
    if ($WhatIf) {
        Invoke-Az deployment group what-if --resource-group $ResourceGroupName --name $deploymentName `
            --template-file $template --parameters "@$paramFile"
        return
    }

    Write-Host 'Deploying infra/main.bicep (idempotent, incremental mode)...'
    $result = Invoke-Az deployment group create --resource-group $ResourceGroupName --name $deploymentName `
        --template-file $template --parameters "@$paramFile" --query properties.outputs -o json | ConvertFrom-Json
} finally {
    Remove-Item $paramFile -ErrorAction SilentlyContinue
}

# Persist outputs for the agent deployment steps (non-secret values only).
$envFile = Join-Path $root 'infra/outputs.env'
$lines = $result.PSObject.Properties | ForEach-Object { "$($_.Name.ToUpperInvariant())=$($_.Value.value)" }
$lines | Set-Content -Path $envFile -Encoding utf8
Write-Host "`nOutputs (saved to infra/outputs.env):"
$lines | ForEach-Object { Write-Host "  $_" }

# Keep azd's project binding in step with ARM outputs, not an earlier scaffold.
if (-not $AzdEnvironmentName) { $AzdEnvironmentName = "$ProjectName-dev" }
function Invoke-Azd {
    $out = & azd @args --cwd $root
    if ($LASTEXITCODE -ne 0) { throw "azd $($args -join ' ') failed (exit $LASTEXITCODE)" }
    return $out
}
$environments = Invoke-Azd env list --output json | ConvertFrom-Json
if ($AzdEnvironmentName -notin @($environments.Name)) {
    Invoke-Azd env new $AzdEnvironmentName --subscription $acct.id --location $Location --no-prompt
}
Invoke-Azd env select $AzdEnvironmentName
Invoke-Azd env set --file $envFile
Invoke-Azd env set "AZURE_SUBSCRIPTION_ID=$($acct.id)" "AZURE_TENANT_ID=$($acct.tenantId)" `
    "AZURE_AI_PROJECT_ENDPOINT=$($result.FOUNDRY_PROJECT_ENDPOINT.value)" `
    "USE_EXISTING_AI_PROJECT=true" "ENABLE_HOSTED_AGENTS=true" "AZD_AGENT_SKIP_ACR=true"
Write-Host "azd environment '$AzdEnvironmentName' now targets $($result.FOUNDRY_PROJECT_ENDPOINT.value)"
