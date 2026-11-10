"use strict";

const { TableClient } = require("@azure/data-tables");

let ready;

async function store() {
  if (!ready) {
    ready = (async () => {
      const client = TableClient.fromConnectionString(
        process.env.AzureWebJobsStorage, "DeploymentLaunchClicks",
        { retryOptions: { maxRetries: 0 } },
      );
      try {
        await client.createTable({ abortSignal: AbortSignal.timeout(2500) });
      } catch (error) {
        if (error.statusCode !== 409) throw error;
      }
      return client;
    })().catch((error) => {
      ready = undefined;
      throw error;
    });
  }
  return ready;
}

module.exports = { store };
