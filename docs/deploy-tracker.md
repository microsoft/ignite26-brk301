# Deployment-button click tracking

The README button uses an Azure Functions redirect owned by the demo
maintainers. Its only destination is the repository's Codespaces creation
page. Query parameters cannot change the destination.

The private Azure Table stores one random event ID and UTC timestamp per
qualifying launch request. No visitor identity, tenant, credentials, IP
address, or user agent is stored in the event table. `HEAD`, obvious
preview/prefetch/crawler requests, and image fetches are excluded.
This is a count of **launch clicks**, not unique visitors or successful
deployments; undetected bots and retries can affect it.
If recording fails, the user is still redirected and a generic error is logged.
Statistics failures return `503`, not an invented zero.

## Validate locally

Use Node.js 22 or 24:

```powershell
npm --prefix deployment-tracker ci --ignore-scripts
npm --prefix deployment-tracker test
npm --prefix deployment-tracker run check
az bicep build --file deployment-tracker/infra/main.bicep --stdout
```

Tests use in-memory doubles and do not increment the deployed counter.

## Provision the maintainer's tracker

This infrastructure is separate from customer demo deployments. Deploy it
once in a maintainer-owned subscription; customers do not deploy a tracker
when they use the demo button.

Select and verify your subscription explicitly. Use a dedicated resource group
and a region supporting Linux Consumption functions with Node.js 22.
The tracker uses consumption-based compute and an Azure Storage account.
No agents, Microsoft 365 resources, or conference ledger state are changed.

From the repository root in PowerShell:

```powershell
$group = "rg-ignite26-brk301-launcher"
$location = "westus2"
az account show --query "{subscription:name, id:id}" --output json
az group create --name $group --location $location --tags scenario=ignite26-brk301 component=deployment-click-tracker
$deployment = az deployment group create --resource-group $group --name deploy-tracker --template-file deployment-tracker/infra/main.bicep --output json | ConvertFrom-Json
$app = $deployment.properties.outputs.functionAppName.value
$launchUrl = $deployment.properties.outputs.launchUrl.value
$statsUrl = $deployment.properties.outputs.statsUrl.value

New-Item -ItemType Directory -Path .state -Force | Out-Null
Compress-Archive -Path deployment-tracker/host.json,deployment-tracker/package.json,deployment-tracker/package-lock.json,deployment-tracker/src -DestinationPath .state/deployment-tracker.zip -Force
az functionapp deployment source config-zip --resource-group $group --name $app --src .state/deployment-tracker.zip --build-remote true
```

Use the deployment's `launchUrl` as the README button target. Keep the static
button image local: loading a README image must not increment the count.
Check `/api/health` and use `HEAD` or a prefetch-marked launch request for
non-counting readiness checks. A normal `GET /api/deploy` increments the count.

## Read click counts

`/api/stats` is protected by a function key; it is not a public visitor endpoint.
An authorized maintainer can retrieve the key without embedding it in a URL:

```powershell
$group = "rg-ignite26-brk301-launcher"
$outputs = az deployment group show --resource-group $group --name deploy-tracker --query properties.outputs --output json | ConvertFrom-Json
$app = $outputs.functionAppName.value
$statsUrl = $outputs.statsUrl.value
$key = az functionapp function keys list --resource-group $group --name $app --function-name stats --query default --output tsv
Invoke-RestMethod -Uri $statsUrl -Headers @{ "x-functions-key" = $key }
Remove-Variable key
```

The response contains `totalClicks` and UTC `byDay` totals. Aggregation
iterates all Azure Table result pages. Retain the Storage account across code
redeployments so the count survives restarts and deploys. Deleting its event
table or resource group deletes the history.

There are no cookies, third-party analytics services, or analytics credentials
in the README. Azure platform logging and normal GitHub/Codespaces behavior
remain separate from this application's event table.

## Hosting lifecycle

The current maintainer tracker uses Node.js 22, the last Node.js version
supported on Linux Consumption. Plan a move to Flex Consumption before
Linux Consumption retires on September 30, 2028, and keep the runtime within
Azure's supported version window. This hosting choice is only for the small
redirect service, not either demo's agent runtime.
