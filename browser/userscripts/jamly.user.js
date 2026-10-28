/* jamly status UI userscript.

   Shows live progress from a running `python -m jamly.server` (GET /status SSE) in a floating
   panel, and alerts when items finish with links back to the source URL. Zero-install: install
   via Tampermonkey/Greasemonkey (or your userscript manager), then run the server on another tab.
   Queue persistence is owned by the local jamly server.

   Protocol shared with the WebExtension background variant (jamly_client.js):
     GET  /status -> {pending, done, total, current, last_completed}
     POST /add    body {"inputs":[...], "titles":[...]} -> {queued, newly_added, pending_count}
     GET  /items  -> {pending:[], done:[]}
     GET  /healthz-> {ok:true,...}
*/
// ==UserScript==
// @name         jamly status UI
// @namespace    https://example.com/jamly
// @version      1.0.0
// @description  Live queue progress + completion alerts for the jamly HTTP status server.
// @author       jamly
// @match        https://www.youtube.com/*
// @match        https://music.youtube.com/*
// @grant        GM_addStyle
// @connect      127.0.0.1
// @connect      localhost
// @run-at       document-end
// ==/UserScript==

(function () {
  "use strict";

  var POLL_INTERVAL_MS = 1000;   // poll /status every second (server emits ~once/sec)
  var SERVER_URL = "http://127.0.0.1:8765"; // where `python -m jamly.server` is listening
  var SERVER_TOKEN = ""; // set when the server requires JAMLY_SERVER_TOKEN

  // --- styling (GM_addStyle; no CSS file dependency) -------------------------------------------
  if (typeof GM_addStyle === "function") {
    GM_addStyle(
      "#ac-panel{position:fixed;top:12px;right:12px;width:320px;background:#1e1e1e;color:#e6e6e6;" +
        "border:1px solid #0078d4;border-radius:8px;font:13px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;box-shadow:0 4px 20px rgba(0,0,0,.5);z-index:2147483647;user-select:none}" +
        "#ac-panel .ac-head{display:flex;justify-content:space-between;align-items:center;padding:8px 10px;background:#252526;border-bottom:1px solid #333}" +
        "#ac-title{font-weight:600;color:#4fc3f7}" +
        "#ac-close{cursor:pointer;color:#9aa;font-weight:bold}" +
        "#ac-body{padding:8px 10px;max-height:520px;overflow:auto}" +
        "#ac-counts{font-size:11px;color:#888;margin-bottom:6px}" +
        "#ac-status{color:#ffd54f;font-weight:600;min-height:1.5em}" +
        "#ac-current-title{color:#fff;font-size:13px;word-break:break-all;margin:2px 0 6px}" +
        "#ac-list{list-style:none;margin:0;padding:0;max-height:240px;overflow:auto}" +
        "#ac-list li{padding:4px 6px;border-bottom:1px solid #333;display:flex;align-items:center;gap:6px}" +
        "#ac-list .ac-done{color:#4caf50}#ac-list .ac-pending{color:#bbb}" +
        "#ac-empty{color:#777;font-style:italic}"
    );
  }

  var panel = document.createElement("div");
  panel.id = "ac-panel";
  // create every element the render functions below write to (#ac-counts / #ac-status /
  // #ac-current-title / #ac-list); the panel used to build only #ac-body, so those writes hit a
  // null node and threw. Build them here so the first poll paints without error.
  panel.innerHTML =
    '<div class="ac-head"><span id="ac-title">jamly</span><span id="ac-close" title="close">×</span></div>' +
    '<div id="ac-body">' +
      '<div id="ac-counts"></div>' +
      '<div id="ac-status"></div>' +
      '<div id="ac-current-title"></div>' +
      '<ul id="ac-list"></ul>' +
    '</div>';
  document.body.appendChild(panel);
  var closeBtn = panel.querySelector("#ac-close");
  if (closeBtn) closeBtn.addEventListener("click", function () { panel.remove(); });

  // --- helpers ---------------------------------------------------------------------------------
  function counts(body) {
    var pending = body.pending, done = body.done;
    return [
      pending == null ? 0 : Math.max(0, pending),
      done == null ? 0 : Math.max(0, done)
    ];
  }

  function renderCounts(pending, done) {
    document.querySelector("#ac-counts").textContent =
      "pending: " + pending + "   ·   done: " + done;
  }

  function renderStatus(current) {
    var el = document.querySelector("#ac-status");
    if (!current || current.status === "idle") {
      el.textContent = "server idle — nothing running";
      return false;
    }
    el.textContent = "[" + (current.stage || "?") + "] " + (current.status || "?").toUpperCase();
    var titleEl = document.querySelector("#ac-current-title");
    if (titleEl) titleEl.textContent = current.title ? current.title : "";
    return true;
  }

  function renderList(last_completed, inputs, done_ids) {
    var list = document.querySelector("#ac-list");
    // pending items (from the queue file) with a clickable link back to their source URL
    if (!inputs || !inputs.length) {
      if (last_completed && last_completed.length) {
        list.innerHTML = renderCompleted(last_completed);
        return;
      }
      list.innerHTML = '<li id="ac-empty">no items yet</li>';
      return;
    }
    var done = done_ids || [];
    var rows = inputs.map(function (u, i) {
      var state = done.indexOf(u) >= 0 ? "done" : "pending";
      return '<li class="ac-' + state + '" title="open source"><a href="' + escapeAttr(u) + '" target="_blank" style="color:inherit;text-decoration:none">● ' + escapeHtml(u) + '</a></li>';
    }).join("");
    // surface terminal records from the server too (done/error), appended after pending rows
    if (last_completed && last_completed.length) {
      rows += renderCompleted(last_completed);
    }
    list.innerHTML = rows || '<li id="ac-empty">no items yet</li>';
  }

  function renderCompleted(last_completed) {
    var out = "";
    for (var i = 0; i < last_completed.length; i++) {
      var rec = last_completed[i];
      if (!rec || !rec.watch_id) continue;
      var successful = rec.status === "done" || rec.status === "skipped";
      var cls = successful ? "ac-done" : "ac-pending";
      var mark = successful ? "✔" : "✗";
      var label = escapeHtml(rec.title || rec.watch_id);
      out += '<li class="' + cls + '">' + mark + ' ' + label + '</li>';
    }
    return out;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function escapeAttr(s) { return escapeHtml(s); }

  // --- poll loop -------------------------------------------------------------------------------
  async function poll() {
    try {
      var headers = SERVER_TOKEN ? { Authorization: "Bearer " + SERVER_TOKEN } : {};
      var resp = await fetch(SERVER_URL + "/status?once=1", {
        cache: "no-store",
        headers: headers
      });
      if (!resp.ok) return;
      var txt = await resp.text();
      var m = txt.match(/\{[\s\S]*\}/); // outermost JSON object (server writes data:{...} ticks)
      if (!m) return;
      var body = JSON.parse(m[0]);
      var totals = counts(body);
      renderCounts(totals[0], totals[1]);
      renderStatus(body.current);
      renderList(body.last_completed || [], null, body.done);
    } catch (e) { /* transient — next poll will retry */ }
  }

  async function start() {
    setInterval(poll, POLL_INTERVAL_MS);
    poll();
  }

  if (typeof document !== "undefined") start();
})();
