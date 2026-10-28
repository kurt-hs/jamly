"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { test } = require("node:test");

function worker(client, settings) {
  let listener;
  const downloads = [];
  const context = {
    jamlyClient: client,
    importScripts() {},
    setInterval() { throw new Error("MV3 workers must not rely on permanent polling"); },
    chrome: {
      runtime: { id: "jamly", onMessage: { addListener(fn) { listener = fn; } } },
      storage: { local: { async get() { return await settings; } } },
      downloads: { async download(options) { downloads.push(options); return 42; } },
    },
  };
  vm.runInNewContext(fs.readFileSync(__dirname + "/jamly_extension_bg.js", "utf8"), context);
  return {
    send: message => new Promise(resolve => listener(message, { id: "jamly" }, resolve)),
    downloads,
  };
}

test("a cold worker waits for saved configuration before making requests", async () => {
  let resolveSettings;
  const settings = new Promise(resolve => { resolveSettings = resolve; });
  let used;
  const background = worker({
    async status(config) { used = config; return { api_version: 3, jobs: [] }; },
  }, settings);
  const response = background.send({ type: "status" });
  assert.equal(used, undefined);
  resolveSettings({ ac_serverUrl: "https://backend.example", ac_serverToken: "paired" });
  assert.equal((await response).ok, true);
  assert.equal(used.url, "https://backend.example");
  assert.equal(used.token, "paired");
});

test("failed requests remain failed across the runtime message boundary", async () => {
  const background = worker({
    async addInputs() { throw new Error("authorization required"); },
  }, {});
  const response = await background.send({ type: "enqueue", inputs: ["url"] });
  assert.equal(response.ok, false);
  assert.equal(response.error, "authorization required");
});

test("downloads carry the pairing token as a header, never in the URL", async () => {
  const background = worker({
    outputURL(_config, url) { return "http://127.0.0.1:8765" + url; },
  }, { ac_serverToken: "paired" });
  const response = await background.send({
    type: "download", output: { url: "/outputs/id/drums?generation=one", name: "drums.wav" },
  });
  assert.equal(response.ok, true);
  assert.equal(background.downloads[0].headers[0].value, "Bearer paired");
  assert.equal(background.downloads[0].url.includes("paired"), false);
});
