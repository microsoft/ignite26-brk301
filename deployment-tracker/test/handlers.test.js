"use strict";

const assert = require("node:assert/strict");
const { test } = require("node:test");
const { createHandlers } = require("../src/handlers");

function fixture(override = {}) {
  const entities = [];
  const errors = [];
  const client = {
    async createEntity(entity) { entities.push(entity); },
    async *listEntities() { yield* entities; },
    ...override,
  };
  const handlers = createHandlers({
    store: async () => client,
    now: () => new Date("2026-11-10T12:00:00Z"),
    uuid: () => "synthetic-test-event",
  });
  const context = { error: (message) => errors.push(message) };
  const request = (method = "GET", headers = {}) => ({ method, headers: new Headers(headers) });
  return { entities, errors, client, handlers, context, request };
}

test("a click persists only an event ID and time before redirecting to a fixed target", async () => {
  const f = fixture();
  const response = await f.handlers.deploy(f.request("GET", {
    "user-agent": "Mozilla/5.0", "sec-fetch-dest": "document",
    "x-forwarded-for": "192.0.2.1", authorization: "not-an-event-field",
  }), f.context);
  assert.equal(response.status, 302);
  assert.equal(response.headers.Location, "https://codespaces.new/microsoft/ignite26-brk301?quickstart=1&ref=main");
  assert.match(response.headers["Cache-Control"], /no-store/);
  assert.deepEqual(f.entities, [{
    partitionKey: "readme-deploy", rowKey: "synthetic-test-event", clickedAt: "2026-11-10T12:00:00.000Z",
  }]);
});

for (const [label, method, headers] of [
  ["HEAD probe", "HEAD", {}],
  ["prefetch", "GET", { purpose: "prefetch" }],
  ["secure prefetch", "GET", { "sec-purpose": "prefetch;prerender" }],
  ["link preview", "GET", { "user-agent": "Slackbot-LinkExpanding 1.0" }],
  ["crawler", "GET", { "user-agent": "ExampleCrawler" }],
  ["image load", "GET", { "sec-fetch-dest": "image" }],
]) {
  test(`${label} redirects without increasing the count`, async () => {
    const f = fixture();
    const response = await f.handlers.deploy(f.request(method, headers), f.context);
    assert.equal(response.status, 302);
    assert.equal(f.entities.length, 0);
  });
}

test("storage failure never breaks the launch or leaks the exception", async () => {
  const f = fixture({ createEntity: async () => { throw new Error("sensitive backend details"); } });
  assert.equal((await f.handlers.deploy(f.request(), f.context)).status, 302);
  assert.deepEqual(f.errors, ["Deployment launch click could not be recorded."]);
});

test("statistics aggregate every page, not just the first storage result", async () => {
  const f = fixture({
    async *listEntities(options) {
      assert.equal(options.queryOptions.filter, "PartitionKey eq 'readme-deploy'");
      yield { clickedAt: "2026-11-10T12:00:00Z" };
      await Promise.resolve();
      yield { clickedAt: "2026-11-09T12:00:00Z" };
      yield { clickedAt: "2026-11-10T13:00:00Z" };
    },
  });
  const response = await f.handlers.stats(f.request(), f.context);
  assert.equal(response.status, 200);
  assert.deepEqual(response.jsonBody, {
    metric: "deployment_launch_clicks", totalClicks: 3,
    byDay: { "2026-11-09": 1, "2026-11-10": 2 },
  });
  assert.match(response.headers["Cache-Control"], /no-store/);
});

test("an empty event table reports zero, not a deployment count", async () => {
  const f = fixture();
  assert.deepEqual((await f.handlers.stats(f.request(), f.context)).jsonBody, {
    metric: "deployment_launch_clicks", totalClicks: 0, byDay: {},
  });
});

test("unavailable statistics return an explicit failure, not a misleading zero", async () => {
  const f = fixture({ async *listEntities() { throw new Error("private diagnostic"); } });
  const response = await f.handlers.stats(f.request(), f.context);
  assert.equal(response.status, 503);
  assert.deepEqual(response.jsonBody, { error: "Statistics unavailable" });
});
