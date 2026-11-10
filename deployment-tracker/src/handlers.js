"use strict";

const { randomUUID } = require("node:crypto");

const DESTINATION = "https://codespaces.new/microsoft/ignite26-brk301?quickstart=1&ref=main";
const PARTITION = "readme-deploy";
const NO_CACHE = { "Cache-Control": "no-store, max-age=0", Pragma: "no-cache" };

function isLaunchClick(request) {
  if (request.method !== "GET") return false;
  const header = (name) => request.headers.get(name) || "";
  if (/prefetch|preview/i.test(`${header("purpose")} ${header("sec-purpose")}`)) return false;
  if (/bot|crawler|spider|preview|slack|discord|facebookexternalhit/i.test(header("user-agent"))) return false;
  const destination = header("sec-fetch-dest");
  return !destination || destination === "document";
}

function createHandlers({ store, now = () => new Date(), uuid = randomUUID }) {
  return {
    async deploy(request, context) {
      if (isLaunchClick(request)) {
        try {
          const client = await store();
          await client.createEntity({
            partitionKey: PARTITION,
            rowKey: uuid(),
            clickedAt: now().toISOString(),
          }, { abortSignal: AbortSignal.timeout(2500) });
        } catch {
          context.error("Deployment launch click could not be recorded.");
        }
      }
      return { status: 302, headers: { ...NO_CACHE, Location: DESTINATION } };
    },

    async stats(_request, context) {
      try {
        const client = await store();
        const byDay = {};
        let totalClicks = 0;
        for await (const entity of client.listEntities({
          queryOptions: { filter: `PartitionKey eq '${PARTITION}'`, select: ["clickedAt"] },
        })) {
          const day = entity.clickedAt.slice(0, 10);
          byDay[day] = (byDay[day] || 0) + 1;
          totalClicks++;
        }
        return {
          status: 200, headers: NO_CACHE,
          jsonBody: {
            metric: "deployment_launch_clicks",
            totalClicks,
            byDay: Object.fromEntries(Object.entries(byDay).sort()),
          },
        };
      } catch {
        context.error("Deployment launch statistics could not be read.");
        return { status: 503, headers: NO_CACHE, jsonBody: { error: "Statistics unavailable" } };
      }
    },
  };
}

module.exports = { createHandlers };
