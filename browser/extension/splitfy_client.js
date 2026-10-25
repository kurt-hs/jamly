/* splitfy_client.js — shared HTTP-contract client for the splitfy status server.

   Shared by the Tampermonkey userscript (splitfy.user.js) and the WebExtension
   background variant (see manifest.json). Both clients talk to the SAME protocol so a user can
   enqueue from either UI against one running `python -m splitfy.server`. The contract:

       GET  /status   -> JSON snapshot {pending, done, total, current, last_completed}
       POST /add      body {"inputs":[...], "titles":[...]} -> {queued, newly_added, pending_count}
       GET  /items    -> {pending:[], done:[]}
       GET  /healthz  -> {ok:true,...}

   The server streams /status as text/event-stream (one progress JSON object per tick). This module
   is DOM-free so it can be dropped into any context (userscript, extension popup, or a local test)
   and validates with `node --check splitfy_client.js`.
*/
(function (global) {
  "use strict";

  var DEFAULT_ENDPOINT = "/status";
  var DEFAULT_ADD = "/add";
  var DEFAULT_ITEMS = "/items";
  var DEFAULT_HEALTHZ = "/healthz";

  // --- HTTP fetch with XHR fallback + a bounded read -----------------------------------------
  function _http(base_url, path, method, body) {
    return new Promise(function (resolve, reject) {
      try {
        if (typeof global.fetch === "function") {
          var headers = {};
          if (body !== undefined && !method.startsWith("GET")) {
            headers["Content-Type"] = "application/json";
            headers["Accept"] = "application/json";
          }
          var ctrl = new AbortController();
          try { globalThis.setTimeout(function () { ctrl.abort(); }, 10000); } catch (e) {} // best-effort hard cap; no setTimeout in some runtimes
          fetch(base_url + path, { method: method, headers: headers, body: body === undefined ? null : JSON.stringify(body), signal: ctrl.signal })
            .then(function (r) { return r.text().then(function (t) { return { status: r.status, text: t }; }); })   // await the async body read so 'text' is a real string, not a Promise
            .then(resolve, reject);
          return;
        }
      } catch (e) { /* fetch unavailable -> use XHR path */ }

      var x = new XMLHttpRequest();
      x.open(method, base_url + path, true);
      if (body !== undefined && !method.startsWith("GET")) {
        x.setRequestHeader("Content-Type", "application/json");
        x.setRequestHeader("Accept", "application/json");
        x.send(body === undefined ? null : JSON.stringify(body));
      } else {
        x.send(null);
      }
      x.onload = function () { resolve({ status: x.status, text: x.responseText }); };
      x.onerror = function () { reject(new Error("network error")); };
    });
  }

  // The server writes "data: {...}\n\n: tick ...\n\n". Extract the outermost JSON object.
  function _extract_status(text) {
    if (!text) return null;
    var m = text.match(/\{[\s\S]*\}/);
    if (!m) return null;
    try {
      var obj = JSON.parse(m[0]);
      // only accept the documented progress shape (guards against partial reads)
      if (obj && typeof obj === "object") return obj;
    } catch (e) { /* malformed -> null */ }
    return null;
  }

  function _base_url(base_url) {
    return (base_url || "").replace(/\/+$/, "");
  }

  // Poll GET /status once. Returns the parsed snapshot, or null if nothing usable yet.
  function pollStatus(base_url) {
    var url = _base_url(base_url);
    return _http(url, DEFAULT_ENDPOINT, "GET").then(function (resp) {
      return _extract_status(resp.text);
    });
  }

  // POST /add. Returns the parsed response object, or null on failure/empty body.
  function addInputs(base_url, inputs, titles) {
    var url = _base_url(base_url);
    var payload = { inputs: Array.isArray(inputs) ? inputs.map(function (i) { return String(i).trim(); }) : [], titles: Array.isArray(titles) ? titles : [] };
    if (!payload.inputs.length) return Promise.resolve({ error: "provide a non-empty 'inputs' list" });
    return _http(url, DEFAULT_ADD, "POST", payload).then(function (resp) {
      var parsed = null;
      try { parsed = JSON.parse(resp.text); } catch (e) { parsed = null; }
      if (!parsed && resp.status === 207) { // save-error: body is empty but status indicates a warning
        return { error: "save-error", queued: payload.inputs, status: resp.status };
      }
      return parsed || { status: resp.status, text: resp.text };
    });
  }

  function getItems(base_url) {
    var url = _base_url(base_url);
    return _http(url, DEFAULT_ITEMS, "GET").then(function (resp) {
      try { return JSON.parse(resp.text); } catch (e) { return null; }
    });
  }

  function healthz(base_url) {
    var url = _base_url(base_url);
    return _http(url, DEFAULT_HEALTHZ, "GET").then(function (resp) {
      try { return JSON.parse(resp.text); } catch (e) { return null; }
    });
  }

  // --- local queue-file helpers ------------------------------------------------------------
  function readQueue(path) {
    if (!path) return Promise.resolve({});
    var fs = global.fs;
    if (fs && typeof fs.existsSync === "function") {
      try {
        var txt = fs.readFileSync(path, "utf8");
        var trimmed = txt.trim();
        if (!trimmed) return Promise.resolve({});
        return Promise.resolve(JSON.parse(trimmed));
      } catch (e) { /* missing/corrupt -> empty queue */ }
    }
    try {
      var url = "file://" + path.replace(/^\/+/, "");
      return _http(url, "", "GET").then(function (resp) {
        if (!resp.text || !resp.text.trim()) return {};
        return JSON.parse(resp.text);
      });
    } catch (e) { /* unavailable */ return Promise.resolve({}); }
  }

  function writeQueue(path, obj) {
    if (!path) return Promise.resolve();
    var fs = global.fs;
    if (fs && typeof fs.writeFileSync === "function") {
      try {
        // best-effort atomic-ish: temp file + rename so a crash never leaves a half-written queue
        var tmp = path + ".tmp-" + Date.now();
        fs.writeFileSync(tmp, JSON.stringify(obj));
        try { fs.renameSync(tmp, path); } catch (e) { fs.unlinkSync(tmp); }
        return Promise.resolve();
      } catch (e) { /* best-effort; the running server owns durable state */ return Promise.resolve(); }
    }
    return Promise.resolve();
  }

  // Expose only what callers need.
  global.autoConvertClient = {
    DEFAULT_ENDPOINT: DEFAULT_ENDPOINT,
    DEFAULT_ADD: DEFAULT_ADD,
    pollStatus: pollStatus,
    addInputs: addInputs,
    getItems: getItems,
    healthz: healthz,
    readQueue: readQueue,
    writeQueue: writeQueue,
  };
})(typeof self !== "undefined" ? self : this);
