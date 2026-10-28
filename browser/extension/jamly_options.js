// jamly_options.js — options page logic for the jamly status WebExtension.
//
// Kept as an EXTERNAL script (referenced from jamly_options.html via <script src>) so the options
// page complies with Manifest V3, which blocks inline scripts under its default CSP. The logic is
// identical to the historical inline version; see the shared server contract in jamly_client.js.
(function () {
  "use strict";
  // Shared download/pipeline vocabulary (identical to the popup + CLI): stage -> icon/label.
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
  function badge(status, title) {
    var s = status || "idle";
    var st = STAGE[s] || STAGE.idle;
    return '<span class="badge ' + (s === "done" || s === "skipped" ? "done" : s === "error" ? "error" : s) + '">' +
           st.icon + ' <span class="lbl">' + st.label + '</span>' +
           ((title) ? ' \u2014 ' + escapeHtml(title) : '') + '</span>';
  }
  function counts(body) {
    var p = (body && body.pending == null ? 0 : Math.max(0, body.pending));
    var d = (body && body.done == null ? 0 : Math.max(0, body.done));
    return [p, d];
  }
  function render() {
    if (!chrome.storage || !chrome.storage.local) return;
    chrome.storage.local.get(["ac_status"], function (items) {
      var ac = items && items.ac_status;
      if (!ac) { document.getElementById("ac_status").textContent = "server not reachable"; return; }
      var c = counts(ac);
      var cur = ac.current || {};
      document.getElementById("ac_status").innerHTML = badge(cur.status, cur.title || (cur.watch_id ? cur.watch_id.slice(0, 12) + "\u2026" : '')) +
        '   \u00b7   pending: ' + c[0] + '   done: ' + c[1];
    });
  }
  render();
  chrome.storage.onChanged.addListener(function (changes, area) {
    if (area === "local" && changes.ac_status) { var snap = changes.ac_status.newValue; if (snap) render(); }
  });
  chrome.storage.local.get(["ac_serverUrl", "ac_serverToken"], function (items) {
    var url = items && items.ac_serverUrl || "http://127.0.0.1:8765";
    document.getElementById("url").value = url;
    document.getElementById("token").value = items && items.ac_serverToken || "";
  });
  document.getElementById("save").addEventListener("click", function () {
    var url = document.getElementById("url").value.trim() || "http://127.0.0.1:8765";
    var token = document.getElementById("token").value;
    var parsed;
    try {
      parsed = new URL(url);
    } catch (e) {
      alert("Enter a valid http(s) server URL.");
      return;
    }
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
      alert("The server URL must use http or https.");
      return;
    }
    var origin = parsed.protocol + "//" + parsed.hostname + "/*";
    chrome.permissions.request({ origins: [origin] }, function (granted) {
      if (!granted) {
        alert("Host permission is required for that server URL.");
        return;
      }
      chrome.storage.local.set({ ac_serverUrl: url, ac_serverToken: token }, function () {
        alert("Saved.");
      });
    });
  });
})();
