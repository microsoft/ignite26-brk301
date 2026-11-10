"use strict";

const { app } = require("@azure/functions");
const { createHandlers } = require("./handlers");
const { store } = require("./store");

const handlers = createHandlers({ store });

app.http("deploy", { methods: ["GET", "HEAD"], authLevel: "anonymous", handler: handlers.deploy });
app.http("stats", { methods: ["GET"], authLevel: "function", handler: handlers.stats });
app.http("health", {
  methods: ["GET"], authLevel: "anonymous",
  handler: () => ({ status: 200, jsonBody: { status: "ok" } }),
});
