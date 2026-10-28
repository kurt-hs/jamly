"use strict";
importScripts("jamly_client.js");

async function config() {
  const stored = await chrome.storage.local.get(["ac_serverUrl", "ac_serverToken"]);
  return {
    url: stored.ac_serverUrl || "http://127.0.0.1:8765",
    token: stored.ac_serverToken || "",
  };
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (sender.id !== chrome.runtime.id || !message) return false;
  (async () => {
    const settings = await config();
    switch (message.type) {
      case "status":
        return jamlyClient.status(settings);
      case "enqueue":
        return jamlyClient.addInputs(settings, message.inputs, Boolean(message.replace), {
          model: message.model,
        });
      case "retry":
        return jamlyClient.retry(settings, message.id, Boolean(message.replace));
      case "download": {
        const url = jamlyClient.outputURL(settings, message.output.url);
        const filename = String(message.output.name).replace(/[\\/]/g, "_");
        return chrome.downloads.download({
          url, filename, saveAs: true,
          headers: [{ name: "Authorization", value: "Bearer " + settings.token }],
        });
      }
      default:
        throw new Error("Unknown Jamly request.");
    }
  })().then(
    result => sendResponse({ ok: true, result }),
    error => sendResponse({ ok: false, error: error.message || String(error) }),
  );
  return true;
});
