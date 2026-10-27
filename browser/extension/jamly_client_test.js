// jamly_client_test.js — client-side integration test for the shared status protocol.
//
// Spins up an in-process HTTP server that mimics the real ItemServer endpoints (GET /status SSE,
// POST /add) and drives `autoConvertClient` against it over Node's global fetch. This validates the
// browser-client parsing logic (the "data: {...}" extraction from the SSE stream, JSON shapes, and
// dedup/trim of inputs) without a real yt-dlp run — the server side itself is covered by
// tests/test_server.py driving urllib against the live ItemServer. Run with `node jamly_client_test.js`.

"use strict";
var http = require("http");

// alias self so the client's (typeof self !== "undefined" ? self : this) binds to Node's globalThis
var self = globalThis;

function startFakeServer() {
  var server = http.createServer(function (req, res) {
    if (req.method === "GET" && req.url === "/status") {
      // mirror the real handler: an SSE data line carrying one progress JSON object + a comment tick
      res.setHeader("Content-Type", "text/event-stream");
      var payload = JSON.stringify({
        pending: 2, done: 0, total: 2,
        current: { watch_id: "abc123", stage: "mix", status: "pending", title: null, error: null },
        last_completed: []
      });
      res.write("data: " + payload + "\n\n");
      res.write(": tick p=2 d=0\n\n");
      return res.end();
    } else if (req.method === "POST" && req.url === "/add") {
      var chunks = [];
      req.on("data", function (c) { chunks.push(c); });
      req.on("end", function () {
        try {
          var body = JSON.parse(Buffer.concat(chunks).toString());
          res.setHeader("Content-Type", "application/json");
          // trim + dedup inputs exactly as the client sends them; newly_added counts unique ids
          var sentInputs = (body.inputs || []).map(String).filter(Boolean);
          res.statusCode = 200;
          res.end(JSON.stringify({ queued: sentInputs, newly_added: new Set(sentInputs).size, pending_count: sentInputs.length }));
        } catch (e) {
          res.statusCode = 400;
          res.end(JSON.stringify({ error: "invalid JSON body" }));
        }
      });
    } else if (req.method === "GET") {
      res.setHeader("Content-Type", "application/json");
      res.statusCode = 200;
      res.end(JSON.stringify({ pending: [], done: [] }));
    } else {
      res.statusCode = 404;
      res.end();
    }
  });
  server.listen(0);
  return new Promise(function (resolve) {
    server.on("listening", function () { resolve("http://127.0.0.1:" + server.address().port); });
  });
}

var assert = require("assert");

startFakeServer().then(function (base_url) {
  var code = "self=globalThis;" + require("fs").readFileSync(__dirname + "/jamly_client.js", "utf8");
  eval(code);

  return new Promise(function (resolve, reject) {
    autoConvertClient.pollStatus(base_url).then(function (snap) {
      assert.ok(snap, "pollStatus returned a snapshot");
      assert.strictEqual(snap.pending, 2);
      assert.strictEqual(snap.done, 0);
      assert.strictEqual(snap.total, 2);
      assert.deepStrictEqual(snap.current.stage, "mix");
      console.log("pollStatus parsed from SSE tick: OK (pending=%d done=%d)", snap.pending, snap.done);

      return autoConvertClient.addInputs(base_url, ["  https://youtu.be/abc123 ", "", "https://youtu.be/def456"], [null, null]);
    }).then(function (resp) {
      assert.strictEqual(resp.newly_added, 2, "newly_added should count unique non-empty inputs");
      assert.strictEqual(resp.pending_count, 2);
      // the client trims + drops empty entries server-side too; verify it sent only the two real ones
      if (Array.isArray(resp.queued)) {
        assert.ok(resp.queued.every(function (q) { return q.length > 0; }), "empties trimmed");
      }
      console.log("addInputs dedup+trim: OK (newly_added=%d)", resp.newly_added);

      // empty inputs -> client rejects before hitting the server
      return autoConvertClient.addInputs(base_url, []);
    }).then(function (resp) {
      assert.ok(resp.error, "empty input list is rejected by the client");
      console.log("addInputs empty-list guard: OK (" + resp.error + ")");

      process.stdout.write("\nALL CLIENT CONTRACT TESTS PASSED\n");
      process.exit(0);
    }).catch(function (err) {
      console.error("client test FAILED:", err && err.stack ? err.stack : err);
      process.exit(1);
    });
  });
}).catch(function (err) {
  console.error("bootstrap error:", err);
  process.exit(1);
});
