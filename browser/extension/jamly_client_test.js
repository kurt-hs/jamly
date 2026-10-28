"use strict";
const assert = require("node:assert/strict");
const http = require("node:http");
const { test } = require("node:test");
const client = require("./jamly_client.js");

async function backend(t) {
  const received = [];
  const server = http.createServer((request, response) => {
    const chunks = [];
    request.on("data", chunk => chunks.push(chunk));
    request.on("end", () => {
      response.setHeader("Content-Type", "application/json");
      if (request.headers.authorization !== "Bearer paired") {
        response.writeHead(401);
        response.end(JSON.stringify({ error: "Pair the client first" }));
        return;
      }
      if (request.url === "/status") {
        response.end(JSON.stringify({ api_version: 3, jobs: [], pending: 0, done: 0 }));
      } else {
        received.push({ url: request.url, body: JSON.parse(Buffer.concat(chunks).toString()) });
        response.writeHead(202);
        response.end(JSON.stringify({ newly_added: 1 }));
      }
    });
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  return { config: { url: "http://127.0.0.1:" + server.address().port, token: "paired" }, received };
}

test("paired JSON status, submission and retry preserve input and options", async t => {
  const { config, received } = await backend(t);
  assert.equal((await client.status(config)).api_version, 3);
  assert.equal((await client.addInputs(config, ["https://example.test/a"], true, {
    model: "htdemucs_ft", audio_format: "wav",
  })).newly_added, 1);
  await client.retry(config, "src-id", false);
  assert.deepEqual(received, [
    { url: "/add", body: {
      inputs: ["https://example.test/a"], replace_existing: true, model: "htdemucs_ft", audio_format: "wav",
    } },
    { url: "/retry/src-id", body: { replace_existing: false } },
  ]);
});

test("HTTP errors reject rather than becoming successful queue responses", async t => {
  const { config } = await backend(t);
  await assert.rejects(client.status({ ...config, token: "" }), /Pair the client first/);
  await assert.rejects(client.addInputs({ ...config, token: "wrong" }, ["url"]), /Pair the client first/);
});

test("artifact downloads stay on the paired backend", () => {
  const config = { url: "https://example.test/jamly" };
  assert.equal(client.outputURL(config, "/outputs/id/drums?generation=one"),
               "https://example.test/jamly/outputs/id/drums?generation=one");
  assert.throws(() => client.outputURL(config, "https://other.example/file"), /invalid/);
  assert.throws(() => client.outputURL(config, "/outputs/../../private"), /invalid/);
  assert.throws(() => client.baseURL("http://127.0.0.1/?token=secret"), /base URL/);
});
