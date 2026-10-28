// ==UserScript==
// @name         Jamly status helper
// @namespace    https://github.com/kurtisnotcurtis/jamly
// @version      0.3.0
// @description  Optional status/enqueue helper for a paired local Jamly backend.
// @match        https://www.youtube.com/*
// @match        https://music.youtube.com/*
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_registerMenuCommand
// @connect      127.0.0.1
// @connect      localhost
// @run-at       document-end
// ==/UserScript==

(function () {
  "use strict";
  let closed = false;
  let timer;
  const panel = document.createElement("aside");
  panel.style.cssText = "position:fixed;right:12px;bottom:12px;z-index:2147483647;background:#202124;color:white;padding:12px;font:14px system-ui;max-width:320px";
  const status = document.createElement("div");
  const split = document.createElement("button");
  const close = document.createElement("button");
  split.textContent = "Split this page";
  close.textContent = "Close";
  panel.append(status, split, close);
  document.body.appendChild(panel);

  GM_registerMenuCommand("Pair Jamly", () => {
    const token = window.prompt("Paste the token printed by jamly-service token:");
    if (token) GM_setValue("token", token.trim());
  });

  function request(path, body) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        url: "http://127.0.0.1:8765" + path,
        method: body === undefined ? "GET" : "POST",
        headers: {
          Authorization: "Bearer " + GM_getValue("token", ""),
          "Content-Type": "application/json",
        },
        data: body === undefined ? undefined : JSON.stringify(body),
        timeout: 10000,
        onload: response => {
          try {
            const value = JSON.parse(response.responseText);
            if (response.status < 200 || response.status >= 300) throw new Error(value.error);
            resolve(value);
          } catch (error) { reject(error); }
        },
        onerror: () => reject(new Error("Cannot reach Jamly. Start jamly-server.")),
        ontimeout: () => reject(new Error("Jamly request timed out.")),
      });
    });
  }

  async function poll() {
    if (closed) return;
    try {
      const state = await request("/status");
      if (state.api_version !== 3) throw new Error("Update the backend and userscript together.");
      status.textContent = state.current
        ? "Jamly: " + state.current.stage
        : `Queued ${state.pending}; complete ${state.done}; failed ${state.failed}.`;
      status.textContent += " Use the Jamly extension to download or retry results.";
    } catch (error) {
      status.textContent = error.message || String(error);
    } finally {
      if (!closed) timer = setTimeout(poll, 3000);
    }
  }
  split.addEventListener("click", async () => {
    try {
      split.disabled = true;
      const response = await request("/add", { inputs: [location.href] });
      status.textContent = `Queued ${response.newly_added} job(s).`;
    } catch (error) {
      status.textContent = error.message || String(error);
    } finally { split.disabled = false; }
  });
  close.addEventListener("click", () => {
    closed = true;
    clearTimeout(timer);
    panel.remove();
  });
  poll();
})();
