// Background service worker for the auto_convert WebExtension.
//
// Shares ONE client implementation with the Tampermonkey userscript by importing the same sibling
// module (auto_convert_client.js) via importScripts(). Both clients therefore speak identical
// protocol to a running `python -m auto_convert.server`:
//   GET  /status -> {pending, done, total, current, last_completed}
//   POST /add    body {"inputs":[...], "titles":[...]} -> {queued, newly_added, pending_count}
//
// The popup enqueues jobs (POST /add) and polls progress (GET /status); the server is the source of
// truth and persists/resumes state on disk. This lets users enqueue from an always-on background page
// instead of driving a CLI process interactively. SERVER_URL defaults to 127.0.0.1:8765 and can be
// overridden through the extension options UI (ac.serverUrl message).

importScripts(chrome.runtime.getURL("auto_convert_client.js"));

var SERVER_URL = "http://127.0.0.1:8765"; // default; settable from the options UI (ac_serverUrl)
chrome.storage.local.get(["ac_serverUrl"], function (items) {
  if (items && items.ac_serverUrl) SERVER_URL = items.ac_serverUrl;
});
// re-sync when the user edits the URL in options
if (chrome.storage && chrome.storage.onChanged) {
  chrome.storage.onChanged.addListener(function (changes, area) {
    if (area === "local" && changes.ac_serverUrl) {
      SERVER_URL = changes.ac_serverUrl.newValue || "http://127.0.0.1:8765";
    }
  });
}

// Poll /status every second and keep the latest snapshot in storage + relay it to the popup.
setInterval(function () {
  try {
    var snap = autoConvertClient.pollStatus(SERVER_URL);
    if (snap) chrome.storage.local.set({ ac_status: snap });
    if (chrome.runtime && chrome.runtime.sendMessage) {
      // best-effort fan-out to any open popup; failures are caught by the caller
      try { chrome.runtime.sendMessage({ type: "ac.status", payload: snap }); } catch (e) {}
    }
  } catch (e) { /* server may be idle/unreachable — next tick retries */ }
}, 1000);

// Expose enqueue + config to the popup over an async message channel.
chrome.runtime.onMessage.addListener(function (msg, sender, sendResponse) {
  if (!msg || !sender) return;

  if (msg.type === "ac.serverUrl") {
    SERVER_URL = msg.url || SERVER_URL;
    sendResponse({ ok: true });
    return false; // synchronous reply only
  }

  if (msg.type === "ac.enqueue") {
    var inputs = Array.isArray(msg.inputs) ? msg.inputs.map(String).filter(Boolean) : [];
    var titles = Array.isArray(msg.titles) ? msg.titles.filter(Boolean) : [];
    autoConvertClient.addInputs(SERVER_URL, inputs, titles).then(function (resp) {
      sendResponse({ ok: true, resp: resp });
    }).catch(function (err) {
      sendResponse({ ok: false, error: String(err) });
    });
    return true; // keep the channel open until async work resolves
  }

  if (msg.type === "ac.status") {
    chrome.storage.local.set({ ac_status: msg.payload });
    try { chrome.runtime.sendMessage({ type: "ac.status", payload: msg.payload }); } catch (e) {}
    return false; // relay to popups, no reply needed here
  }
});
