targetScope = 'resourceGroup'

@description('Object ID of the authenticated operator, only for bootstrap and validation.')
param operatorObjectId string

param location string = resourceGroup().location

resource account 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'hbfin${uniqueString(resourceGroup().id, 'finance-autopilots')}'
  location: location
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  tags: {
    application: 'hotel-bank-finance-autopilots'
    dataClassification: 'controlled-project'
  }
  properties: {
    allowSharedKeyAccess: false
    allowBlobPublicAccess: false
    defaultToOAuthAuthentication: true
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    publicNetworkAccess: 'Enabled'
    encryption: {
      services: {
        blob: {
          enabled: true
          keyType: 'Account'
        }
      }
      keySource: 'Microsoft.Storage'
    }
  }
}

resource service 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: account
  name: 'default'
  properties: {
    isVersioningEnabled: true
    deleteRetentionPolicy: {
      enabled: true
      days: 7
    }
    containerDeleteRetentionPolicy: {
      enabled: true
      days: 7
    }
  }
}

var containerNames = [
  'finance-ledger'
  'hotel-tasks'
  'bank-tasks'
  'validation'
  'runtime-bindings'
]

resource containers 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = [for name in containerNames: {
  parent: service
  name: name
  properties: {
    publicAccess: 'None'
  }
}]

var contributorRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')

resource bootstrapRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (name, i) in containerNames: {
  name: guid(containers[i].id, operatorObjectId, contributorRoleId)
  scope: containers[i]
  properties: {
    roleDefinitionId: contributorRoleId
    principalId: operatorObjectId
    principalType: 'User'
  }
}]

output storageAccountName string = account.name
output storageAccountId string = account.id
output blobEndpoint string = account.properties.primaryEndpoints.blob
output ledgerContainerId string = containers[0].id
output ledgerBlobUrl string = '${account.properties.primaryEndpoints.blob}finance-ledger/state.json'
output hotelTasksContainerId string = containers[1].id
output bankTasksContainerId string = containers[2].id
output validationContainerId string = containers[3].id
output runtimeBindingsContainerId string = containers[4].id
output runtimeBindingsBlobUrl string = '${account.properties.primaryEndpoints.blob}runtime-bindings/instances.json'
