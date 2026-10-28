(function () {
  "use strict";
  const byId = id => document.getElementById(id);
  chrome.storage.local.get(["ac_serverUrl", "ac_serverToken"]).then(stored => {
    byId("url").value = stored.ac_serverUrl || "http://127.0.0.1:8765";
    byId("token").value = stored.ac_serverToken || "";
  });

  byId("save").addEventListener("click", async () => {
    try {
      const config = {
        url: jamlyClient.baseURL(byId("url").value.trim()),
        token: byId("token").value.trim(),
      };
      if (!config.token) throw new Error("Enter the token from 'jamly-service token'.");
      const parsed = new URL(config.url);
      const origin = parsed.protocol + "//" + parsed.hostname + "/*";
      const granted = await chrome.permissions.request({ origins: [origin] });
      if (!granted) throw new Error("Host permission is required for this backend.");
      await chrome.storage.local.set({ ac_serverUrl: config.url, ac_serverToken: config.token });
      await jamlyClient.status(config);
      byId("status").textContent = "Paired. Open the Jamly toolbar button to split a track.";
    } catch (error) {
      byId("status").textContent = error.message || String(error);
    }
  });
})();
