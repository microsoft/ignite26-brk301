targetScope = 'resourceGroup'
param operatorObjectId string
param modelAccountName string
param location string = resourceGroup().location

resource search 'Microsoft.Search/searchServices@2025-05-01' = {
  name: 'hbf-iq-${uniqueString(resourceGroup().id, 'finance-policies')}'
  location: location
  sku: {
    name: 'basic'
  }
  identity: {
    type: 'SystemAssigned'
  }
  tags: {
    application: 'hotel-bank-finance-autopilots'
    dataClassification: 'controlled-project'
  }
  properties: {
    replicaCount: 1
    partitionCount: 1
    hostingMode: 'Default'
    disableLocalAuth: true
    semanticSearch: 'standard'
    publicNetworkAccess: 'enabled'
  }
}

resource modelAccount 'Microsoft.CognitiveServices/accounts@2024-10-01' existing = {
  name: modelAccountName
}

var serviceContributor = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7ca78c08-252a-4471-8644-bb5ff32d4ba0')
var indexContributor = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '8ebe5a00-799e-43f5-93ac-243d3dce84a7')
var modelUser = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')

resource serviceRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(search.id, operatorObjectId, serviceContributor)
  scope: search
  properties: {
    roleDefinitionId: serviceContributor
    principalId: operatorObjectId
    principalType: 'User'
  }
}
resource indexRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(search.id, operatorObjectId, indexContributor)
  scope: search
  properties: {
    roleDefinitionId: indexContributor
    principalId: operatorObjectId
    principalType: 'User'
  }
}
resource modelRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(modelAccount.id, search.id, modelUser)
  scope: modelAccount
  properties: {
    roleDefinitionId: modelUser
    principalId: search.identity.principalId
    principalType: 'ServicePrincipal'
  }
}
output name string = search.name
output id string = search.id
output endpoint string = 'https://${search.name}.search.windows.net'
