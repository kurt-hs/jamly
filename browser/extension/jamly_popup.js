// jamly_popup.js — popup logic for the jamly status WebExtension.
//
// Kept as an EXTERNAL script (referenced from jamly_popup.html via <script src>) so the popup
// complies with Manifest V3, which blocks inline scripts under its default CSP. The logic is
// identical to the historical inline version; see the shared server contract in jamly_client.js.
(function () {
  "use strict";

  // Shared download/pipeline vocabulary (kept identical to the CLI's per-item rendering):
  // stage -> {icon, label}. Color is applied by a CSS class (.badge.<cls>) below.
  var STAGE = {
    idle:     { icon: "\u25cc", label: "IDLE" },
    pending:  { icon: "\u25b0", label: "QUEUED" },
    download: { icon: "\u2b1b", label: "EXTRACTING" },
    separate: { icon: "\u2702\ufe0f", label: "SEPARATING" },
    mix:      { icon: "\u{1F5A4}", label: "MIXING" },
    done:     { icon: "\u2714", label: "DONE" },
    error:    { icon: "\u2717", label: "ERROR" },
    skipped:  { icon: "\u2714", label: "SKIPPED" },
  };

  function escapeHtml(value) {
    return String(value || "").replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function counts(body) {
    var p = (body && body.pending == null ? 0 : Math.max(0, body.pending));
    var d = (body && body.done == null ? 0 : Math.max(0, body.done));
    return [p, d];
  }

  // Render a single stage badge: "\u2714 DONE" / "\u2b1b EXTRACTING", plus the title when present.
  function badge(status, title) {
    var s = status || "idle";
    var st = STAGE[s] || STAGE.idle;
    return '<span class="badge ' + (s === "done" || s === "skipped" ? "done" : s === "error" ? "error" : s) + '">' +
           st.icon + ' <span class="lbl">' + st.label + '</span>' +
           ((title) ? ' &mdash; ' + escapeHtml(title) : '') + '</span>';
  }

  function renderStatus(body) {
    var el = document.getElementById("status");
    if (!body || !body.current || body.current.status === "idle") {
      el.className = "idle";
      el.textContent = "server idle — nothing running";
      return;
    }
    el.className = "";
    var cur = body.current;
    el.innerHTML = badge(cur.status, cur.title || (cur.watch_id ? cur.watch_id.slice(0, 12) + "\u2026" : ''));
    document.getElementById("current-title").textContent = cur.title || '';
  }

  function renderList(body) {
    var list = document.getElementById("list");
    var last_completed = body && body.last_completed || [];
    // completed terminal records from the server; the running item is shown live in #status above
    if (!last_completed.length) {
      list.innerHTML = '<li class="pending">no items yet</li>';
      return;
    }
    var out = "";
    for (var i = 0; i < last_completed.length; i++) {
      var rec = last_completed[i];
      if (!rec || !rec.watch_id) continue;
      // terminal status is only ever 'done' or 'error'; otherwise fall back to the watch id
      out += '<li>' + badge(rec.status, rec.title || rec.watch_id) + '</li>';
    }
    list.innerHTML = out;
  }

  function apply(body) {
    var c = counts(body);
    document.getElementById("counts").textContent = "pending: " + c[0] + "   \u00b7   done: " + c[1];
    renderStatus(body);
    renderList(body);
  }

  // restore latest snapshot from storage on load
  chrome.storage.local.get(["ac_status"], function (items) {
    if (items.ac_status) apply(items.ac_status);
  });

  // refresh whenever the SW relays a new /status snapshot through storage
  chrome.storage.onChanged.addListener(function (changes, area) {
    if (area === "local" && changes.ac_status) {
      var snap = changes.ac_status.newValue;
      if (snap) apply(snap);
    }
  });

  // enqueue form -> POST /add via the service worker's shared client
  document.getElementById("submit").addEventListener("click", function () {
    var inputsEl = document.getElementById("inputs");
    var titlesEl = document.getElementById("titles");
    var inputs = inputsEl.value.split("\n").map(function (s) { return s.trim(); }).filter(Boolean);
    if (!inputs.length) {
      document.getElementById("status").textContent = "nothing to enqueue";
      return;
    }
    var titles = titlesEl.value.split("\n").map(function (s) { return s.trim(); });
    chrome.runtime.sendMessage({ type: "ac.enqueue", inputs: inputs, titles: titles }, function (resp) {
      if (!chrome.runtime || !chrome.runtime.lastError) {
        document.getElementById("status").textContent = resp && resp.ok ? ("queued " + (resp.resp ? resp.resp.newly_added : inputs.length)) : (resp.error || "enqueue failed");
      } else {
        document.getElementById("status").textContent = chrome.runtime.lastError.message;
      }
    });
  });
})();
