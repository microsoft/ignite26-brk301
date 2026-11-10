targetScope = 'resourceGroup'

@description('Deploy both scenarios, customer service only, or payroll/treasurer only.')
@allowed([
  'both'
  'customer-service'
  'payroll'
])
param scenario string = 'both'

@description('Region supporting the pinned models and Foundry Hosted Agents. Use the resource group region for payroll.')
param location string = resourceGroup().location

@description('Azure AI Search region; may differ when Search capacity is unavailable in the main region.')
param searchLocation string = location

@description('Human operator object ID. Defaults to the signed-in identity starting the deployment.')
@minLength(36)
@maxLength(36)
param operatorObjectId string = deployer().objectId

@description('GPT-5.4-mini GlobalStandard capacity. Verify model availability and subscription quota in the chosen region.')
@minValue(1)
param modelCapacity int = 10

@description('Text-embedding-3-small Standard capacity for customer service; unused for payroll only.')
@minValue(1)
param embeddingCapacity int = 10

var includeCustomerService = scenario != 'payroll'
var includePayroll = scenario != 'customer-service'
var resourceToken = uniqueString(resourceGroup().id)
var accountName = 'brk301-${resourceToken}'
var projectName = 'hotel-agents'
var tags = {
  application: 'ignite26-brk301'
}

module foundry '../demos/customer-service-agent/infra/foundry-project.bicep' = {
  name: 'brk301-foundry'
  params: {
    location: location
    accountName: accountName
    projectName: projectName
    modelCapacity: modelCapacity
    projectDescription: 'Shared Microsoft Foundry project for Hotel Guest Services, Hotel Payroll, and Hotel Treasurer.'
  }
}

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: accountName
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' existing = {
  parent: account
  name: projectName
}

resource embedding 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = if (includeCustomerService) {
  parent: account
  name: 'text-embedding-3-small'
  dependsOn: [
    foundry
  ]
  sku: {
    name: 'Standard'
    capacity: embeddingCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'text-embedding-3-small'
      version: '1'
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}

var foundryUserRole = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '53ca6127-db72-4b80-b1b0-d745d6d5456d'
)
var modelUserRole = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
)

resource operatorProjectAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(project.id, operatorObjectId, foundryUserRole)
  scope: project
  dependsOn: [
    foundry
  ]
  properties: {
    principalId: operatorObjectId
    principalType: 'User'
    roleDefinitionId: foundryUserRole
  }
}

resource operatorModelAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, operatorObjectId, modelUserRole)
  scope: account
  dependsOn: [
    foundry
  ]
  properties: {
    principalId: operatorObjectId
    principalType: 'User'
    roleDefinitionId: modelUserRole
  }
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: 'brk301${resourceToken}'
  location: location
  tags: tags
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
  }
}

var acrPushRole = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '8311e382-0749-4cb8-b61a-304f252e45ec'
)
var acrPullRole = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '7f951dda-4ed3-4680-a7ca-43fe172d538d'
)
var pullingIdentities = [
  foundry.outputs.accountPrincipalId
  foundry.outputs.projectPrincipalId
]

resource operatorRegistryPush 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, operatorObjectId, acrPushRole)
  scope: registry
  properties: {
    principalId: operatorObjectId
    principalType: 'User'
    roleDefinitionId: acrPushRole
  }
}

resource registryPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for i in range(0, 2): {
    name: guid(registry.id, account.id, project.id, string(i), acrPullRole)
    scope: registry
    properties: {
      principalId: pullingIdentities[i]
      principalType: 'ServicePrincipal'
      roleDefinitionId: acrPullRole
    }
  }
]

module customerService '../demos/customer-service-agent/infra/foundry-iq.bicep' = if (includeCustomerService) {
  name: 'brk301-customer-service'
  params: {
    location: location
    searchLocation: searchLocation
    foundryAccountName: accountName
    foundryProjectName: projectName
    foundryProjectPrincipalId: foundry.outputs.projectPrincipalId
    deploymentPrincipalId: operatorObjectId
    searchServiceName: 'brk301-guest-${resourceToken}'
    storageAccountName: 'stbrkguest${resourceToken}'
    tags: tags
  }
}

module payrollStorage '../demos/hotel-payroll-treasurer-agents/infra/storage.bicep' = if (includePayroll) {
  name: 'brk301-payroll-storage'
  params: {
    location: location
    operatorObjectId: operatorObjectId
  }
}

module payrollSearch '../demos/hotel-payroll-treasurer-agents/infra/search.bicep' = if (includePayroll) {
  name: 'brk301-payroll-search'
  dependsOn: [
    foundry
  ]
  params: {
    location: searchLocation
    operatorObjectId: operatorObjectId
    modelAccountName: accountName
  }
}

output NEXT_STEPS_URL string = 'https://github.com/microsoft/ignite26-brk301/blob/main/docs/deploy.md#2-continue-after-azure-deployment'
output AZURE_TENANT_ID string = tenant().tenantId
output AZURE_SUBSCRIPTION_ID string = subscription().subscriptionId
output AZURE_RESOURCE_GROUP string = resourceGroup().name
output AZURE_LOCATION string = location
output AZURE_AI_ACCOUNT_NAME string = accountName
output AZURE_AI_PROJECT_NAME string = projectName
output AZURE_AI_PROJECT_ID string = foundry.outputs.projectId
output AZURE_AI_PROJECT_ENDPOINT string = foundry.outputs.projectEndpoint
output AZURE_AI_MODEL_DEPLOYMENT_NAME string = foundry.outputs.modelDeploymentName
output AZURE_OPENAI_ENDPOINT string = foundry.outputs.modelEndpoint
output AZURE_CONTAINER_REGISTRY_NAME string = registry.name
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = registry.properties.loginServer
output customerService object = includeCustomerService
  ? {
      AZURE_SEARCH_ENDPOINT: customerService!.outputs.searchServiceEndpoint
      AZURE_STORAGE_ACCOUNT_URL: customerService!.outputs.storageAccountUrl
      FOUNDRY_IQ_BLOB_CONTAINER: customerService!.outputs.documentContainerName
      FOUNDRY_IQ_KNOWLEDGE_BASE_NAME: 'hotel-foundryiq'
    }
  : {}
output payroll object = includePayroll
  ? {
      config: {
        tenantId: tenant().tenantId
        subscriptionId: subscription().subscriptionId
        resourceGroup: resourceGroup().name
        location: location
        searchLocation: searchLocation
        foundry: {
          account: accountName
          project: projectName
          modelDeployment: foundry.outputs.modelDeploymentName
        }
      }
      storageAccountName: payrollStorage!.outputs.storageAccountName
      runtimeBindingsBlobUrl: payrollStorage!.outputs.runtimeBindingsBlobUrl
      ledgerBlobUrl: payrollStorage!.outputs.ledgerBlobUrl
      searchEndpoint: payrollSearch!.outputs.endpoint
    }
  : {}
