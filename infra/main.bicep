// Foundry infrastructure for the hosted-agents A2A POC.
// Idempotent: every resource name is deterministic (derived from the resource group),
// so re-running the deployment updates resources in place instead of duplicating them.
targetScope = 'resourceGroup'

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Short prefix used in resource names.')
@maxLength(8)
param baseName string = 'fha'

@description('Foundry project name (child of the Foundry account).')
param projectName string = 'project-a2a-poc'

@description('Model deployment used by the hosted agents.')
param modelDeployment object = {
  name: 'gpt-5.4-mini'
  modelName: 'gpt-5.4-mini'
  modelVersion: '2026-03-17'
  skuName: 'GlobalStandard'
  capacity: 10
}

@description('Object ID of the developer/CI principal that manages agents, connections, and toolboxes.')
param developerPrincipalId string = ''

@allowed([
  'User'
  'ServicePrincipal'
  'Group'
])
param developerPrincipalType string = 'User'

@description('Object IDs of hosted-agent identities (instance_identity) that must call models and other agents over A2A. Fill in after the agents are deployed and re-run.')
param agentPrincipalIds array = []

@description('Set to true only when a soft-deleted Foundry account with the same name must be restored.')
param restoreSoftDeletedAccount bool = false

param tags object = {
  project: 'foundry-hosted-agents-a2a-poc'
  managedBy: 'bicep'
}

var resourceToken = toLower(uniqueString(subscription().id, resourceGroup().id, location))
var accountName = 'foundry-${baseName}-${resourceToken}'

var roles = {
  foundryUser: '53ca6127-db72-4b80-b1b0-d745d6d5456d'
  foundryProjectManager: 'eadc314b-1a2d-4efa-be10-5d325db5065e'
  foundryAgentConsumer: 'eed3b665-ab3a-47b6-8f48-c9382fb1dad6'
  monitoringMetricsPublisher: '3913510d-42f4-4e42-8a64-420c390055eb'
}

// ---------- Monitoring ----------
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'laws-${baseName}-${resourceToken}'
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appInsights-${baseName}-${resourceToken}'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
  }
}

// ---------- Foundry account + project ----------
resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: accountName
  location: location
  tags: tags
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    allowProjectManagement: true
    customSubDomainName: accountName
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Allow'
      ipRules: []
      virtualNetworkRules: []
    }
    disableLocalAuth: true
    restore: restoreSoftDeletedAccount
  }
}

resource model 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: account
  name: modelDeployment.name
  sku: {
    name: modelDeployment.skuName
    capacity: modelDeployment.capacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: modelDeployment.modelName
      version: modelDeployment.modelVersion
    }
    versionUpgradeOption: 'OnceNewDefaultVersionAvailable'
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: account
  name: projectName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: projectName
    description: 'POC: Foundry hosted agents talking over A2A'
  }
  dependsOn: [
    model
  ]
}

resource appInsightsConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-06-01' = {
  parent: project
  name: 'appinsights'
  properties: {
    category: 'AppInsights'
    target: appInsights.id
    authType: 'ApiKey'
    isSharedToAll: true
    credentials: {
      key: appInsights.properties.ConnectionString
    }
    metadata: {
      ApiType: 'Azure'
      ResourceId: appInsights.id
    }
  }
}

// ---------- RBAC (deterministic names => idempotent) ----------
resource projectMiMetricsPublisher 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: appInsights
  name: guid(appInsights.id, project.id, roles.monitoringMetricsPublisher)
  properties: {
    principalId: project.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.monitoringMetricsPublisher)
  }
}

var developerRoles = empty(developerPrincipalId) ? [] : [
  roles.foundryUser
  roles.foundryProjectManager
]

resource developerRoleAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for roleId in developerRoles: {
    scope: project
    name: guid(project.id, developerPrincipalId, roleId)
    properties: {
      principalId: developerPrincipalId
      principalType: developerPrincipalType
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleId)
    }
  }
]

var agentRoleAssignmentsInput = flatten(map(agentPrincipalIds, pid => [
  { principalId: pid, roleId: roles.foundryUser }
  { principalId: pid, roleId: roles.foundryAgentConsumer }
]))

resource agentRoleAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for ra in agentRoleAssignmentsInput: {
    scope: project
    name: guid(project.id, ra.principalId, ra.roleId)
    properties: {
      principalId: ra.principalId
      principalType: 'ServicePrincipal'
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', ra.roleId)
    }
  }
]

// ---------- Outputs ----------
output AZURE_RESOURCE_GROUP string = resourceGroup().name
output AZURE_LOCATION string = location
output AZURE_AI_ACCOUNT_NAME string = account.name
output AZURE_AI_PROJECT_NAME string = project.name
output AZURE_AI_PROJECT_ID string = project.id
output FOUNDRY_PROJECT_ENDPOINT string = project.properties.endpoints['AI Foundry API']
output AZURE_OPENAI_ENDPOINT string = account.properties.endpoints['OpenAI Language Model Instance API']
output AZURE_AI_MODEL_DEPLOYMENT_NAME string = model.name
output APPLICATIONINSIGHTS_RESOURCE_ID string = appInsights.id
output PROJECT_PRINCIPAL_ID string = project.identity.principalId
