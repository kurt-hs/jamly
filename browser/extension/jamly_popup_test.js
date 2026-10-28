"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { test } = require("node:test");
const tick = () => new Promise(resolve => setImmediate(resolve));

function popup(handler) {
  function element() {
    return {
      textContent: "", value: "", checked: false, children: [], events: {},
      addEventListener(name, callback) { this.events[name] = callback; },
      appendChild(child) { this.children.push(child); },
      replaceChildren() { this.children = []; },
    };
  }
  const elements = Object.fromEntries(
    ["connection", "counts", "error", "jobs", "options", "current", "inputs", "replace", "submit", "model", "model-help"]
      .map(id => [id, element()])
  );
  const context = {
    document: { getElementById: id => elements[id], createElement: element },
    window: { addEventListener() {} },
    setTimeout() { return 1; }, clearTimeout() {},
    chrome: {
      runtime: { sendMessage: handler, openOptionsPage() {} },
      tabs: { async query() { return [{ url: "https://example.test/current" }]; } },
    },
  };
  vm.runInNewContext(fs.readFileSync(__dirname + "/jamly_popup.js", "utf8"), context);
  return elements;
}

test("popup renders text safely and exposes paired result downloads", async () => {
  const messages = [];
  const elements = popup(async message => {
    messages.push(message);
    return { ok: true, result: { pending: 0, done: 1, failed: 0,
      models: [{ id: "htdemucs", label: "Balanced", description: "Faster" }],
      defaults: { model: "htdemucs" }, jobs: [{
      watch_id: "id", title: "<img src=x onerror=alert(1)>", status: "done",
      outputs: [{ kind: "drums", url: "/outputs/id/drums?generation=one", name: "drums.wav" }],
    }] } };
  });
  await tick();
  const row = elements.jobs.children[0];
  assert.equal(row.children[0].textContent, "DONE: <img src=x onerror=alert(1)>");
  await row.children[2].events.click();
  assert.equal(messages.at(-1).type, "download");
});

test("the readable quality choice explains the cost and reaches submission", async () => {
  const messages = [];
  const elements = popup(async message => {
    messages.push(message);
    return { ok: true, result: {
      pending: 0, done: 0, failed: 0, jobs: [], defaults: { model: "htdemucs" },
      models: [
        { id: "htdemucs", label: "Balanced", description: "Faster" },
        { id: "htdemucs_ft", label: "Higher quality", description: "About 4x longer" },
      ],
    } };
  });
  await tick();
  assert.equal(elements.model.children[1].textContent, "Higher quality");
  elements.model.value = "htdemucs_ft";
  elements.model.events.change();
  assert.match(elements["model-help"].textContent, /4x/);
  elements.inputs.value = "https://example.test/track";
  await elements.submit.events.click();
  assert.equal(messages.find(message => message.type === "enqueue").model, "htdemucs_ft");
});

test("offline and rejected submissions are visible, never queued successes", async () => {
  const elements = popup(async () => ({ ok: false, error: "authorization required" }));
  await tick();
  assert.match(elements.connection.textContent, /Offline or not paired/);
  elements.inputs.value = "https://example.test/a\nhttps://example.test/b";
  await elements.submit.events.click();
  assert.equal(elements.error.textContent, "authorization required");
  assert.equal(elements.connection.textContent.includes("queued undefined"), false);
});
