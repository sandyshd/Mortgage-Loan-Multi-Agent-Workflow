// --------------------------------------------------------------------------
// Bicep: Provision Azure AI Foundry + supporting resources for the
// Mortgage Loan Origination multi-agent demo.
//
// Uses the new Foundry resource model (no Hub required):
//   Microsoft.CognitiveServices/accounts with allowProjectManagement
//
// Usage:
//   az deployment group create \
//     --resource-group <rg-name> \
//     --template-file infra/main.bicep \
//     --parameters projectName=mortgage-demo location=eastus2
// --------------------------------------------------------------------------

@description('Base name for all resources (lowercase, no spaces).')
param projectName string = 'mortgagedemo'

@description('Azure region for deployment.')
param location string = resourceGroup().location

@description('Model deployment name (e.g. gpt-4o).')
param modelDeploymentName string = 'gpt-4o'

@description('Model name to deploy.')
param modelName string = 'gpt-4o'

@description('Model version.')
param modelVersion string = '2024-11-20'

@description('SKU for the Foundry resource.')
param foundrySku string = 'S0'

@description('App Service SKU (B1 for demo, S1+ for production).')
param appServiceSku string = 'B1'

// ── Variables ────────────────────────────────────────────────────────
var uniqueSuffix = uniqueString(resourceGroup().id, projectName)
var foundryName = 'ai-${take(projectName, 16)}-${take(uniqueSuffix, 6)}'
var foundryProjectName = 'proj-${take(projectName, 14)}-${take(uniqueSuffix, 6)}'
var storageName = 'st${take(replace(projectName, '-', ''), 10)}${take(uniqueSuffix, 6)}'
var keyVaultName = 'kv-${take(projectName, 10)}-${take(uniqueSuffix, 6)}'
var appServicePlanName = 'asp-${take(projectName, 14)}-${take(uniqueSuffix, 6)}'
var webAppName = 'app-${take(projectName, 14)}-${take(uniqueSuffix, 6)}'

// ── Storage Account ─────────────────────────────────────────────────
resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
  }
}

// ── Key Vault ───────────────────────────────────────────────────────
resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  properties: {
    sku: { family: 'A', name: 'standard' }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enabledForDeployment: false
    enabledForDiskEncryption: false
    enabledForTemplateDeployment: false
  }
}

// ── Azure AI Foundry Resource (no Hub needed) ───────────────────────
resource foundry 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: foundryName
  location: location
  kind: 'AIServices'
  sku: {
    name: foundrySku
  }
  identity: { type: 'SystemAssigned' }
  properties: {
    customSubDomainName: foundryName
    allowProjectManagement: true
    publicNetworkAccess: 'Enabled'
  }
}

// ── Foundry Project ─────────────────────────────────────────────────
resource foundryProject 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  name: foundryProjectName
  parent: foundry
  location: location
  identity: { type: 'SystemAssigned' }
  properties: {
    displayName: 'Mortgage Loan Origination Demo'
    description: 'Multi-agent workflow for mortgage loan origination'
  }
}

// ── Model Deployment (gpt-4o on the Foundry resource) ───────────────
resource modelDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: foundry
  name: modelDeploymentName
  sku: {
    name: 'GlobalStandard'
    capacity: 10
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: modelName
      version: modelVersion
    }
  }
}

// ── App Service Plan ────────────────────────────────────────────────
resource appServicePlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: appServicePlanName
  location: location
  kind: 'linux'
  sku: { name: appServiceSku }
  properties: {
    reserved: true
  }
}

// ── Web App (FastAPI + Frontend) ────────────────────────────────────
resource webApp 'Microsoft.Web/sites@2023-12-01' = {
  name: webAppName
  location: location
  identity: { type: 'SystemAssigned' }
  properties: {
    serverFarmId: appServicePlan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.11'
      appCommandLine: 'startup.sh'
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      appSettings: [
        { name: 'PROJECT_ENDPOINT', value: foundryProject.properties.endpoints['AI Foundry API'] }
        { name: 'MODEL_DEPLOYMENT', value: modelDeploymentName }
        { name: 'SCM_DO_BUILD_DURING_DEPLOYMENT', value: 'true' }
        { name: 'WEBSITES_PORT', value: '8000' }
      ]
    }
  }
}

// ── RBAC: Web App → Cognitive Services OpenAI User on Foundry ───────
var cognitiveServicesOpenAIUserRoleId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'

resource webAppRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundry.id, webApp.id, cognitiveServicesOpenAIUserRoleId)
  scope: foundry
  properties: {
    principalId: webApp.identity.principalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesOpenAIUserRoleId)
    principalType: 'ServicePrincipal'
  }
}

// ── RBAC: Web App → Azure AI Developer on Foundry (Agents/Responses API) ──
var azureAIDeveloperRoleId = '64702f94-c441-49e6-a78b-ef80e0188fee'

resource webAppAIDeveloperRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundry.id, webApp.id, azureAIDeveloperRoleId)
  scope: foundry
  properties: {
    principalId: webApp.identity.principalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', azureAIDeveloperRoleId)
    principalType: 'ServicePrincipal'
  }
}

// ── RBAC: Web App → Azure AI User on Project (invoke agents) ────────
var azureAIUserRoleId = '53ca6127-db72-4b80-b1b0-d745d6d5456d'

resource webAppProjectAIUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundryProject.id, webApp.id, azureAIUserRoleId)
  scope: foundryProject
  properties: {
    principalId: webApp.identity.principalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', azureAIUserRoleId)
    principalType: 'ServicePrincipal'
  }
}

// ── RBAC: Web App → Azure AI Developer on Project ───────────────────
resource webAppProjectAIDeveloper 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundryProject.id, webApp.id, azureAIDeveloperRoleId)
  scope: foundryProject
  properties: {
    principalId: webApp.identity.principalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', azureAIDeveloperRoleId)
    principalType: 'ServicePrincipal'
  }
}

// ── Outputs ─────────────────────────────────────────────────────────
output foundryEndpoint string = foundry.properties.endpoint
output projectEndpoint string = foundryProject.properties.endpoints['AI Foundry API']
output foundryName string = foundry.name
output projectName string = foundryProject.name
output modelDeployment string = modelDeploymentName
output webAppName string = webApp.name
output webAppUrl string = 'https://${webApp.properties.defaultHostName}'
output webAppPrincipalId string = webApp.identity.principalId
