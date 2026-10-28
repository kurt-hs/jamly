(function (global) {
  "use strict";

  function baseURL(value) {
    const url = new URL(value || "http://127.0.0.1:8765");
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
      throw new Error("Use an http(s) backend base URL without credentials, query, or fragment.");
    }
    return url.href.replace(/\/+$/, "");
  }

  async function request(config, path, method = "GET", body) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 10000);
    try {
      const headers = { Accept: "application/json" };
      if (config.token) headers.Authorization = "Bearer " + config.token;
      if (body !== undefined) headers["Content-Type"] = "application/json";
      const response = await global.fetch(baseURL(config.url) + path, {
        method, headers, body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal, cache: "no-store",
      });
      let payload;
      try {
        payload = await response.json();
      } catch (_error) {
        throw new Error("The backend returned an invalid response.");
      }
      if (!response.ok) {
        throw new Error(payload.error || "Backend HTTP " + response.status);
      }
      return payload;
    } finally {
      clearTimeout(timer);
    }
  }

  async function status(config) {
    const result = await request(config, "/status");
    if (result.api_version !== 3 || !Array.isArray(result.jobs)) {
      throw new Error("Backend and browser versions differ; update both Jamly components.");
    }
    return result;
  }

  function outputURL(config, relative) {
    const base = baseURL(config.url);
    if (typeof relative !== "string" || !relative.startsWith("/outputs/")) {
      throw new Error("The backend returned an invalid output URL.");
    }
    const url = new URL(base + relative);
    if (url.origin !== new URL(base).origin ||
        !url.pathname.startsWith(new URL(base).pathname.replace(/\/$/, "") + "/outputs/")) {
      throw new Error("The backend returned an invalid output URL.");
    }
    return url.href;
  }

  const client = {
    baseURL, outputURL, status, request,
    health: config => request(config, "/healthz"),
    addInputs: (config, inputs, replace = false, options = {}) => request(config, "/add", "POST", {
      inputs, replace_existing: replace, model: options.model, audio_format: options.audio_format,
    }),
    retry: (config, id, replace = false) => request(
      config, "/retry/" + encodeURIComponent(id), "POST", { replace_existing: replace }
    ),
  };
  global.jamlyClient = client;
  if (typeof module !== "undefined" && module.exports) module.exports = client;
})(globalThis);
