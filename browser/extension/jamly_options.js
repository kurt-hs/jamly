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
    mix:      { icon: "\U0001F5A4", label: "MIXING" },
    done:     { icon: "\u2714", label: "DONE" },
    error:    { icon: "\u2717", label: "ERROR" },
  };
  function badge(status, title) {
    var s = status || "idle";
    var st = STAGE[s] || STAGE.idle;
    return '<span class="badge ' + (s === "done" ? "done" : s === "error" ? "error" : s) + '">' +
           st.icon + ' <span class="lbl">' + st.label + '</span>' +
           ((title) ? ' \u2014 ' + title : '') + '</span>';
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
      document.getElementById("ac_status").innerHTML = badge(cur.status, cur.title || (cur.watch_id ? cur.watch_id[0] + "\u2026" : '')) +
        '   \u00b7   pending: ' + c[0] + '   done: ' + c[1];
    });
  }
  render();
  chrome.storage.onChanged.addListener(function (changes, area) {
    if (area === "local" && changes.ac_status) { var snap = changes.ac_status.newValue; if (snap) render(); }
  });
  chrome.storage.local.get(["ac_serverUrl"], function (items) {
    var url = items && items.ac_serverUrl || "http://127.0.0.1:8765";
    document.getElementById("url").value = url;
  });
  document.getElementById("save").addEventListener("click", function () {
    chrome.storage.local.set({ ac_serverUrl: document.getElementById("url").value.trim() || "http://127.0.0.1:8765" }, function () {
      alert("Saved.");
    });
  });
})();
